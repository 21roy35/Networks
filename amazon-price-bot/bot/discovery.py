"""Full-catalog discovery: find glitch prices across ALL of Amazon.sa.

Brute-force polling every ASIN is impossible (millions of products vs a few
requests per second), so instead we make Amazon's own search index do the
work server-side. Each sweep requests, per catalog department:

    /s?i=<dept>&low-price=<min>&high-price=<max>&s=price-asc-rank
       [&rh=p_n_pct-off-with-tax:<pct>-]        (>=X% off refinement)

One response = ~24-60 products that ALREADY match "costs almost nothing right
now", each card carrying both the current price and the strike-through list
price. A candidate is an item whose list price is large (>= min_list_price_sar)
but whose current price is tiny — i.e. exactly the "500 SAR item at 5 SAR"
pattern. Candidates are then re-verified against the live buy-box before the
alert fires, so search-index staleness can't trigger a false buy.

Cost per sweep: len(categories) x pages_per_category requests (~36 by
default) to screen the whole catalog — versus millions for brute force.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from selectolax.parser import HTMLParser

from .config import Config
from .scraper import BASE, Blocked, PriceResult, check_blocked, fetch_price, headers, parse_amount
from .state import RunState
from .storage import Store

log = logging.getLogger("discovery")

OnDeal = Callable[[PriceResult, float, float], Awaitable[None]]


@dataclass
class Candidate:
    asin: str
    price: float
    list_price: float
    title: str | None


def parse_search_results(body: str) -> list[Candidate]:
    """Extract (asin, current price, strike-through list price) from a search page."""
    tree = HTMLParser(body)
    out: list[Candidate] = []
    for card in tree.css("div[data-component-type='s-search-result']"):
        asin = (card.attributes.get("data-asin") or "").strip()
        if len(asin) != 10:
            continue

        price = list_price = None
        for node in card.css("span.a-price"):
            classes = node.attributes.get("class") or ""
            offscreen = node.css_first(".a-offscreen")
            if offscreen is None:
                continue
            amount = parse_amount(offscreen.text())
            if amount is None:
                continue
            if "a-text-price" in classes:  # strike-through "was" price
                if list_price is None:
                    list_price = amount
            elif price is None:
                price = amount

        if price is None or list_price is None:
            continue

        title_node = card.css_first("h2 span")
        out.append(
            Candidate(
                asin=asin,
                price=price,
                list_price=list_price,
                title=title_node.text(strip=True) if title_node else None,
            )
        )
    return out


class Discovery:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        state: RunState,
        on_deal: OnDeal,
        client: httpx.AsyncClient,
    ):
        self.cfg = cfg
        self.store = store
        self.state = state
        self.on_deal = on_deal
        self.client = client

    # -- one search page ---------------------------------------------------
    def _search_params(self, category: str, page: int) -> dict[str, str]:
        d = self.cfg.discovery
        params = {
            "i": category,
            "s": "price-asc-rank",
            "low-price": f"{d.min_price_sar:g}",
            "high-price": f"{d.max_price_sar:g}",
            "page": str(page),
        }
        if d.use_pct_off_filter and d.min_discount_pct > 0:
            params["rh"] = f"p_n_pct-off-with-tax:{int(d.min_discount_pct)}-"
        return params

    async def _sweep_page(self, category: str, page: int) -> list[Candidate]:
        r = await self.client.get(
            f"{BASE}/s", params=self._search_params(category, page), headers=headers()
        )
        if r.status_code in (503, 429):
            raise Blocked()
        r.raise_for_status()
        check_blocked(r.text)
        return parse_search_results(r.text)

    # -- candidate filtering + verification ---------------------------------
    def _is_candidate(self, c: Candidate) -> bool:
        d = self.cfg.discovery
        if c.price > d.max_price_sar or c.price < d.min_price_sar:
            return False
        if c.list_price < d.min_list_price_sar:
            return False
        discount = (1 - c.price / c.list_price) * 100
        return discount >= d.min_discount_pct

    async def _handle_candidate(self, c: Candidate) -> None:
        cooldown = self.cfg.alerts.realert_cooldown_seconds
        if not self.store.should_alert(c.asin, c.price, cooldown):
            return  # already alerted at this or a lower price recently

        title, price = c.title, c.price
        if self.cfg.discovery.verify_before_alert:
            # Search indexes lag; confirm against the live buy-box before alerting.
            try:
                live = await fetch_price(self.client, c.asin)
            except Blocked:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: verify failed (%s); skipping", c.asin, exc)
                return
            if live.price is None or not live.in_stock:
                self.store.record_discovery(c.asin, c.price, c.list_price, "gone")
                return
            if live.price > self.cfg.discovery.max_price_sar:
                self.store.record_discovery(c.asin, c.price, c.list_price, "stale")
                return
            price, title = live.price, live.title or c.title

        discount = (1 - price / c.list_price) * 100 if c.list_price > 0 else 0.0
        result = PriceResult(asin=c.asin, price=price, title=title, in_stock=True)
        log.info(
            "DISCOVERED %s @ %.2f SAR (list %.0f, -%.0f%%) %s",
            c.asin, price, c.list_price, discount, (title or "")[:60],
        )
        self.store.mark_alerted(c.asin, price)
        self.store.record_discovery(c.asin, price, c.list_price, "alerted")
        await self.on_deal(result, c.list_price, discount)

    # -- main loop -----------------------------------------------------------
    async def run(self) -> None:
        d = self.cfg.discovery
        if not d.enabled:
            log.info("discovery disabled in config")
            return
        sem = asyncio.Semaphore(self.cfg.monitor.concurrency)
        log.info(
            "discovery started: %d categories x %d pages, band %.0f-%.0f SAR, "
            ">=%.0f%% off, list >= %.0f SAR",
            len(d.categories), d.pages_per_category, d.min_price_sar,
            d.max_price_sar, d.min_discount_pct, d.min_list_price_sar,
        )
        while True:
            if self.state.paused:
                await asyncio.sleep(5)
                continue
            await self.sweep_once(sem)
            await asyncio.sleep(d.sweep_interval_seconds + random.uniform(0, 30))

    async def sweep_once(self, sem: asyncio.Semaphore | None = None) -> int:
        """One full catalog sweep. Returns the number of candidates found."""
        d = self.cfg.discovery
        sem = sem or asyncio.Semaphore(self.cfg.monitor.concurrency)
        blocked = False
        found = 0

        async def one(category: str, page: int) -> None:
            nonlocal blocked, found
            async with sem:
                await asyncio.sleep(random.uniform(0, 3))  # stagger
                try:
                    cards = await self._sweep_page(category, page)
                except Blocked:
                    blocked = True
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("sweep %s p%d failed: %s", category, page, exc)
                    return
                for c in cards:
                    if not self._is_candidate(c):
                        continue
                    found += 1
                    try:
                        await self._handle_candidate(c)
                    except Blocked:
                        blocked = True
                        return

        await asyncio.gather(
            *(one(cat, p) for cat in d.categories for p in range(1, d.pages_per_category + 1))
        )
        self.state.sweeps_done += 1
        self.state.candidates_seen += found

        if blocked:
            cooldown = self.cfg.monitor.cooldown_on_block
            log.warning("discovery throttled; cooling down %ds", cooldown)
            await asyncio.sleep(cooldown)
        return found

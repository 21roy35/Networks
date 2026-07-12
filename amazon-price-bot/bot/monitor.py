"""Watchlist polling loop: concurrent price checks -> deal detection -> alert."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable

import httpx

from .config import Config
from .scraper import Blocked, PriceResult, fetch_price
from .state import RunState
from .storage import Store

log = logging.getLogger("monitor")

# Callback signature: (result, ref_price, discount_pct) -> sends the Telegram alert
AlertFn = Callable[[PriceResult, float, float], Awaitable[None]]


def is_deal(price: float, ref_price: float, cfg: Config) -> bool:
    """Both configured rules must pass; a rule set to 0 is disabled."""
    d = cfg.deal
    if d.max_price_sar > 0 and price > d.max_price_sar:
        return False
    if d.min_discount_pct > 0:
        if ref_price <= 0:
            return False  # can't evaluate a discount rule without a reference
        discount = (1 - price / ref_price) * 100
        if discount < d.min_discount_pct:
            return False
    return True


class Monitor:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        state: RunState,
        alert: AlertFn,
        client: httpx.AsyncClient,
    ):
        self.cfg = cfg
        self.store = store
        self.state = state
        self.alert = alert
        self.client = client

    async def run(self) -> None:
        sem = asyncio.Semaphore(self.cfg.monitor.concurrency)
        log.info("monitor started; %d item(s) on watchlist", len(self.store.products()))

        while True:
            if self.state.paused:
                await asyncio.sleep(5)
                continue

            products = self.store.products()
            blocked = False

            async def check(asin: str, ref_price: float) -> None:
                nonlocal blocked
                async with sem:
                    # small stagger so requests don't fire in one burst
                    await asyncio.sleep(random.uniform(0, 2))
                    try:
                        result = await fetch_price(self.client, asin)
                    except Blocked:
                        blocked = True
                        return
                    except Exception as exc:  # noqa: BLE001 - keep the loop alive
                        log.warning("%s: fetch failed: %s", asin, exc)
                        return
                    await self._evaluate(result, ref_price)

            await asyncio.gather(*(check(p.asin, p.ref_price) for p in products))

            if blocked:
                cooldown = self.cfg.monitor.cooldown_on_block
                log.warning("Amazon is throttling us; cooling down %ds", cooldown)
                await asyncio.sleep(cooldown)
                continue

            await asyncio.sleep(
                self.cfg.monitor.poll_interval_seconds
                + random.uniform(0, self.cfg.monitor.jitter_seconds)
            )

    async def _evaluate(self, result: PriceResult, ref_price: float) -> None:
        if result.price is None or not result.in_stock:
            return
        self.store.record_price(result.asin, result.price, result.title)
        # Feeds the glitch scorer's price-history evidence for this ASIN.
        self.store.update_price_stats(result.asin, result.price)

        if not is_deal(result.price, ref_price, self.cfg):
            return
        if not self.store.should_alert(
            result.asin, result.price, self.cfg.alerts.realert_cooldown_seconds
        ):
            return

        discount = (1 - result.price / ref_price) * 100 if ref_price > 0 else 0.0
        log.info("DEAL %s @ %.2f SAR (%.0f%% off)", result.asin, result.price, discount)
        self.store.mark_alerted(result.asin, result.price)
        await self.alert(result, ref_price, discount)

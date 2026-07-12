"""Fast Amazon.sa price fetching over plain HTTP (no browser).

Strategy, in order of cheapness:
  1. The AOD (All Offers Display) AJAX endpoint — a small HTML fragment with
     the buy-box offer, a fraction of the size of the full product page.
  2. The full /dp/ page as fallback, parsed with selectolax (C-speed parser)
     plus regex fallbacks, since Amazon rotates its price markup.

Blocking (captcha / 503 dog page) is detected and reported so the monitor can
back off instead of hammering.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser

BASE = "https://www.amazon.sa"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# "SAR 1,234.56" / "1,234.56 ريال" / plain "9.00"
_PRICE_RE = re.compile(r"(?:SAR|ر\.س|ريال)?\s*([\d,]+(?:\.\d{1,2})?)")
_JSON_PRICE_RE = re.compile(r'"priceAmount"\s*:\s*([\d.]+)')
_BLOCK_MARKERS = (
    "api-services-support@amazon.com",
    "Type the characters you see",
    "/errors/validateCaptcha",
)


class Blocked(Exception):
    """Amazon served a captcha or throttled us."""


@dataclass
class PriceResult:
    asin: str
    price: float | None      # None => no buyable offer found
    title: str | None
    in_stock: bool


@dataclass
class OffersResult:
    asin: str
    buybox: float | None     # pinned / buy-box offer price
    others: list[float]      # competing sellers' prices for the SAME ASIN
    title: str | None


def headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


_headers = headers  # backwards-friendly alias


def make_client(timeout: int, proxy: str = "") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        timeout=timeout,
        follow_redirects=True,
        proxy=proxy or None,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


def parse_amount(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def check_blocked(body: str) -> None:
    if any(marker in body for marker in _BLOCK_MARKERS):
        raise Blocked()


_parse_amount = parse_amount
_check_blocked = check_blocked


def _price_from_html(body: str) -> tuple[float | None, str | None]:
    tree = HTMLParser(body)

    title_node = tree.css_first("#productTitle") or tree.css_first("#aod-asin-title-text")
    title = title_node.text(strip=True) if title_node else None

    # Preferred: hidden a-offscreen span inside the buybox price containers.
    for sel in (
        "#aod-price-0 .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "span.a-price .a-offscreen",
    ):
        node = tree.css_first(sel)
        if node:
            price = _parse_amount(node.text())
            if price is not None:
                return price, title

    # Fallback: embedded JSON blobs carry the buybox price.
    m = _JSON_PRICE_RE.search(body)
    if m:
        return float(m.group(1)), title

    return None, title


async def _fetch_aod(client: httpx.AsyncClient, asin: str) -> str | None:
    """Fetch the All-Offers ajax fragment; None if it wasn't usable."""
    try:
        r = await client.get(
            f"{BASE}/gp/product/ajax/ref=aod_f_new",
            params={"asin": asin, "pc": "dp", "experienceId": "aodAjaxMain"},
            headers=headers(),
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return None
    if r.status_code != 200:
        return None
    check_blocked(r.text)
    return r.text


async def _fetch_dp(client: httpx.AsyncClient, asin: str) -> PriceResult:
    """Full product page fallback."""
    r = await client.get(f"{BASE}/dp/{asin}", params={"psc": "1"}, headers=headers())
    if r.status_code in (503, 429):
        raise Blocked()
    r.raise_for_status()
    check_blocked(r.text)

    price, title = _price_from_html(r.text)
    unavailable = "currently unavailable" in r.text.lower()
    return PriceResult(
        asin=asin, price=price, title=title, in_stock=price is not None and not unavailable
    )


def parse_offer_prices(body: str) -> tuple[list[float], str | None]:
    """All non-strike-through offer prices in an AOD fragment, buy-box first."""
    tree = HTMLParser(body)
    title_node = tree.css_first("#aod-asin-title-text") or tree.css_first("#productTitle")
    title = title_node.text(strip=True) if title_node else None

    prices: list[float] = []
    for node in tree.css("span.a-price"):
        if "a-text-price" in (node.attributes.get("class") or ""):
            continue  # strike-through "was" price, not an offer
        offscreen = node.css_first(".a-offscreen")
        if offscreen is None:
            continue
        amount = parse_amount(offscreen.text())
        if amount is not None:
            prices.append(amount)
    return prices, title


async def fetch_price(client: httpx.AsyncClient, asin: str) -> PriceResult:
    """Fetch the current buy-box price for an ASIN, cheapest request first."""
    body = await _fetch_aod(client, asin)
    if body is not None:
        price, title = _price_from_html(body)
        if price is not None:
            return PriceResult(asin=asin, price=price, title=title, in_stock=True)
    return await _fetch_dp(client, asin)


async def fetch_offers(client: httpx.AsyncClient, asin: str) -> OffersResult:
    """Buy-box price PLUS competing sellers' prices, in one request.

    The competing prices are the key anti-fake signal: a seller can invent a
    strike-through list price, but they can't invent other sellers offering
    the same ASIN at 480 SAR."""
    body = await _fetch_aod(client, asin)
    if body is not None:
        prices, title = parse_offer_prices(body)
        if prices:
            buybox = prices[0]
            # Drop duplicates of the buy-box price (the pinned offer repeats
            # in the list) so a single-seller item yields others == [].
            others = [p for p in prices[1:] if abs(p - buybox) > 0.01]
            return OffersResult(asin=asin, buybox=buybox, others=others, title=title)

    dp = await _fetch_dp(client, asin)
    return OffersResult(
        asin=asin, buybox=dp.price if dp.in_stock else None, others=[], title=dp.title
    )

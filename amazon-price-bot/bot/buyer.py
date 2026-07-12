"""One-click checkout via Playwright using your saved Amazon session.

You authenticate ONCE with `python -m bot.login` (a real browser window where
you log in yourself); the session cookies are saved to `amazon_session.json`
and reused here. Your password is never stored by the bot.

Flow: product page -> "Buy Now" -> Turbo (popup) checkout if offered,
otherwise the classic checkout page -> re-verify the live price against the
safety cap -> Place Order. A screenshot is captured at every terminal state
so you always have evidence of what happened.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import time
from dataclasses import dataclass

from playwright.async_api import Error as PWError, TimeoutError as PWTimeout, async_playwright

from .config import Config
from .scraper import BASE

log = logging.getLogger("buyer")

_AMOUNT_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)")


@dataclass
class BuyResult:
    ok: bool
    message: str
    screenshot: pathlib.Path | None = None


def _amount(text: str) -> float | None:
    m = _AMOUNT_RE.search(text.replace("‏", "").replace("‎", ""))
    return float(m.group(1).replace(",", "")) if m else None


class Buyer:
    """Serialises purchases: one checkout at a time."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = asyncio.Lock()

    async def buy(self, asin: str) -> BuyResult:
        async with self._lock:
            return await self._buy(asin)

    async def _buy(self, asin: str) -> BuyResult:
        cfg = self.cfg.buy
        state = self.cfg.storage_state_path
        if not state.exists():
            return BuyResult(False, "No Amazon session found. Run `python -m bot.login` first.")

        shots = self.cfg.base_dir / cfg.screenshot_dir
        shots.mkdir(exist_ok=True)
        shot = shots / f"{asin}-{int(time.time())}.png"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=cfg.headless)
            context = await browser.new_context(
                storage_state=str(state),
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = await context.new_page()
            try:
                await page.goto(f"{BASE}/dp/{asin}?psc=1", wait_until="domcontentloaded")

                if await page.locator("form[action*='validateCaptcha']").count():
                    await page.screenshot(path=shot)
                    return BuyResult(False, "Amazon showed a captcha; try again shortly.", shot)
                if "/ap/signin" in page.url:
                    return BuyResult(
                        False, "Session expired — run `python -m bot.login` again.", None
                    )

                # Sanity-check the product-page price before clicking anything.
                price_node = page.locator(
                    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, "
                    "#corePrice_feature_div .a-price .a-offscreen"
                ).first
                try:
                    page_price = _amount(await price_node.inner_text(timeout=5000) or "")
                except PWTimeout:
                    page_price = None
                if page_price is not None and page_price > cfg.max_auto_price_sar:
                    await page.screenshot(path=shot)
                    return BuyResult(
                        False,
                        f"Aborted: live price is {page_price:.2f} SAR, above your "
                        f"max_auto_price_sar cap of {cfg.max_auto_price_sar:.2f}.",
                        shot,
                    )

                buy_now = page.locator("#buy-now-button")
                if not await buy_now.count():
                    await page.screenshot(path=shot)
                    return BuyResult(False, "No Buy Now button (deal gone or out of stock).", shot)
                await buy_now.click()

                # Path A: Turbo checkout iframe (one-tap popup)
                try:
                    frame = page.frame_locator("#turbo-checkout-iframe")
                    place = frame.locator("#turbo-checkout-pyo-button")
                    await place.wait_for(state="visible", timeout=8000)
                    total_txt = await frame.locator(
                        "#turbo-checkout-panel-container"
                    ).inner_text(timeout=3000)
                    if not self._total_ok(total_txt):
                        await page.screenshot(path=shot)
                        return BuyResult(
                            False, "Aborted at Turbo checkout: total exceeds safety cap.", shot
                        )
                    await place.click()
                    await page.wait_for_load_state("domcontentloaded")
                    await page.screenshot(path=shot)
                    return self._confirm(page.url, shot)
                except PWTimeout:
                    pass  # no turbo popup -> classic checkout

                # Path B: classic single-page checkout
                await page.wait_for_load_state("domcontentloaded")
                if "/ap/signin" in page.url:
                    return BuyResult(
                        False, "Amazon asked to re-authenticate — run `python -m bot.login`.", None
                    )

                order_total = page.locator(
                    "#subtotals .grand-total-price, .order-summary-grand-total, "
                    "#subtotals-marketplace-table .grand-total-price"
                ).first
                try:
                    if not self._total_ok(await order_total.inner_text(timeout=6000)):
                        await page.screenshot(path=shot)
                        return BuyResult(
                            False, "Aborted at checkout: order total exceeds safety cap.", shot
                        )
                except PWTimeout:
                    log.warning("%s: could not read order total; relying on page-price check", asin)

                place = page.locator(
                    "#placeOrder, input[name='placeYourOrder1'], #submitOrderButtonId"
                ).first
                await place.wait_for(state="visible", timeout=10000)
                await place.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.screenshot(path=shot)
                return self._confirm(page.url, shot)

            except (PWTimeout, PWError) as exc:
                try:
                    await page.screenshot(path=shot)
                except PWError:
                    shot = None
                return BuyResult(False, f"Checkout failed: {exc.__class__.__name__}: {exc}", shot)
            finally:
                await context.close()
                await browser.close()

    def _total_ok(self, total_text: str) -> bool:
        total = _amount(total_text)
        return total is not None and total <= self.cfg.buy.max_auto_price_sar

    @staticmethod
    def _confirm(url: str, shot: pathlib.Path) -> BuyResult:
        if "thankyou" in url or "thank-you" in url:
            return BuyResult(True, "Order placed ✅ — check the screenshot and your email.", shot)
        return BuyResult(
            False,
            "Order not confirmed (possibly a card OTP/verification step — see screenshot).",
            shot,
        )

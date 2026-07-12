"""One-time interactive Amazon.sa login: `python -m bot.login`.

Opens a real browser window. Log in yourself (including any OTP), then come
back to the terminal and press Enter. Only the session cookies are saved —
the bot never sees or stores your password.
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

from .config import load_config
from .scraper import BASE


async def main() -> None:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        await page.goto(
            f"{BASE}/ap/signin?openid.return_to={BASE}&openid.mode=checkid_setup"
            "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
            "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
            "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
            "&openid.assoc_handle=saflex"
        )
        print("\nLog in to Amazon.sa in the browser window (complete any OTP).")
        input("When you can see your account page / homepage as signed-in, press Enter here... ")
        await context.storage_state(path=str(cfg.storage_state_path))
        await browser.close()
        print(f"Session saved to {cfg.storage_state_path}")
        print("Tip: make sure your default address and payment method are set on Amazon,")
        print("and that your card doesn't require an OTP for every purchase, or the")
        print("auto-buy will stop at the bank verification step.")


if __name__ == "__main__":
    asyncio.run(main())

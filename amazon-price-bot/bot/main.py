"""Entrypoint: `python -m bot.main [config.yaml]`."""
from __future__ import annotations

import asyncio
import logging
import sys

from .buyer import Buyer
from .config import load_config
from .discovery import Discovery
from .monitor import Monitor
from .scraper import PriceResult, make_client
from .state import RunState
from .storage import Store
from .telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")


async def run() -> None:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    store = Store(cfg.base_dir / "sniper.db")
    state = RunState()

    # Seed the DB watchlist from config (DB is the source of truth afterwards).
    for item in cfg.watchlist:
        store.upsert_product(item.asin, item.ref_price)

    buyer = Buyer(cfg)
    tg = TelegramBot(cfg, store, buyer, state)
    client = make_client(cfg.monitor.request_timeout, cfg.monitor.proxy)

    background: set[asyncio.Task] = set()

    async def on_deal(
        result: PriceResult, ref_price: float, discount: float, evidence: str | None = None
    ) -> None:
        """Shared deal sink for watchlist hits and catalog discoveries."""
        auto = (
            cfg.buy.enabled
            and cfg.buy.auto_buy
            and result.price is not None
            and result.price <= cfg.buy.max_auto_price_sar
        )
        await tg.send_deal_alert(result, ref_price, discount, evidence, auto_buying=auto)
        if auto:
            # Don't stall the sweep while checkout runs; Buyer serialises itself.
            task = asyncio.create_task(tg.do_buy(result.asin))
            background.add(task)
            task.add_done_callback(background.discard)

    monitor = Monitor(cfg, store, state, on_deal, client)
    discovery = Discovery(cfg, store, state, on_deal, client)
    if cfg.discovery.enabled:
        tg.sweep_now = discovery.sweep_once

    async with client, tg.app:
        await tg.app.start()
        await tg.app.updater.start_polling(drop_pending_updates=True)
        try:
            tasks = [asyncio.create_task(monitor.run(), name="monitor")]
            if cfg.discovery.enabled:
                tasks.append(asyncio.create_task(discovery.run(), name="discovery"))
            await asyncio.gather(*tasks)  # runs forever
        finally:
            await tg.app.updater.stop()
            await tg.app.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

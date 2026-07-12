"""Entrypoint: `python -m bot.main [config.yaml]`."""
from __future__ import annotations

import asyncio
import logging
import sys

from .buyer import Buyer
from .config import load_config
from .monitor import Monitor
from .storage import Store
from .telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def run() -> None:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    store = Store(cfg.base_dir / "sniper.db")

    # Seed the DB watchlist from config (DB is the source of truth afterwards).
    for item in cfg.watchlist:
        store.upsert_product(item.asin, item.ref_price)

    buyer = Buyer(cfg)
    tg = TelegramBot(cfg, store, buyer)
    monitor = Monitor(cfg, store, tg.send_deal_alert)

    async with tg.app:
        await tg.app.start()
        await tg.app.updater.start_polling(drop_pending_updates=True)
        try:
            await monitor.run()  # runs forever
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

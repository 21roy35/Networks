"""Typed config loading for the sniper bot."""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True)
class TelegramCfg:
    bot_token: str
    chat_id: int


@dataclasses.dataclass(frozen=True)
class MonitorCfg:
    poll_interval_seconds: int = 45
    jitter_seconds: int = 20
    concurrency: int = 4
    request_timeout: int = 15
    cooldown_on_block: int = 600


@dataclasses.dataclass(frozen=True)
class DealCfg:
    max_price_sar: float = 10.0
    min_discount_pct: float = 85.0


@dataclasses.dataclass(frozen=True)
class AlertsCfg:
    realert_cooldown_seconds: int = 3600


@dataclasses.dataclass(frozen=True)
class BuyCfg:
    enabled: bool = True
    headless: bool = True
    storage_state: str = "amazon_session.json"
    max_auto_price_sar: float = 15.0
    quantity: int = 1
    screenshot_dir: str = "screenshots"


@dataclasses.dataclass(frozen=True)
class WatchItem:
    asin: str
    ref_price: float


@dataclasses.dataclass(frozen=True)
class Config:
    telegram: TelegramCfg
    monitor: MonitorCfg
    deal: DealCfg
    alerts: AlertsCfg
    buy: BuyCfg
    watchlist: list[WatchItem]
    base_dir: pathlib.Path

    @property
    def storage_state_path(self) -> pathlib.Path:
        return self.base_dir / self.buy.storage_state


def _sub(cls: type, raw: dict[str, Any] | None):
    raw = raw or {}
    fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in fields})


def load_config(path: str | pathlib.Path = "config.yaml") -> Config:
    path = pathlib.Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    tg = raw.get("telegram") or {}
    if not tg.get("bot_token") or not tg.get("chat_id"):
        raise SystemExit("config.yaml: telegram.bot_token and telegram.chat_id are required")

    watchlist = [
        WatchItem(asin=str(item["asin"]).strip(), ref_price=float(item.get("ref_price", 0)))
        for item in (raw.get("watchlist") or [])
    ]

    return Config(
        telegram=TelegramCfg(bot_token=str(tg["bot_token"]), chat_id=int(tg["chat_id"])),
        monitor=_sub(MonitorCfg, raw.get("monitor")),
        deal=_sub(DealCfg, raw.get("deal")),
        alerts=_sub(AlertsCfg, raw.get("alerts")),
        buy=_sub(BuyCfg, raw.get("buy")),
        watchlist=watchlist,
        base_dir=path.resolve().parent,
    )

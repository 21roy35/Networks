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
    proxy: str = ""  # optional, e.g. "http://user:pass@host:port"


# Default catalog slices for full-catalog discovery. These are Amazon search
# department aliases (the `i=` parameter of /s); edit freely in config.yaml.
_DEFAULT_CATEGORIES = [
    "electronics",
    "computers",
    "kitchen",
    "home",
    "appliances",
    "toys",
    "sporting",
    "beauty",
    "office-products",
    "tools",
    "automotive",
    "videogames",
]


@dataclasses.dataclass(frozen=True)
class DiscoveryCfg:
    enabled: bool = True
    sweep_interval_seconds: int = 300
    categories: list[str] = dataclasses.field(default_factory=lambda: list(_DEFAULT_CATEGORIES))
    min_price_sar: float = 1.0
    max_price_sar: float = 10.0
    min_list_price_sar: float = 100.0
    min_discount_pct: float = 90.0
    pages_per_category: int = 3
    use_pct_off_filter: bool = True
    verify_before_alert: bool = True


@dataclasses.dataclass(frozen=True)
class DealCfg:
    max_price_sar: float = 10.0
    min_discount_pct: float = 85.0


@dataclasses.dataclass(frozen=True)
class AlertsCfg:
    realert_cooldown_seconds: int = 3600


# Product types that habitually carry fabricated strike-through prices, plus
# digital goods that can't be sniped. Matched case-insensitively against the
# product title; extend in config.yaml (Arabic terms welcome).
_DEFAULT_BLOCKED_KEYWORDS = [
    "sticker",
    "decal",
    "keychain",
    "key chain",
    "lanyard",
    "screen protector",
    "tempered glass",
    "phone case",
    "case for",
    "cover for",
    "skin for",
    "washi tape",
    "temporary tattoo",
    "gift card",
    "e-gift",
    "ebook",
    "sim card",
    "wallpaper",
    "poster",
]


@dataclasses.dataclass(frozen=True)
class FilterCfg:
    min_score: int = 40           # evidence score a discovery needs to alert
    min_reviews: int = 20         # ratings needed for the social-proof signal
    blocked_keywords: list[str] = dataclasses.field(
        default_factory=lambda: list(_DEFAULT_BLOCKED_KEYWORDS)
    )


@dataclasses.dataclass(frozen=True)
class BuyCfg:
    enabled: bool = True
    auto_buy: bool = False  # buy instantly on alert, without waiting for the button
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
    discovery: DiscoveryCfg
    deal: DealCfg
    filter: FilterCfg
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
        discovery=_sub(DiscoveryCfg, raw.get("discovery")),
        deal=_sub(DealCfg, raw.get("deal")),
        filter=_normalize_filter(_sub(FilterCfg, raw.get("filter"))),
        alerts=_sub(AlertsCfg, raw.get("alerts")),
        buy=_sub(BuyCfg, raw.get("buy")),
        watchlist=watchlist,
        base_dir=path.resolve().parent,
    )


def _normalize_filter(f: FilterCfg) -> FilterCfg:
    return dataclasses.replace(
        f, blocked_keywords=[str(k).lower() for k in f.blocked_keywords]
    )

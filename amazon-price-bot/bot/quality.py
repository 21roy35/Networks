"""Glitch-quality scoring: is this a real high-value item at a broken price,
or a sticker pack with a fabricated strike-through "was" price?

The core problem: the list price printed on a search card is SELLER-SUPPLIED
and routinely faked on junk listings, so it can gate nothing by itself.
Instead, each candidate must earn a score from evidence Amazon can't easily
fake, and only candidates at or above `filter.min_score` alert:

  +50  competing offers — another seller lists the SAME ASIN at a real price
       (>= min_list_price_sar). Parsed from the All-Offers response we already
       fetch during verification, so this signal costs zero extra requests.
  +50  own price history — this bot previously observed the ASIN's buy-box at
       a real price (builds up automatically while the bot runs).
  +25  social proof — the search card shows >= min_reviews ratings…
  +15  …with an average of 4.0+ stars.
  -35  anti-signal: other sellers exist and ALL of them are cheap too,
       i.e. the item is genuinely a low-value product, not a glitch.

Default threshold 40 means: competing-offer evidence alone passes, price
history alone passes, strong reviews (count + rating) pass — but a no-name,
zero-review, single-offer listing with a big claimed discount does not.

Before any of that, titles matching `filter.blocked_keywords` are rejected
outright (stickers, screen protectors, gift cards, …) — the fastest gate.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from .config import Config
from .storage import Store

log = logging.getLogger("quality")


@dataclass
class Verdict:
    passed: bool
    score: int
    evidence: str  # human-readable summary, shown in the Telegram alert


class GlitchScorer:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    def blocked_keyword(self, *titles: str | None) -> str | None:
        """Return the blocklist keyword that matches any given title, if any."""
        for title in titles:
            if not title:
                continue
            low = title.lower()
            for kw in self.cfg.filter.blocked_keywords:
                if kw in low:
                    return kw
        return None

    def score(
        self,
        asin: str,
        reviews: int,
        rating: float,
        other_offers: list[float],
    ) -> Verdict:
        f = self.cfg.filter
        value_floor = self.cfg.discovery.min_list_price_sar
        score = 0
        evidence: list[str] = []

        # 1) Competing offers at a real price = independent proof of value.
        value_offers = [p for p in other_offers if p >= value_floor]
        if value_offers:
            score += 50
            evidence.append(
                f"{len(value_offers)} other seller(s) at ~"
                f"{statistics.median(value_offers):.0f} SAR"
            )
        elif other_offers:
            # Every other seller is cheap too -> genuinely low-value product.
            score -= 35
            evidence.append(f"all {len(other_offers)} other offer(s) are cheap too")

        # 2) Our own observation history: we saw this ASIN priced high before.
        hist = self.store.max_seen_price(asin)
        if hist is not None and hist >= value_floor:
            score += 50
            evidence.append(f"seen at {hist:.0f} SAR by this bot before")

        # 3) Social proof from the search card.
        if reviews >= f.min_reviews:
            score += 25
            evidence.append(f"{reviews} ratings")
            if rating >= 4.0:
                score += 15
                evidence.append(f"{rating:.1f}★")

        return Verdict(
            passed=score >= f.min_score,
            score=score,
            evidence="; ".join(evidence) if evidence else "no independent evidence of value",
        )

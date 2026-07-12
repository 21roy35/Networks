"""SQLite persistence for the watchlist and alert dedup state.

sqlite3 is used synchronously: every call here touches a tiny local DB for a
few microseconds, which is cheaper than dragging in an async driver.
"""
from __future__ import annotations

import pathlib
import sqlite3
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    asin        TEXT PRIMARY KEY,
    ref_price   REAL NOT NULL DEFAULT 0,
    last_price  REAL,
    last_seen   REAL,
    title       TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    asin        TEXT NOT NULL,
    price       REAL NOT NULL,
    alerted_at  REAL NOT NULL,
    PRIMARY KEY (asin, price)
);
CREATE TABLE IF NOT EXISTS discoveries (
    asin        TEXT PRIMARY KEY,
    price       REAL NOT NULL,
    list_price  REAL NOT NULL,
    status      TEXT NOT NULL,
    seen_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT NOT NULL,
    price       REAL NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    created_at  REAL NOT NULL
);
"""


@dataclass
class Product:
    asin: str
    ref_price: float
    last_price: float | None
    title: str | None


class Store:
    def __init__(self, path: pathlib.Path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- watchlist ---------------------------------------------------------
    def upsert_product(self, asin: str, ref_price: float | None = None) -> None:
        self._db.execute(
            "INSERT INTO products (asin, ref_price) VALUES (?, COALESCE(?, 0)) "
            "ON CONFLICT(asin) DO UPDATE SET ref_price = COALESCE(?, ref_price)",
            (asin, ref_price, ref_price),
        )
        self._db.commit()

    def remove_product(self, asin: str) -> bool:
        cur = self._db.execute("DELETE FROM products WHERE asin = ?", (asin,))
        self._db.commit()
        return cur.rowcount > 0

    def products(self) -> list[Product]:
        rows = self._db.execute(
            "SELECT asin, ref_price, last_price, title FROM products ORDER BY asin"
        ).fetchall()
        return [Product(*row) for row in rows]

    def record_price(self, asin: str, price: float, title: str | None) -> None:
        self._db.execute(
            "UPDATE products SET last_price = ?, last_seen = ?, title = COALESCE(?, title) "
            "WHERE asin = ?",
            (price, time.time(), title, asin),
        )
        self._db.commit()

    # -- alert dedup -------------------------------------------------------
    def should_alert(self, asin: str, price: float, cooldown: int) -> bool:
        """True unless we already alerted this ASIN at this (or a lower) price
        within the cooldown window. A *further* drop always re-alerts."""
        row = self._db.execute(
            "SELECT MIN(price), MAX(alerted_at) FROM alerts "
            "WHERE asin = ? AND alerted_at > ?",
            (asin, time.time() - cooldown),
        ).fetchone()
        min_price, _ = row
        return min_price is None or price < min_price

    def mark_alerted(self, asin: str, price: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO alerts (asin, price, alerted_at) VALUES (?, ?, ?)",
            (asin, price, time.time()),
        )
        self._db.commit()

    # -- discoveries -------------------------------------------------------
    def record_discovery(self, asin: str, price: float, list_price: float, status: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO discoveries (asin, price, list_price, status, seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (asin, price, list_price, status, time.time()),
        )
        self._db.commit()

    def discovery_stats(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM discoveries GROUP BY status"
        ).fetchall()
        return dict(rows)

    # -- purchases ---------------------------------------------------------
    def record_purchase(self, asin: str, price: float, status: str, detail: str) -> None:
        self._db.execute(
            "INSERT INTO purchases (asin, price, status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (asin, price, status, detail, time.time()),
        )
        self._db.commit()

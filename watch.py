"""
watch.py — Price-watch persistence and drop-detection for Whenever.

WatchDB wraps stdlib sqlite3 with a two-table schema (watches, price_history).
check_all_watches iterates active watches, calls the fare function, compares
against the stored last_price, and returns a list of drop records.

Prices come only from app.get_fare (real provider chain) — never fabricated.
"""

import datetime
import sqlite3
from typing import Callable, Optional

import requests


# ---------------------------------------------------------------------------
# WatchDB
# ---------------------------------------------------------------------------

class WatchDB:
    """SQLite-backed store for price watches.

    Accept ":memory:" for tests (no WAL mode in that case — WAL is file-only).
    """

    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS watches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                origin        TEXT NOT NULL,
                dest_iata     TEXT NOT NULL,
                dest_city     TEXT,
                dep_date      TEXT NOT NULL,
                ret_date      TEXT NOT NULL,
                adults        INTEGER NOT NULL DEFAULT 2,
                children      INTEGER NOT NULL DEFAULT 0,
                threshold_pct REAL NOT NULL DEFAULT 25.0,
                last_price    INTEGER,
                last_source   TEXT,
                created_at    TEXT NOT NULL,
                active        INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id   INTEGER NOT NULL REFERENCES watches(id),
                checked_at TEXT NOT NULL,
                price      INTEGER,
                source     TEXT,
                book       TEXT
            );
        """)
        self._conn.commit()

    def add_watch(
        self,
        origin: str,
        dest_iata: str,
        dest_city: Optional[str],
        dep_date: str,
        ret_date: str,
        adults: int = 2,
        children: int = 0,
        threshold_pct: float = 25.0,
        last_price: Optional[int] = None,
        last_source: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> int:
        if created_at is None:
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = self._conn.execute(
            """INSERT INTO watches
               (origin, dest_iata, dest_city, dep_date, ret_date,
                adults, children, threshold_pct, last_price, last_source,
                created_at, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
            (origin, dest_iata, dest_city, dep_date, ret_date,
             adults, children, threshold_pct, last_price, last_source, created_at),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_watches(self, active_only: bool = True):
        if active_only:
            cur = self._conn.execute("SELECT * FROM watches WHERE active=1 ORDER BY id")
        else:
            cur = self._conn.execute("SELECT * FROM watches ORDER BY id")
        return [dict(row) for row in cur.fetchall()]

    def remove_watch(self, watch_id: int):
        self._conn.execute("UPDATE watches SET active=0 WHERE id=?", (watch_id,))
        self._conn.commit()

    def update_price(
        self,
        watch_id: int,
        price: Optional[int],
        source: Optional[str],
        book: Optional[str],
        checked_at: str,
    ):
        """Insert a price_history row and update watches.last_price/last_source.

        If price is None, record it in history but do NOT overwrite the stored
        last_price (keeps the last real price alive for future drop comparisons).
        """
        self._conn.execute(
            """INSERT INTO price_history (watch_id, checked_at, price, source, book)
               VALUES (?,?,?,?,?)""",
            (watch_id, checked_at, price, source, book),
        )
        if price is not None:
            self._conn.execute(
                "UPDATE watches SET last_price=?, last_source=? WHERE id=?",
                (price, source, watch_id),
            )
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# check_all_watches
# ---------------------------------------------------------------------------

def check_all_watches(
    db: WatchDB,
    fare_fn: Optional[Callable] = None,
    webhook_url: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> list:
    """Check all active watches; return a list of drop dicts.

    fare_fn defaults to app.get_fare (imported lazily to avoid a circular import
    at module load time when tests inject a fake).

    A drop is: new price strictly less than last_price (and last_price is not None).
    On each check, price_history is updated and last_price is refreshed (if not None).
    """
    if fare_fn is None:
        from app import get_fare as fare_fn  # lazy import

    if now_iso is None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    drops = []
    for watch in db.list_watches(active_only=True):
        watch_id = watch["id"]
        origin = watch["origin"]
        dest_iata = watch["dest_iata"]
        dep_date = watch["dep_date"]
        ret_date = watch["ret_date"]
        adults = watch["adults"]
        children = watch["children"]
        last_price = watch["last_price"]

        fare = fare_fn(origin, dest_iata, dep_date, ret_date, adults, children)
        new_price = fare.get("cheapest_cad") if fare else None
        source = fare.get("source") if fare else None
        book = fare.get("book") if fare else None

        # Some providers (e.g. Amadeus) price a fare but give no booking
        # deep-link. Fall back to a Kayak handoff so every alert has a usable
        # booking URL — same behaviour as the web search route. The watch row
        # stores `children` as a count, not ages, so pass [] for child_ages
        # (origin/dest/dates/adults still encode a valid handoff).
        if not book:
            from app import kayak_link  # lazy import (avoids circular import)
            book = kayak_link(origin, dest_iata, dep_date, ret_date, adults, [])

        # Record the check (null price OK in history)
        db.update_price(watch_id, new_price, source, book, now_iso)

        if new_price is None:
            # No data — do not detect a drop; last_price is preserved by update_price
            continue

        # Detect drop
        if last_price is not None and new_price < last_price:
            delta = new_price - last_price
            drop = {
                "watch_id": watch_id,
                "origin": origin,
                "dest_iata": dest_iata,
                "dest_city": watch.get("dest_city"),
                "dep_date": dep_date,
                "ret_date": ret_date,
                "old_price": last_price,
                "new_price": new_price,
                "delta": delta,
                "source": source,
                "book": book,
            }
            drops.append(drop)

            _print_drop(drop)
            if webhook_url:
                _post_webhook(webhook_url, drop)

    return drops


def _print_drop(drop: dict):
    print(
        f"[PRICE DROP] {drop['origin']}→{drop['dest_iata']} "
        f"{drop['dep_date']}/{drop['ret_date']} "
        f"CA${drop['old_price']:,} → CA${drop['new_price']:,} "
        f"({drop['delta']:,}) "
        f"book: {drop['book']}"
    )


def _post_webhook(url: str, drop: dict):
    try:
        requests.post(url, json=drop, timeout=10)
    except Exception:
        pass

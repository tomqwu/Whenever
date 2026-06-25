"""
watch.py — Price-watch persistence and drop-detection for Whenever.

WatchDB wraps stdlib sqlite3 with a two-table schema (watches, price_history).
check_all_watches iterates active watches, calls the fare function, compares
against the stored last_price, and returns a list of drop records.

Prices come only from app.get_fare (real provider chain) — never fabricated.
"""

import datetime
import json
import sqlite3
from typing import Callable, Optional

import requests


# Placeholder age used to encode the correct passenger COUNT in a kayak
# fallback link when a watch stored a children count but no exact ages.
# It is approximate (the traveler adjusts ages at the booking site) and only
# used when exact ages weren't saved.
DEFAULT_CHILD_AGE = 10


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
        try:
            self._create_tables()
        except Exception:
            # Don't leak the open connection if schema/index setup fails (e.g.
            # an unresolvable duplicate-active-rows IntegrityError).
            self.close()
            raise

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
                child_ages    TEXT NOT NULL DEFAULT '[]',
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
        # Canonicalize stored child_ages (sort) so order-variants like [11,9]
        # and [9,11] share identical raw JSON text. A DB written before ages were
        # sorted on store can hold both variants for the same trip; without this
        # pass the de-dup GROUP BY (and the raw-text unique index) would treat
        # them as distinct and let two active rows survive. Idempotent: a no-op
        # when every row is already canonical.
        self._canonicalize_child_ages()
        # Before creating the partial UNIQUE INDEX, collapse any pre-existing
        # duplicate active rows. A DB created before this index (or before
        # `children` joined the key) can hold multiple active rows for the same
        # trip key; CREATE UNIQUE INDEX would then raise IntegrityError and brick
        # every code path that opens the DB. De-duping first is idempotent and a
        # no-op on a fresh/empty DB.
        self._dedupe_active_watches()
        # At most one ACTIVE watch per trip key. This is the atomic backstop for
        # the route's pre-check: two concurrent identical POSTs can both pass a
        # list_watches() check before either inserts, but only one INSERT can
        # satisfy this partial unique index — the second raises
        # sqlite3.IntegrityError, which the route treats as "already exists".
        # The key includes `children` (the passenger COUNT) so a count-only watch
        # (children=2, child_ages=[]) is distinct from an adults-only one
        # (children=0, child_ages=[]) — they are priced for different parties.
        # Partial (WHERE active = 1) so removing a watch (active -> 0) frees the
        # key and the same trip can be re-watched later.
        try:
            self._conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_active_unique
                    ON watches(origin, dest_iata, dep_date, ret_date,
                               adults, children, child_ages)
                    WHERE active = 1;
            """)
        except sqlite3.IntegrityError:
            # A residual duplicate slipped through (e.g. a concurrent writer):
            # de-dupe once more and retry. If it still fails, surface a clear,
            # actionable error rather than a bare IntegrityError from init.
            self._dedupe_active_watches()
            try:
                self._conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_active_unique
                        ON watches(origin, dest_iata, dep_date, ret_date,
                                   adults, children, child_ages)
                        WHERE active = 1;
                """)
            except sqlite3.IntegrityError as exc:
                raise sqlite3.IntegrityError(
                    "Could not create idx_watch_active_unique: duplicate active "
                    "watch rows remain after de-dup. Inspect the 'watches' table "
                    f"for active duplicates on the trip key. ({exc})"
                ) from exc
        self._conn.commit()

    def _canonicalize_child_ages(self):
        """Rewrite any non-canonical stored child_ages JSON to sorted form.

        add_watch sorts ages before storing, but a DB written before that change
        can hold unsorted text (e.g. "[11, 9]"). Re-encoding to the canonical
        sorted text ("[9, 11]") lets the de-dup pass and the raw-text unique
        index see order-variants of the same party as one key. Idempotent: rows
        already canonical are left untouched.
        """
        rows = self._conn.execute(
            "SELECT id, child_ages FROM watches"
        ).fetchall()
        for row in rows:
            raw = row["child_ages"]
            ages = json.loads(raw or "[]")
            canonical = json.dumps(sorted(ages))
            if canonical != raw:
                self._conn.execute(
                    "UPDATE watches SET child_ages=? WHERE id=?",
                    (canonical, row["id"]),
                )
        self._conn.commit()

    def _dedupe_active_watches(self):
        """Keep one active row per trip key; mark the rest inactive.

        For each set of active rows sharing the unique-index key
        (origin, dest_iata, dep_date, ret_date, adults, children, child_ages),
        keep the lowest id active and set active=0 on the others. Idempotent and
        safe on every init: a fresh/empty DB or an already-deduped DB is a no-op.
        """
        self._conn.execute("""
            UPDATE watches SET active = 0
            WHERE active = 1
              AND id NOT IN (
                  SELECT MIN(id) FROM watches
                  WHERE active = 1
                  GROUP BY origin, dest_iata, dep_date, ret_date,
                           adults, children, child_ages
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
        child_ages: Optional[list] = None,
    ) -> int:
        if created_at is None:
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if child_ages is None:
            child_ages = []
        # Reconcile the two passenger inputs:
        #   - If ages are supplied, they are authoritative (count = len(ages)).
        #   - Otherwise preserve the explicit `children` count (ages unknown),
        #     and store an empty ages list.
        # Canonicalize ages by sorting before storage so the partial unique
        # index (keyed on the raw child_ages JSON text) and the route's
        # order-insensitive dedup pre-check agree: [11,9] and [9,11] for the
        # same trip become the identical stored text "[9, 11]", collapsing to a
        # single active row instead of two index-distinct duplicates.
        if child_ages:
            child_ages = sorted(child_ages)
            children = len(child_ages)
        else:
            child_ages = []
        cur = self._conn.execute(
            """INSERT INTO watches
               (origin, dest_iata, dest_city, dep_date, ret_date,
                adults, children, child_ages, threshold_pct,
                last_price, last_source, created_at, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (origin, dest_iata, dest_city, dep_date, ret_date,
             adults, children, json.dumps(child_ages), threshold_pct,
             last_price, last_source, created_at),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_watches(self, active_only: bool = True):
        if active_only:
            cur = self._conn.execute("SELECT * FROM watches WHERE active=1 ORDER BY id")
        else:
            cur = self._conn.execute("SELECT * FROM watches ORDER BY id")
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["child_ages"] = json.loads(d.get("child_ages") or "[]")
            rows.append(d)
        return rows

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

    def set_baseline(
        self,
        watch_id: int,
        last_price: Optional[int],
        last_source: Optional[str],
    ):
        """Seed a watch's baseline (last_price/last_source) without history.

        Unlike update_price, this records no price_history row — it is a manual
        seed for a watch that was created before a fare was known, so the
        scheduler's first run can detect a real drop against this baseline.
        """
        self._conn.execute(
            "UPDATE watches SET last_price=?, last_source=? WHERE id=?",
            (last_price, last_source, watch_id),
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
        child_ages = watch.get("child_ages") or []
        # Use the stored children COUNT (may exceed len(child_ages) when ages
        # were unknown at save time); the kayak fallback still uses child_ages.
        children = watch["children"]
        last_price = watch["last_price"]

        fare = fare_fn(origin, dest_iata, dep_date, ret_date, adults, children)
        new_price = fare.get("cheapest_cad") if fare else None
        source = fare.get("source") if fare else None
        book = fare.get("book") if fare else None

        # Some providers (e.g. Amadeus) price a fare but give no booking
        # deep-link. Fall back to a Kayak handoff so every alert has a usable
        # booking URL — same behaviour as the web search route. Encode the
        # correct passenger COUNT: prefer exact saved ages; otherwise, when a
        # children count was stored without ages, use a placeholder age per
        # child so the link still carries the right number of kids. The
        # placeholder age is approximate (the traveler adjusts it at the
        # booking site) and only used when exact ages weren't saved.
        if not book:
            from app import kayak_link  # lazy import (avoids circular import)
            if child_ages:
                book_ages = child_ages
            elif children:
                book_ages = [DEFAULT_CHILD_AGE] * children
            else:
                book_ages = []
            book = kayak_link(origin, dest_iata, dep_date, ret_date, adults, book_ages)

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

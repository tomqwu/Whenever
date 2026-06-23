"""
scheduler.py — Standalone price-watch runner for Whenever.

Intended to be executed by cron:
    python scheduler.py

Opens the watch DB (path from WATCH_DB env, default whenever_watches.db),
runs check_all_watches against all active watches using app.get_fare, prints
a summary of any price drops, and exits 0.

Can also be imported directly for testing or scripting:
    import scheduler
    scheduler.main([])
"""

import os
import sys
from typing import Optional

from watch import WatchDB, check_all_watches
import app as appmod


def add_watch(
    db: WatchDB,
    origin: str,
    dest_iata: str,
    dest_city: Optional[str] = None,
    dep_date: str = "",
    ret_date: str = "",
    adults: int = 2,
    children: int = 0,
    threshold_pct: float = 25.0,
) -> int:
    """Thin wrapper: add a watch to the given WatchDB. Returns the new watch id."""
    return db.add_watch(
        origin=origin,
        dest_iata=dest_iata,
        dest_city=dest_city,
        dep_date=dep_date,
        ret_date=ret_date,
        adults=adults,
        children=children,
        threshold_pct=threshold_pct,
    )


def list_watches(db: WatchDB) -> list:
    """Thin wrapper: return all active watches from the given WatchDB."""
    return db.list_watches(active_only=True)


def remove_watch(db: WatchDB, watch_id: int) -> None:
    """Thin wrapper: deactivate a watch by id."""
    db.remove_watch(watch_id)


def main(argv=None) -> int:
    """Open the DB, run all active watches, print summary, return exit code."""
    db_path = os.environ.get("WATCH_DB") or "whenever_watches.db"
    webhook_url = os.environ.get("WATCH_WEBHOOK_URL")

    db = WatchDB(db_path)
    try:
        drops = check_all_watches(db, fare_fn=appmod.get_fare, webhook_url=webhook_url)
    finally:
        db.close()

    if drops:
        print(f"[SUMMARY] {len(drops)} price drop(s) detected.")
    else:
        print("[SUMMARY] No price drops detected.")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

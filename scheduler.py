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
    last_price: Optional[int] = None,
    last_source: Optional[str] = None,
    child_ages: Optional[list] = None,
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
        last_price=last_price,
        last_source=last_source,
        child_ages=child_ages,
    )


def list_watches(db: WatchDB) -> list:
    """Thin wrapper: return all active watches from the given WatchDB."""
    return db.list_watches(active_only=True)


def remove_watch(db: WatchDB, watch_id: int) -> None:
    """Thin wrapper: deactivate a watch by id."""
    db.remove_watch(watch_id)


def main(argv=None) -> int:
    """Open the DB, run all active watches, print summary, return exit code.

    Refuses to run while DEMO_MODE is on (#44): in demo mode app.get_fare returns
    clearly-labeled SAMPLE fares, and the scheduler PERSISTS each re-priced fare
    into WATCH_DB — so running it in demo mode would leak demo prices into the
    real watch database and emit bogus drop alerts. The price-watch checker is a
    real-pricing background job; it has no business running on sample data, so we
    refuse loudly rather than poison the DB.
    """
    if appmod.DEMO_MODE:
        print("[ERROR] DEMO_MODE is on — refusing to run the price-watch checker on "
              "sample fares (it would persist demo prices into the watch DB). "
              "Unset DEMO_MODE to check real prices.")
        return 0

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

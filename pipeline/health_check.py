"""
Pipeline health check + light housekeeping.

Two jobs:
  1. Data-quality guardrails — answer "did the pipeline actually work, and is
     the data sane?" without spending any Keepa tokens. Pure local SQL.
  2. Housekeeping — prune orphaned seller rows (sellers that no longer appear
     in any retained history event).

Run modes:
  (no args)        : print a markdown health report, exit 0 if healthy else 1
  --json           : print the report as JSON instead of markdown
  --clean          : also delete UNNAMED orphan sellers (safe — preserves rows
                     whose name we already paid a token for)
  --clean-all      : delete ALL orphan sellers, named or not (reclaims rows but
                     a seller that reappears later costs 1 token to re-resolve)
  --vacuum         : run VACUUM after cleanup to shrink the DB file

Importable:
  from health_check import run_checks, format_markdown
  checks = run_checks(conn)          # list[Check]
  md = format_markdown(checks)       # str

Exit code is 1 if any check has status "fail", else 0 — usable in cron/CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(HERE / "amazon_tracker.db"))

# How fresh the daily CSV snapshot should be, and how many missing ASINs we
# tolerate before flagging. Tune to your run cadence.
CSV_FRESH_DAYS = 2
CSV_MISSING_WARN_PCT = 5.0

# Must match HISTORY_RETENTION_DAYS in keepa_api_offers.py — the collector skips
# events older than this at insert time, and --purge deletes rows that aged past
# it (insert-time skipping alone never shrinks already-stored rows).
RETENTION_DAYS = int(os.environ.get("KEEPA_RETENTION_DAYS", 90))


@dataclass
class Check:
    label: str
    status: str   # "ok" | "warn" | "fail"
    detail: str
    count: int = 0


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ── Individual checks ───────────────────────────────────────────────────────
def check_csv_freshness(conn) -> Check:
    """Are recent daily snapshots present for (most of) the catalog?"""
    total = _scalar(conn, "SELECT COUNT(*) FROM dim_product") or 0
    if not total:
        return Check("Daily CSV freshness", "warn", "Catalog is empty (dim_product has 0 rows).")
    missing = _scalar(
        conn,
        f"""SELECT COUNT(*) FROM dim_product
            WHERE asin NOT IN (
                SELECT asin FROM fct_keepa_daily
                WHERE snapshot_date >= date('now', '-{CSV_FRESH_DAYS} days')
            )""",
    ) or 0
    pct = 100.0 * missing / total
    detail = (f"{missing:,}/{total:,} ASINs ({pct:.1f}%) have no fct_keepa_daily "
              f"row in the last {CSV_FRESH_DAYS} days.")
    if missing == 0:
        return Check("Daily CSV freshness", "ok", "All catalog ASINs have a recent snapshot.")
    status = "warn" if pct <= CSV_MISSING_WARN_PCT else "fail"
    return Check("Daily CSV freshness", status, detail, missing)


def check_tick_failures(conn) -> Check:
    """Did any per-seller API fetches fail in the last 24h?"""
    if not _table_exists(conn, "asin_api_state"):
        return Check("API tick failures", "ok", "No asin_api_state table yet.")
    rows = conn.execute(
        """SELECT asin, error_msg FROM asin_api_state
           WHERE fetch_success = 0
             AND last_attempted_at >= datetime('now', '-1 day')
           ORDER BY last_attempted_at DESC LIMIT 10"""
    ).fetchall()
    n = _scalar(
        conn,
        """SELECT COUNT(*) FROM asin_api_state
           WHERE fetch_success = 0 AND last_attempted_at >= datetime('now','-1 day')""",
    ) or 0
    if n == 0:
        return Check("API tick failures", "ok", "No failed fetches in the last 24h.")
    sample = "; ".join(f"{r['asin']}: {(r['error_msg'] or '')[:40]}" for r in rows[:3])
    return Check("API tick failures", "fail",
                 f"{n} ASIN(s) failed to fetch in last 24h. e.g. {sample}", n)


def check_backfill_progress(conn) -> Check:
    """How far through the first per-seller pass are we? (informational)"""
    if not _table_exists(conn, "asin_api_state"):
        return Check("Per-seller backfill", "warn", "No asin_api_state table yet.")
    total = _scalar(conn, "SELECT COUNT(*) FROM dim_product") or 0
    fetched = _scalar(
        conn, "SELECT COUNT(*) FROM asin_api_state WHERE last_fetched_at IS NOT NULL"
    ) or 0
    never = total - fetched
    pct = 100.0 * fetched / total if total else 0
    detail = f"{fetched:,}/{total:,} ASINs fetched ({pct:.0f}%); {never:,} never fetched."
    status = "ok" if never == 0 else "warn"
    return Check("Per-seller backfill", status, detail, never)


def check_negative_stock(conn) -> Check:
    """Sanity: stock should never be negative after parsing (-1 → 0 mapping)."""
    if not _table_exists(conn, "fct_keepa_seller_history"):
        return Check("Stock sanity", "ok", "No seller-history table yet.")
    n = _scalar(conn, "SELECT COUNT(*) FROM fct_keepa_seller_history WHERE stock < 0") or 0
    if n == 0:
        return Check("Stock sanity", "ok", "No negative stock values.")
    return Check("Stock sanity", "fail", f"{n} rows have stock < 0 — parser regression?", n)


def check_unnamed_active_sellers(conn) -> Check:
    """Active sellers (events in 90d) that still have no name — tick should
    auto-resolve these, so a non-zero count means name resolution is lagging."""
    if not _table_exists(conn, "dim_keepa_seller"):
        return Check("Seller names", "ok", "No seller dim yet.")
    n = _scalar(
        conn,
        """SELECT COUNT(*) FROM dim_keepa_seller d
           WHERE (d.seller_name IS NULL OR d.seller_name = '')
             AND EXISTS (SELECT 1 FROM fct_keepa_seller_history h
                         WHERE h.seller_id = d.seller_id
                           AND h.change_time >= datetime('now','-90 days'))""",
    ) or 0
    if n == 0:
        return Check("Seller names", "ok", "All active sellers have names.")
    return Check("Seller names", "warn",
                 f"{n} active seller(s) unnamed — run `keepa_api_offers.py --seller-names`.", n)


def check_orphan_sellers(conn) -> Check:
    """Seller rows with no remaining history events (housekeeping target)."""
    if not _table_exists(conn, "dim_keepa_seller"):
        return Check("Orphan sellers", "ok", "No seller dim yet.")
    n = _scalar(
        conn,
        """SELECT COUNT(*) FROM dim_keepa_seller d
           WHERE NOT EXISTS (SELECT 1 FROM fct_keepa_seller_history h
                             WHERE h.seller_id = d.seller_id)""",
    ) or 0
    if n == 0:
        return Check("Orphan sellers", "ok", "No orphaned seller rows.")
    return Check("Orphan sellers", "warn",
                 f"{n} seller row(s) have no history events — run with --clean to prune.", n)


def run_checks(conn) -> list[Check]:
    return [
        check_csv_freshness(conn),
        check_tick_failures(conn),
        check_backfill_progress(conn),
        check_negative_stock(conn),
        check_unnamed_active_sellers(conn),
        check_orphan_sellers(conn),
    ]


# ── Housekeeping ────────────────────────────────────────────────────────────
def purge_old_history(conn, retention_days: int = RETENTION_DAYS) -> int:
    """Delete seller-history events older than the retention window.

    The collector skips old events at INSERT, but rows stored on an earlier run
    still age out — without this the table grows unbounded. Returns rows deleted.
    Backed by idx_seller_hist_time(change_time) so it stays cheap at 5K ASINs.
    """
    if not _table_exists(conn, "fct_keepa_seller_history"):
        return 0
    cur = conn.execute(
        "DELETE FROM fct_keepa_seller_history "
        "WHERE change_time < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    conn.commit()
    return cur.rowcount


def clean_orphan_sellers(conn, include_named: bool = False) -> int:
    """Delete dim_keepa_seller rows with no history events.

    By default keeps rows that already have a name (we paid a token for those —
    keeping the row avoids re-paying if the seller reappears). include_named=True
    deletes every orphan regardless.
    """
    name_guard = "" if include_named else \
        "AND (d.seller_name IS NULL OR d.seller_name = '')"
    cur = conn.execute(
        f"""DELETE FROM dim_keepa_seller AS d
            WHERE NOT EXISTS (SELECT 1 FROM fct_keepa_seller_history h
                              WHERE h.seller_id = d.seller_id)
              {name_guard}"""
    )
    conn.commit()
    return cur.rowcount


# ── Formatting ──────────────────────────────────────────────────────────────
_ICON = {"ok": "✅", "warn": "⚠️", "fail": "🔴"}


def format_markdown(checks: list[Check]) -> str:
    worst = "ok"
    for c in checks:
        if c.status == "fail":
            worst = "fail"; break
        if c.status == "warn":
            worst = "warn"
    header = {"ok": "✅ Healthy", "warn": "⚠️ Warnings", "fail": "🔴 Action needed"}[worst]
    lines = [f"**Pipeline health — {header}**", ""]
    for c in checks:
        lines.append(f"- {_ICON[c.status]} **{c.label}** — {c.detail}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pipeline health check + housekeeping")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    ap.add_argument("--purge", action="store_true",
                    help=f"Delete seller-history events older than {RETENTION_DAYS} days")
    ap.add_argument("--clean", action="store_true",
                    help="Delete UNNAMED orphan sellers (keeps paid-for named rows)")
    ap.add_argument("--clean-all", action="store_true",
                    help="Delete ALL orphan sellers (named too)")
    ap.add_argument("--vacuum", action="store_true", help="VACUUM after cleanup")
    args = ap.parse_args()

    conn = get_conn()
    try:
        if args.purge:
            n = purge_old_history(conn)
            print(f"Purged {n} seller-history event(s) older than {RETENTION_DAYS} days.")
        if args.clean or args.clean_all:
            deleted = clean_orphan_sellers(conn, include_named=args.clean_all)
            print(f"Pruned {deleted} orphan seller row(s)"
                  f"{' (including named)' if args.clean_all else ' (unnamed only)'}.")
        if args.vacuum:
            conn.execute("VACUUM")
            print("VACUUM complete.")

        checks = run_checks(conn)
        if args.json:
            print(json.dumps([asdict(c) for c in checks], indent=2))
        else:
            print(format_markdown(checks))
        return 1 if any(c.status == "fail" for c in checks) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

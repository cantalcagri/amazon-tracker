"""
Migration 002 — rich CSV columns + API token-usage log.

Idempotent and non-destructive. Adds the high-value columns the free Keepa
Product Viewer CSV can fill (product codes for Costco linking, fees, ratings,
sales signals) plus the api_token_log table.

    python schema/migrations/002_csv_rich_columns_and_token_log.py [DB_PATH]

Adds (only when missing):
    dim_product:    upc, ean, part_number, weight_g, referral_fee_pct, fba_pick_pack_fee
    fct_keepa_daily: new_offer_count, rating, rating_count, bought_past_month,
                     monthly_sold_peak, pct_amazon_30d, pct_amazon_90d, return_rate
Then re-applies schema.sql (creates api_token_log + new indexes, refreshes views).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "schema" / "schema.sql"

ADDITIONS: dict[str, list[tuple[str, str]]] = {
    "dim_product": [
        ("upc",               "TEXT"),
        ("ean",               "TEXT"),
        ("part_number",       "TEXT"),
        ("weight_g",          "REAL"),
        ("referral_fee_pct",  "REAL"),
        ("fba_pick_pack_fee", "REAL"),
    ],
    "fct_keepa_daily": [
        ("new_offer_count",   "INTEGER"),
        ("rating",            "REAL"),
        ("rating_count",      "INTEGER"),
        ("bought_past_month", "INTEGER"),
        ("monthly_sold_peak", "INTEGER"),
        ("pct_amazon_30d",    "REAL"),
        ("pct_amazon_90d",    "REAL"),
        ("return_rate",       "REAL"),
    ],
}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        added = 0
        for table, cols in ADDITIONS.items():
            if not _table_exists(conn, table):
                continue
            existing = _columns(conn, table)
            for name, decl in cols:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    print(f"  + {table}.{name}")
                    added += 1
        conn.commit()
        conn.executescript(SCHEMA.read_text())
        conn.commit()
        print(f"Migration 002 complete ({added} column(s) added).")
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "DB_PATH", str(REPO / "pipeline" / "amazon_tracker.db"))
    print(f"Migrating {db}")
    migrate(db)

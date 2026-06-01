"""
Migration 001 — marketplace/item identity + offer-truncation flags.

Idempotent and non-destructive. Run on an existing amazon_tracker.db that was
created before these columns existed. A fresh DB built from schema.sql already
has them, so this is a no-op there too.

    python schema/migrations/001_marketplace_and_offer_flags.py [DB_PATH]
    # or honor the DB_PATH env var:
    DB_PATH=pipeline/amazon_tracker.db python schema/migrations/001_*.py

Adds (only when missing):
    dim_product.marketplace  ('amazon_us' default), .gtin, .item_uid
    asin_api_state.offers_successful, .offers_truncated
Then re-applies schema.sql (creates the new indexes + refreshes views).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "schema" / "schema.sql"

# table -> [(column, column_decl), ...]
ADDITIONS: dict[str, list[tuple[str, str]]] = {
    "dim_product": [
        ("marketplace", "TEXT NOT NULL DEFAULT 'amazon_us'"),
        ("gtin",        "TEXT"),
        ("item_uid",    "TEXT"),
    ],
    "asin_api_state": [
        ("offers_successful", "INTEGER"),
        ("offers_truncated",  "INTEGER"),
    ],
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        added = 0
        for table, cols in ADDITIONS.items():
            if not _table_exists(conn, table):
                continue  # schema.sql (applied below) will create it fresh
            existing = _columns(conn, table)
            for name, decl in cols:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    print(f"  + {table}.{name}")
                    added += 1
        conn.commit()
        # Re-apply schema.sql for new indexes + view refresh (all non-destructive).
        conn.executescript(SCHEMA.read_text())
        conn.commit()
        print(f"Migration 001 complete ({added} column(s) added).")
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "DB_PATH", str(REPO / "pipeline" / "amazon_tracker.db"))
    print(f"Migrating {db}")
    migrate(db)

"""
Shared database utilities for Amazon Tracker.
Imported by both browser_collector.py and keepa_api_collector.py.
"""

import os
import sqlite3
from pathlib import Path

DB_PATH        = os.getenv("DB_PATH", "amazon_tracker.db")
RETENTION_DAYS = 10


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema_path = Path(__file__).parent.parent / "schema" / "schema.sql"
    with open(schema_path) as f:
        sql = f.read()
    with get_conn() as conn:
        conn.executescript(sql)


def upsert_product(conn, asin, title=None, brand=None,
                   parent_asin=None, variation_size=None, variation_color=None):
    conn.execute("""
        INSERT INTO dim_product (asin, parent_asin, title, brand, variation_size, variation_color)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asin) DO UPDATE SET
            title           = COALESCE(excluded.title, title),
            brand           = COALESCE(excluded.brand, brand),
            parent_asin     = COALESCE(excluded.parent_asin, parent_asin),
            variation_size  = COALESCE(excluded.variation_size, variation_size),
            variation_color = COALESCE(excluded.variation_color, variation_color)
    """, (asin, parent_asin, title, brand, variation_size, variation_color))


def purge_old_snapshots(conn):
    conn.execute(f"""
        DELETE FROM fct_asin_daily
        WHERE snapshot_date < date('now', '-{RETENTION_DAYS} days')
    """)
    conn.commit()

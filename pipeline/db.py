"""
Shared database utilities for Amazon Tracker.
Imported by keepa_csv_importer.py (get_conn, init_db, upsert_product).
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "amazon_tracker.db")


def get_conn() -> sqlite3.Connection:
    # WAL lets the dashboards read while the collector writes (no more "database
    # is locked"). busy_timeout makes a blocked write wait instead of throwing.
    # REQUIRES a local APFS DB path — WAL fails on iCloud/Dropbox/network mounts
    # (that, not WAL itself, was the old "disk I/O error").
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema_path = Path(__file__).parent.parent / "schema" / "schema.sql"
    with open(schema_path) as f:
        sql = f.read()
    with get_conn() as conn:
        conn.executescript(sql)


def upsert_product(conn, asin, title=None, brand=None,
                   parent_asin=None, variation_size=None, variation_color=None,
                   image_url=None, gtin=None, upc=None, ean=None,
                   part_number=None, weight_g=None, referral_fee_pct=None,
                   fba_pick_pack_fee=None):
    """Upsert a product. All fields are COALESCE-merged, so a NULL never wipes an
    existing value — the CSV and API can each fill in whatever they know."""
    conn.execute("""
        INSERT INTO dim_product (asin, parent_asin, title, brand,
                                  variation_size, variation_color, image_url,
                                  gtin, upc, ean, part_number, weight_g,
                                  referral_fee_pct, fba_pick_pack_fee)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asin) DO UPDATE SET
            title           = COALESCE(excluded.title, title),
            brand           = COALESCE(excluded.brand, brand),
            parent_asin     = COALESCE(excluded.parent_asin, parent_asin),
            variation_size  = COALESCE(excluded.variation_size, variation_size),
            variation_color = COALESCE(excluded.variation_color, variation_color),
            image_url       = COALESCE(excluded.image_url, image_url),
            gtin            = COALESCE(excluded.gtin, gtin),
            upc             = COALESCE(excluded.upc, upc),
            ean             = COALESCE(excluded.ean, ean),
            part_number     = COALESCE(excluded.part_number, part_number),
            weight_g        = COALESCE(excluded.weight_g, weight_g),
            referral_fee_pct  = COALESCE(excluded.referral_fee_pct, referral_fee_pct),
            fba_pick_pack_fee = COALESCE(excluded.fba_pick_pack_fee, fba_pick_pack_fee)
    """, (asin, parent_asin, title, brand, variation_size, variation_color,
          image_url, gtin, upc, ean, part_number, weight_g,
          referral_fee_pct, fba_pick_pack_fee))

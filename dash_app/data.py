"""
DuckDB-backed data access for the Dash app.

The operational pipeline keeps writing the SQLite file unchanged. DuckDB
attaches that file READ-ONLY (`sqlite` extension) and acts as the analytical
engine — zero ETL, zero copy. Every query here runs through DuckDB over the
live SQLite tables/views.

Why DuckDB and not sqlite3 directly: columnar/vectorized execution, richer SQL
(FULL OUTER JOIN, qualify, list/struct fns), and a clean swap-path to a native
.duckdb warehouse or MotherDuck later without touching call sites.
"""

from __future__ import annotations

import os
import threading
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(HERE.parent / "pipeline" / "amazon_tracker.db"))

_LOCK = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None


def _conn() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        c = duckdb.connect(database=":memory:")
        c.execute("INSTALL sqlite; LOAD sqlite;")
        c.execute(f"ATTACH '{DB_PATH}' AS src (TYPE sqlite, READ_ONLY)")
        _con = c
    return _con


def q(sql: str, params: list | None = None) -> pd.DataFrame:
    """Run a query through DuckDB over the attached SQLite DB. Thread-safe."""
    with _LOCK:
        return _conn().execute(sql, params or []).df()


def _cutoff(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ── Catalog / selectors ─────────────────────────────────────────────────────
def brands() -> pd.DataFrame:
    return q(
        """SELECT COALESCE(d.brand, '(no brand)') AS brand,
                  COUNT(DISTINCT d.asin) AS asin_count
           FROM src.dim_product d
           JOIN (SELECT DISTINCT asin FROM src.fct_keepa_daily) k ON k.asin = d.asin
           GROUP BY 1 ORDER BY 1"""
    )


def asins_for_brand(brand: str | None) -> pd.DataFrame:
    base = """
        SELECT k.asin,
               COALESCE(d.title, k.asin) AS title,
               COALESCE(sc.sellers, 0)   AS sellers
        FROM (SELECT DISTINCT asin FROM src.fct_keepa_daily) k
        LEFT JOIN src.dim_product d ON d.asin = k.asin
        LEFT JOIN (
            SELECT asin, COUNT(DISTINCT seller_id) AS sellers
            FROM src.fct_keepa_seller_history
            WHERE stock IS NOT NULL GROUP BY asin
        ) sc ON sc.asin = k.asin
        {where}
        ORDER BY sellers DESC, title
    """
    if not brand or brand == "__all__":
        return q(base.format(where=""))
    return q(base.format(where="WHERE COALESCE(d.brand,'(no brand)') = ?"), [brand])


def product(asin: str) -> dict:
    df = q("SELECT * FROM src.dim_product WHERE asin = ?", [asin])
    return df.iloc[0].to_dict() if not df.empty else {}


# ── Trend / KPIs ────────────────────────────────────────────────────────────
def trend(asin: str, days: int) -> pd.DataFrame:
    return q(
        """SELECT snapshot_date AS Date,
                  sales_rank_current AS bsr,
                  buy_box_price AS bb_price,
                  fba_price, fbm_price,
                  fba_stock, buy_box_stock AS bb_stock,
                  total_offers, fba_offers, fbm_offers,
                  oos_90d_pct AS oos_90d,
                  monthly_sold_num AS monthly_sold
           FROM src.fct_keepa_daily
           WHERE asin = ? AND snapshot_date >= ?
           ORDER BY snapshot_date""",
        [asin, _cutoff(days)],
    )


def latest_kpis(asin: str) -> dict:
    df = q(
        """SELECT sales_rank_current AS bsr, buy_box_price AS bb_price,
                  fba_stock, total_offers, fba_offers
           FROM src.fct_keepa_daily
           WHERE asin = ?
           ORDER BY snapshot_date DESC LIMIT 1""",
        [asin],
    )
    return df.iloc[0].to_dict() if not df.empty else {}


# ── Canonical daily units sold (per-seller → FBA-delta fallback) ────────────
def daily_sales(asin: str, days: int) -> tuple[pd.DataFrame, str]:
    ps = q(
        """SELECT sale_date AS Date, units_sold, units_restocked
           FROM src.v_asin_daily_sales
           WHERE asin = ? AND sale_date >= ?
           ORDER BY sale_date""",
        [asin, _cutoff(days)],
    )
    if not ps.empty:
        return ps, "per-seller"
    fb = q(
        """SELECT snapshot_date AS Date, units_sold, 0 AS units_restocked
           FROM src.v_daily_sales
           WHERE asin = ? AND snapshot_date >= ? AND units_sold IS NOT NULL
           ORDER BY snapshot_date""",
        [asin, _cutoff(days)],
    )
    return fb, "fba-delta"


# ── Per-seller stock over time (one series per seller) ──────────────────────
def seller_stock(asin: str, days: int) -> pd.DataFrame:
    return q(
        """SELECT h.change_time AS Date,
                  COALESCE(s.seller_name, h.seller_id) AS seller,
                  h.stock
           FROM src.fct_keepa_seller_history h
           LEFT JOIN src.dim_keepa_seller s ON s.seller_id = h.seller_id
           WHERE h.asin = ? AND h.stock IS NOT NULL AND DATE(h.change_time) >= ?
           ORDER BY h.change_time""",
        [asin, _cutoff(days)],
    )


def has_seller_data(asin: str) -> bool:
    df = q("SELECT 1 FROM src.fct_keepa_seller_history WHERE asin = ? LIMIT 1", [asin])
    return not df.empty

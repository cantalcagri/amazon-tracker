"""
Replenishment recommendation engine.

For each tracked ASIN, computes:
  - 7/14/30-day sales velocity (units/day) from FBA stock deltas in v_daily_sales
  - BSR & price trends over the same window
  - Current FBA stock + days-of-supply
  - Competition signal (% top seller, 30d)

Then assigns a recommendation class:
  SHIP_NOW   — low FBA stock, fast mover, decent BSR
  HOLD       — too much stock already, OR rank too poor, OR buy-box locked
  AVOID_BUY  — slow seller AND poor rank AND locked competition (don't buy more)
  WATCH      — need more days of data / unclear

Returns a DataFrame with one row per ASIN containing the metrics + the call.
The classification thresholds are passed in via a dict so they can be tuned
from the dashboard sidebar without editing code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd


@dataclass
class Thresholds:
    """Tunable knobs — surfaced as sidebar sliders in the dashboard."""
    target_days_of_supply: int = 30      # days of FBA supply we want to maintain
    ship_when_days_below:  int = 21      # ship if days_of_supply falls below this
    hold_when_days_above:  int = 60      # hold if days_of_supply already above this
    min_velocity_to_ship:  float = 0.5   # don't ship if avg < this/day (slow mover)
    bad_velocity:          float = 0.3   # avoid-buy threshold (very slow)
    bad_bsr:               int = 500_000 # BSR worse than this = poor seller
    locked_buybox_pct:     float = 80.0  # top_seller_30d above this = monopoly
    velocity_window_days:  int = 14      # window for velocity / trend calculations


def _safe_div(a, b):
    return a / b if (a is not None and b not in (None, 0)) else None


def compute(conn: sqlite3.Connection, thresholds: Thresholds | None = None) -> pd.DataFrame:
    """Compute the recommendation table. Returns one row per ASIN.

    Columns:
      asin, title, brand, image_url, fba_stock, buy_box_price, bsr,
      velocity_avg, days_of_supply, bsr_trend, price_trend,
      top_seller_30d, recommendation, reasons (list[str]), score
    """
    th = thresholds or Thresholds()

    # 1. Most recent snapshot's state per ASIN
    latest = pd.read_sql_query(
        """
        WITH last AS (
            SELECT MAX(snapshot_date) AS d FROM fct_keepa_daily
        )
        SELECT
            k.asin,
            d.title,
            d.brand,
            d.image_url,
            k.snapshot_date           AS as_of,
            k.sales_rank_current      AS bsr,
            k.sales_rank_30d_avg      AS bsr_30d,
            k.buy_box_price,
            k.buy_box_stock,
            k.fba_stock,
            k.fba_offers,
            k.fbm_offers,
            k.total_offers,
            k.pct_top_seller_30d      AS top_seller_30d,
            k.pct_top_seller_90d      AS top_seller_90d,
            k.oos_90d_pct,
            k.monthly_sold_num
        FROM fct_keepa_daily k
        LEFT JOIN dim_product d ON d.asin = k.asin
        WHERE k.snapshot_date = (SELECT d FROM last)
        """,
        conn,
    )

    if latest.empty:
        return latest

    # 2. Velocity over the last N days (avg of non-null units_sold_per_day)
    vel = pd.read_sql_query(
        """
        SELECT
            asin,
            AVG(units_sold_per_day) AS velocity_avg,
            SUM(units_sold)         AS units_sold_total,
            COUNT(units_sold_per_day) AS measured_days,
            MIN(snapshot_date)      AS first_measurable,
            MAX(snapshot_date)      AS last_measurable
        FROM v_daily_sales
        WHERE units_sold_per_day IS NOT NULL
          AND snapshot_date >= date('now', ?)
        GROUP BY asin
        """,
        conn,
        params=(f"-{th.velocity_window_days} days",),
    )

    # 3. BSR & price slope over the same window — simple (last - first) / N
    trend = pd.read_sql_query(
        """
        WITH win AS (
            SELECT asin, snapshot_date, sales_rank_current, buy_box_price
            FROM fct_keepa_daily
            WHERE snapshot_date >= date('now', ?)
        ),
        agg AS (
            SELECT
                asin,
                COUNT(*) AS pts,
                MIN(snapshot_date) AS d_first,
                MAX(snapshot_date) AS d_last
            FROM win GROUP BY asin
        )
        SELECT
            a.asin,
            w_last.sales_rank_current  - w_first.sales_rank_current AS bsr_change,
            w_last.buy_box_price       - w_first.buy_box_price      AS price_change,
            a.pts
        FROM agg a
        LEFT JOIN win w_first ON w_first.asin = a.asin AND w_first.snapshot_date = a.d_first
        LEFT JOIN win w_last  ON w_last.asin  = a.asin AND w_last.snapshot_date  = a.d_last
        """,
        conn,
        params=(f"-{th.velocity_window_days} days",),
    )

    df = latest.merge(vel, on="asin", how="left").merge(trend, on="asin", how="left")

    # Fallback velocity from Keepa Monthly Sold if we lack derived data
    df["velocity_fallback"] = df["monthly_sold_num"] / 30.0
    df["velocity_used"] = df["velocity_avg"].fillna(df["velocity_fallback"])
    df["velocity_source"] = df["velocity_avg"].notna().map({True: "FBA-delta", False: "Keepa monthly"})
    df.loc[df["velocity_used"].isna(), "velocity_source"] = "—"

    # Days of supply
    df["days_of_supply"] = df.apply(
        lambda r: _safe_div(r["fba_stock"], r["velocity_used"])
        if pd.notna(r["fba_stock"]) and r["velocity_used"] and r["velocity_used"] > 0
        else None,
        axis=1,
    )

    # 4. Classification
    def classify(r):
        reasons: list[str] = []
        v   = r["velocity_used"]
        dos = r["days_of_supply"]
        bsr = r["bsr"]
        top = r["top_seller_30d"]
        stock = r["fba_stock"]

        # If we have no velocity signal at all → WATCH (need more data)
        if pd.isna(v) or v is None:
            reasons.append("Velocity unknown — need more days of data")
            return "WATCH", reasons, 40

        # AVOID-BUY: slow + poor rank + locked competition (all must be true)
        if (
            v < th.bad_velocity
            and pd.notna(bsr) and bsr > th.bad_bsr
            and pd.notna(top) and top > th.locked_buybox_pct - 10
        ):
            reasons.append(f"Velocity {v:.2f}/day < {th.bad_velocity}")
            reasons.append(f"BSR {bsr:,.0f} > {th.bad_bsr:,}")
            reasons.append(f"Top seller {top:.0f}% locks buy-box")
            return "AVOID_BUY", reasons, 0

        # HARD-RULE: explicit zero FBA stock + reasonable BSR + decent velocity
        # → ship immediately
        if (pd.notna(stock) and stock == 0
            and pd.notna(bsr) and bsr < th.bad_bsr
            and v >= th.min_velocity_to_ship):
            reasons.append("FBA stock = 0 (OOS)")
            reasons.append(f"BSR {bsr:,.0f} still healthy")
            reasons.append(f"Selling {v:.2f}/day")
            return "SHIP_NOW", reasons, 100

        # HOLD: already enough stock OR can't win buy-box
        if pd.notna(dos) and dos > th.hold_when_days_above:
            reasons.append(f"Already {dos:.0f} days of FBA supply (>{th.hold_when_days_above})")
            return "HOLD", reasons, 20
        if pd.notna(top) and top > th.locked_buybox_pct:
            reasons.append(f"Top seller holds {top:.0f}% of buy-box — hard to win")
            return "HOLD", reasons, 15

        # SHIP_NOW: low DoS + decent velocity
        if pd.notna(dos) and dos < th.ship_when_days_below and v >= th.min_velocity_to_ship:
            reasons.append(f"Only {dos:.0f} days of supply (target {th.target_days_of_supply})")
            reasons.append(f"Selling {v:.2f}/day")
            return "SHIP_NOW", reasons, 80

        # Slow seller (velocity known but below ship threshold)
        if v < th.min_velocity_to_ship:
            reasons.append(f"Velocity {v:.2f}/day below ship threshold ({th.min_velocity_to_ship})")
            return "HOLD", reasons, 25

        # Velocity good but FBA stock unknown → can't compute DoS
        if pd.isna(stock):
            reasons.append(f"Selling {v:.2f}/day but FBA stock unknown — need Keepa data")
            return "WATCH", reasons, 50

        # Healthy stock + healthy velocity
        reasons.append(f"DoS {dos:.0f}d ≥ ship threshold ({th.ship_when_days_below})")
        return "HOLD", reasons, 30

    classes, reasons, scores = zip(*df.apply(classify, axis=1))
    df["recommendation"] = list(classes)
    df["reasons"] = list(reasons)
    df["score"] = list(scores)

    # Suggested ship quantity (without warehouse data — assumes unlimited supply):
    # the gap between target_days_of_supply and current days_of_supply, in units.
    def suggested_qty(r):
        if r["recommendation"] != "SHIP_NOW":
            return None
        v = r["velocity_used"] or 0
        stock = r["fba_stock"] or 0
        target_units = th.target_days_of_supply * v
        gap = max(0, target_units - stock)
        return int(round(gap))

    df["suggested_ship_qty"] = df.apply(suggested_qty, axis=1)

    return df[[
        "asin", "title", "brand", "image_url", "as_of",
        "bsr", "buy_box_price", "fba_stock", "fba_offers", "fbm_offers",
        "top_seller_30d", "velocity_used", "velocity_source",
        "days_of_supply", "bsr_change", "price_change",
        "recommendation", "score", "reasons", "suggested_ship_qty",
    ]]


if __name__ == "__main__":  # pragma: no cover — quick CLI smoke test
    import os
    import sys
    db = os.getenv("DB_PATH", "amazon_tracker.db")
    with sqlite3.connect(db) as conn:
        out = compute(conn)
    counts = out["recommendation"].value_counts()
    print("Recommendation breakdown:")
    print(counts.to_string())
    print()
    print("Top 5 SHIP_NOW (by score):")
    ship = out[out["recommendation"] == "SHIP_NOW"].sort_values("score", ascending=False).head(5)
    for _, r in ship.iterrows():
        print(f"  {r['asin']} | DoS={r['days_of_supply']:.0f} | v={r['velocity_used']:.2f}/d | "
              f"qty={r['suggested_ship_qty']} | {(r['title'] or '')[:55]}")
    sys.exit(0)

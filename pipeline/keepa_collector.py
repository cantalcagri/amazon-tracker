"""
Amazon Seller Tracker - Keepa API Collector
============================================
Uses Keepa API exclusively — no scraping, no bans, fully legal.

Keepa API docs: https://keepa.com/#!api/3-Request_Products

Token cost per run:
  - Product query with offers + stats = ~3-4 tokens per ASIN
  - At 5 tokens/min (Pro), you can run ~1-2 ASINs per minute safely

Usage:
    python keepa_collector.py --asin B0CV3CDPTK
    python keepa_collector.py --asins B0CV3CDPTK,B0CL5Z7VFR
    python keepa_collector.py --asins B0CV3CDPTK --scheduler --interval 6
"""

import os
import time
import json
import logging
import sqlite3
import schedule
import argparse
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("tracker.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ──────────────────────────────
# CONFIG
# ──────────────────────────────
DB_PATH        = os.getenv("DB_PATH", "amazon_tracker.db")
KEEPA_API_KEY  = os.getenv("KEEPA_API_KEY", "")
RETENTION_DAYS = 10
KEEPA_DOMAIN   = 1   # 1 = amazon.com (US)

# Keepa price is in "Keepa price units" = cents * 10
# Divide by 100 to get USD. -1 = not available.
def keepa_price(val) -> Optional[float]:
    if val is None or val == -1:
        return None
    return round(val / 100, 2)


# ──────────────────────────────
# DATABASE
# ──────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema", "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with get_conn() as conn:
        conn.executescript(sql)
    log.info("DB initialized: %s", DB_PATH)


def upsert_product(conn, asin, title=None, brand=None, category=None) -> int:
    conn.execute("""
        INSERT INTO dim_product (asin, title, brand, category)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(asin) DO UPDATE SET
            title    = COALESCE(excluded.title, title),
            brand    = COALESCE(excluded.brand, brand),
            category = COALESCE(excluded.category, category),
            updated_at = CURRENT_TIMESTAMP
    """, (asin, title, brand, category))
    return conn.execute("SELECT product_id FROM dim_product WHERE asin=?", (asin,)).fetchone()["product_id"]


def upsert_seller(conn, seller_name, fulfillment, rating=None,
                  rating_count=None, positive_pct=None, seller_url=None) -> int:
    today = date.today().isoformat()
    conn.execute("""
        INSERT INTO dim_seller
            (seller_name, fulfillment, rating, rating_count, positive_pct, seller_url, first_seen_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(seller_name, fulfillment) DO UPDATE SET
            rating       = COALESCE(excluded.rating, rating),
            rating_count = COALESCE(excluded.rating_count, rating_count),
            positive_pct = COALESCE(excluded.positive_pct, positive_pct),
            updated_at   = CURRENT_TIMESTAMP
    """, (seller_name, fulfillment, rating, rating_count, positive_pct, seller_url, today))
    return conn.execute(
        "SELECT seller_id FROM dim_seller WHERE seller_name=? AND fulfillment=?",
        (seller_name, fulfillment)
    ).fetchone()["seller_id"]


# ──────────────────────────────
# KEEPA API CLIENT
# ──────────────────────────────
class KeepaAPI:
    BASE = "https://api.keepa.com"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("KEEPA_API_KEY is not set. Add it to your .env file.")
        self.key = api_key
        self.session = requests.Session()

    def check_tokens(self) -> dict:
        """Check remaining token balance before querying"""
        r = self.session.get(
            f"{self.BASE}/token",
            params={"key": self.key},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        log.info("Keepa tokens: %d available | refill: %d/min",
                 data.get("tokensLeft", 0), data.get("refillRate", 0))
        return data

    def get_product(self, asin: str) -> Optional[dict]:
        """
        Fetch product with full offer list and stats.
        Costs ~3-4 tokens per call.
        
        Parameters:
          offers=20     → fetch up to 20 current marketplace offers (sellers)
          stats=1       → include sales rank stats
          history=1     → include price/rank history arrays
          rating=1      → include review/rating data
        """
        params = {
            "key":     self.key,
            "domain":  KEEPA_DOMAIN,
            "asin":    asin,
            "offers":  20,      # number of marketplace offers to return
            "stats":   1,       # sales statistics
            "history": 1,       # full price/BSR history arrays
            "rating":  1,       # seller rating data
            "buybox":  1,       # buy box data
        }
        try:
            r = self.session.get(f"{self.BASE}/product", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()

            tokens_left = data.get("tokensLeft", "?")
            log.info("Keepa tokens remaining after query: %s", tokens_left)

            products = data.get("products", [])
            if not products:
                log.warning("No product data returned for ASIN %s", asin)
                return None
            return products[0]

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                log.warning("Token rate limit hit — waiting 60s")
                time.sleep(60)
                return self.get_product(asin)  # retry once
            log.error("Keepa HTTP error for %s: %s", asin, e)
            return None
        except Exception as e:
            log.error("Keepa API error for %s: %s", asin, e)
            return None

    def parse_product(self, p: dict) -> dict:
        """
        Extract the fields we need from Keepa's raw product object.
        Returns a clean dict ready for the DB.
        """
        result = {
            "asin":     p.get("asin"),
            "title":    p.get("title"),
            "brand":    p.get("brand"),
            "sellers":  [],
        }

        # ── BSR (Best Sellers Rank) ──
        # salesRanks is a dict: {categoryId: [time, rank, time, rank, ...]}
        # We want the most recent rank from the primary category
        sales_ranks = p.get("salesRanks", {})
        root_cat_id = str(p.get("rootCategory", ""))

        # Categories map (Keepa category IDs → names)
        categories = p.get("categories", {})
        root_cat_name = categories.get(root_cat_id, {})
        if isinstance(root_cat_name, dict):
            root_cat_name = root_cat_name.get("name", f"Cat#{root_cat_id}")

        result["bsr_category"] = root_cat_name

        # Get latest BSR from the array (every 2 elements: [time, rank, time, rank...])
        if root_cat_id in sales_ranks:
            rank_arr = sales_ranks[root_cat_id]
            if rank_arr and len(rank_arr) >= 2:
                result["bsr_rank"] = rank_arr[-1]  # last value in the array

        # Also check stats for current rank
        stats = p.get("stats", {})
        if stats:
            current = stats.get("current", [])
            # current is an array indexed by Keepa's CSV type:
            # index 3 = Amazon price, index 9 = sales rank
            if current and len(current) > 9 and current[9] != -1:
                result["bsr_rank"] = current[9]

        # ── Offers / Sellers ──
        offers = p.get("offers", [])
        for offer in offers:
            # Only process NEW condition offers (condition 1 = New)
            if offer.get("condition", 1) != 1:
                continue

            seller = {}
            seller["seller_name"]   = offer.get("sellerId", "Unknown")
            seller["fulfillment"]   = "FBA" if offer.get("isPrime", False) else "FBM"
            seller["is_buy_box"]    = offer.get("isBuyBoxWinner", False)

            # Price: offerCSV contains [time, price, time, price, ...]
            # We want the most recent price
            offer_csv = offer.get("offerCSV", [])
            if offer_csv and len(offer_csv) >= 2:
                latest_price = offer_csv[-1]  # last value
                seller["price"] = keepa_price(latest_price)

            # Stock: stockCSV contains [time, stock, ...]
            stock_csv = offer.get("stockCSV", [])
            if stock_csv and len(stock_csv) >= 2:
                latest_stock = stock_csv[-1]
                seller["inventory"] = latest_stock if latest_stock != -1 else None

            # Seller rating from sellerObject if available
            seller_info = offer.get("sellerObject", {})
            if seller_info:
                seller["rating"]       = seller_info.get("rating")
                seller["rating_count"] = seller_info.get("ratingCount")
                seller["positive_pct"] = seller_info.get("ratingPercentage")

            # Skip offers with no price
            if seller.get("price") is None:
                continue

            result["sellers"].append(seller)

        log.info("Parsed product %s: BSR=%s, %d sellers",
                 result["asin"], result.get("bsr_rank"), len(result["sellers"]))
        return result


# ──────────────────────────────
# UNITS SOLD CALCULATOR
# ──────────────────────────────
def calculate_units_sold(conn, product_id: int, today: str):
    """
    Core sales estimation logic:
    
    Day N vs Day N-1 per seller:
      - Seller in both days → sold = max(0, inv_yesterday - inv_today)
      - Seller in yesterday, gone today → sold = inv_yesterday (all sold / OOS)
      - Seller NEW today → is_new_seller=1, NOT counted in sales
    
    This gives us a MINIMUM units sold estimate.
    """
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    yesterday_rows = conn.execute("""
        SELECT seller_id, inventory FROM fact_seller_snapshot
        WHERE product_id=? AND snapshot_date=?
    """, (product_id, yesterday)).fetchall()

    today_rows = conn.execute("""
        SELECT seller_id, inventory FROM fact_seller_snapshot
        WHERE product_id=? AND snapshot_date=?
    """, (product_id, today)).fetchall()

    if not yesterday_rows:
        log.info("No yesterday data for product_id=%d — skipping sales calc (need 2 days)", product_id)
        return

    yesterday_map = {r["seller_id"]: r["inventory"] for r in yesterday_rows}
    today_map     = {r["seller_id"]: r["inventory"] for r in today_rows}
    all_sellers   = set(yesterday_map) | set(today_map)

    total_sold = 0
    for sid in all_sellers:
        inv_y = yesterday_map.get(sid)
        inv_t = today_map.get(sid)
        is_new = 1 if sid not in yesterday_map else 0

        if is_new:
            units_sold, oos_sold = 0, 0
        elif inv_t is None:
            # Gone today → assume all remaining were sold
            units_sold = inv_y or 0
            oos_sold   = 1
        else:
            units_sold = max(0, (inv_y or 0) - (inv_t or 0))
            oos_sold   = 0

        if not is_new:
            total_sold += units_sold

        fulfillment_row = conn.execute(
            "SELECT fulfillment FROM dim_seller WHERE seller_id=?", (sid,)
        ).fetchone()
        fulfillment = fulfillment_row["fulfillment"] if fulfillment_row else "FBM"

        conn.execute("""
            INSERT OR REPLACE INTO fact_daily_units_sold
                (calc_date, product_id, seller_id, fulfillment,
                 inv_yesterday, inv_today, units_sold, oos_sold, is_new_seller)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (yesterday, product_id, sid, fulfillment,
              inv_y, inv_t, units_sold, oos_sold, is_new))

    log.info("Sales calc complete: product_id=%d date=%s → %d units sold", product_id, yesterday, total_sold)


def update_agg(conn, product_id: int, today: str, bsr_rank: int, bsr_category: str):
    """Update the daily summary aggregate row"""
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    prev = conn.execute("""
        SELECT bsr_rank FROM fact_bsr_snapshot
        WHERE product_id=? AND snapshot_date=?
        ORDER BY snapshot_hour DESC LIMIT 1
    """, (product_id, yesterday)).fetchone()
    prev_bsr = prev["bsr_rank"] if prev else None

    price_row = conn.execute("""
        SELECT
            SUM(f.price * COALESCE(f.inventory, 1)) /
                NULLIF(SUM(COALESCE(f.inventory, 1)), 0)  AS wavg,
            MIN(f.price)  AS min_p,
            MAX(f.price)  AS max_p,
            COUNT(*)      AS total_sellers,
            SUM(CASE WHEN s.fulfillment='FBA' THEN 1 ELSE 0 END) AS fba_cnt,
            SUM(CASE WHEN s.fulfillment='FBM' THEN 1 ELSE 0 END) AS fbm_cnt
        FROM fact_seller_snapshot f
        JOIN dim_seller s ON f.seller_id = s.seller_id
        WHERE f.product_id=? AND f.snapshot_date=?
    """, (product_id, today)).fetchone()

    sales_row = conn.execute("""
        SELECT
            SUM(units_sold) AS total_sold,
            SUM(CASE WHEN fulfillment='FBA' THEN units_sold ELSE 0 END) AS fba_sold,
            SUM(CASE WHEN fulfillment='FBM' THEN units_sold ELSE 0 END) AS fbm_sold
        FROM fact_daily_units_sold
        WHERE product_id=? AND calc_date=? AND is_new_seller=0
    """, (product_id, yesterday)).fetchone()

    conn.execute("""
        INSERT OR REPLACE INTO agg_product_daily
            (agg_date, product_id, bsr_rank, bsr_rank_prev,
             avg_price_weighted, min_price, max_price,
             total_sellers, fba_sellers, fbm_sellers,
             total_units_sold, fba_units_sold, fbm_units_sold)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today, product_id, bsr_rank, prev_bsr,
        price_row["wavg"], price_row["min_p"], price_row["max_p"],
        price_row["total_sellers"], price_row["fba_cnt"], price_row["fbm_cnt"],
        sales_row["total_sold"] or 0,
        sales_row["fba_sold"] or 0,
        sales_row["fbm_sold"] or 0
    ))


def purge_old_snapshots(conn):
    deleted_s = conn.execute(
        f"DELETE FROM fact_seller_snapshot WHERE snapshot_date < date('now','-{RETENTION_DAYS} days')"
    ).rowcount
    deleted_b = conn.execute(
        f"DELETE FROM fact_bsr_snapshot WHERE snapshot_date < date('now','-{RETENTION_DAYS} days')"
    ).rowcount
    if deleted_s or deleted_b:
        log.info("Purged %d seller + %d BSR snapshots older than %d days",
                 deleted_s, deleted_b, RETENTION_DAYS)


# ──────────────────────────────
# MAIN PIPELINE
# ──────────────────────────────
def run_pipeline(asin: str):
    today = date.today().isoformat()
    hour  = datetime.now().hour
    log.info("═══ Pipeline: %s  ASIN: %s ═══", today, asin)

    keepa = KeepaAPI(KEEPA_API_KEY)

    # Optional: check tokens first
    try:
        token_info = keepa.check_tokens()
        if token_info.get("tokensLeft", 0) < 5:
            log.warning("Low tokens! Only %d left. Skipping run.", token_info["tokensLeft"])
            return
    except Exception:
        pass  # Non-fatal

    raw = keepa.get_product(asin)
    if not raw:
        log.error("No data returned for %s", asin)
        return

    product = keepa.parse_product(raw)

    init_db()
    with get_conn() as conn:

        # 1. Upsert product dimension
        product_id = upsert_product(
            conn, asin,
            title=product.get("title"),
            brand=product.get("brand"),
            category=product.get("bsr_category")
        )

        # 2. BSR snapshot
        bsr = product.get("bsr_rank")
        bsr_cat = product.get("bsr_category", "Unknown")
        if bsr:
            conn.execute("""
                INSERT OR REPLACE INTO fact_bsr_snapshot
                    (snapshot_date, snapshot_hour, product_id, bsr_rank, bsr_category)
                VALUES (?, ?, ?, ?, ?)
            """, (today, hour, product_id, bsr, bsr_cat))
            log.info("BSR: #%d in %s", bsr, bsr_cat)

        # 3. Seller snapshots
        sellers = product.get("sellers", [])
        for s in sellers:
            seller_id = upsert_seller(
                conn,
                seller_name  = s["seller_name"],
                fulfillment  = s.get("fulfillment", "FBM"),
                rating       = s.get("rating"),
                rating_count = s.get("rating_count"),
                positive_pct = s.get("positive_pct"),
            )
            conn.execute("""
                INSERT OR REPLACE INTO fact_seller_snapshot
                    (snapshot_date, snapshot_hour, product_id, seller_id,
                     price, inventory, is_buy_box_winner, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today, hour, product_id, seller_id,
                s.get("price"),
                s.get("inventory"),
                1 if s.get("is_buy_box") else 0,
                json.dumps(s)
            ))

        log.info("Saved %d seller snapshots", len(sellers))

        # 4. Calculate units sold vs yesterday
        calculate_units_sold(conn, product_id, today)

        # 5. Update daily aggregate
        update_agg(conn, product_id, today, bsr or 0, bsr_cat)

        # 6. Purge old raw snapshots
        purge_old_snapshots(conn)

        conn.commit()

    log.info("═══ Done: %s ═══", asin)


# ──────────────────────────────
# SCHEDULER
# ──────────────────────────────
def start_scheduler(asins: list, interval_hours: int = 6):
    log.info("Scheduler started — every %dh | ASINs: %s", interval_hours, asins)

    def job():
        for asin in asins:
            try:
                run_pipeline(asin.strip())
                time.sleep(15)  # small gap between ASINs to respect rate limits
            except Exception as e:
                log.error("Pipeline error %s: %s", asin, e)

    schedule.every(interval_hours).hours.do(job)
    job()   # run immediately on start

    while True:
        schedule.run_pending()
        time.sleep(30)


# ──────────────────────────────
# CLI
# ──────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amazon Seller Tracker — Keepa API")
    parser.add_argument("--asin",      default="B0CV3CDPTK", help="Single ASIN")
    parser.add_argument("--asins",     help="Comma-separated list of ASINs")
    parser.add_argument("--scheduler", action="store_true",   help="Run on a schedule")
    parser.add_argument("--interval",  type=int, default=6,   help="Schedule interval in hours")
    args = parser.parse_args()

    asin_list = [a.strip() for a in args.asins.split(",")] if args.asins else [args.asin]

    if args.scheduler:
        start_scheduler(asin_list, interval_hours=args.interval)
    else:
        for asin in asin_list:
            run_pipeline(asin)

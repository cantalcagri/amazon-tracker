"""
Keepa API per-seller offers collector.

Each ASIN's `stockCSV` and `offerCSV` time-series (returned by Keepa's
`/product?offers=20&stock=1` endpoint) is parsed into one row per change
event in `fct_keepa_seller_history` — giving us a true per-seller stock
& price history.

Run modes:
  --tick           : take ONE batch (up to 100 stalest ASINs), exit. For cron.
  --asins X,Y,Z    : fetch specific ASINs once.
  --status         : print token balance + queue stats, no API call.
  --dry-run        : show what WOULD be fetched without spending tokens.

Cron setup (every 15 min):
  */15 * * * * cd /Users/cagri/Desktop/amazon-tracker/pipeline && \\
               /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3 \\
               keepa_api_offers.py --tick >> tick.log 2>&1

Token economics (your plan):
  - 5 tokens/min refill = 7,200/day cap
  - Balance cap = 300 (over-refill is lost)
  - ~4 tokens/ASIN with batch of 100
  - For 1K ASINs: each refreshed every ~13 hr; for 5K: every ~3 days
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# ── Config ────────────────────────────────────────────────────────────────
HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

API_KEY = os.environ.get("KEEPA_API_KEY")
DB_PATH = os.environ.get("DB_PATH", str(HERE / "amazon_tracker.db"))
KEEPA_BASE = "https://api.keepa.com"
DOMAIN = 1  # amazon.com

# Token-budget guardrails
MIN_TOKENS_TO_RUN = 150   # don't start a batch unless balance >= this
MAX_BATCH_SIZE    = 100   # Keepa's hard max per call
SAFETY_BUFFER     = 50    # keep this many tokens unspent

# Keepa epoch: their timestamps are minutes since 2011-01-01 (UTC)
KEEPA_EPOCH_MIN = 21564000  # offset to convert keepa-minute → unix-minute


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(HERE / "keepa_api.log"), logging.StreamHandler()],
)
log = logging.getLogger("keepa_api_offers")


# ── DB helpers ────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def keepa_minute_to_datetime(keepa_min: int) -> datetime:
    """Keepa timestamps are minutes since 2011-01-01 UTC."""
    unix_seconds = (keepa_min + KEEPA_EPOCH_MIN) * 60
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


# ── API ───────────────────────────────────────────────────────────────────
def get_tokens_left() -> int:
    r = requests.get(f"{KEEPA_BASE}/token", params={"key": API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()["tokensLeft"]


def fetch_products(asins: list[str]) -> dict:
    """One call for up to 100 ASINs. Returns the full Keepa JSON response."""
    params = {
        "key": API_KEY, "domain": DOMAIN,
        "asin": ",".join(asins[:MAX_BATCH_SIZE]),
        "offers": 20,   # +6 tokens/ASIN
        "stock":  1,    # +3 tokens/ASIN
        "history": 0,
    }
    log.info("API call: %d ASINs (offers=20, stock=1)", len(asins))
    t0 = time.time()
    r = requests.get(f"{KEEPA_BASE}/product", params=params, timeout=180)
    r.raise_for_status()
    elapsed = time.time() - t0
    data = r.json()
    log.info("  → HTTP %d in %.1fs · tokensConsumed=%s · tokensLeft=%s · products=%d",
             r.status_code, elapsed, data.get("tokensConsumed"),
             data.get("tokensLeft"), len(data.get("products", [])))
    return data


# ── Queue selection ───────────────────────────────────────────────────────
def pick_next_batch(conn: sqlite3.Connection, batch_size: int = MAX_BATCH_SIZE) -> list[str]:
    """
    Choose up to `batch_size` ASINs for the next API call.
    Priority order:
      1. Hot ASINs (any with SHIP_NOW or HOLD recommendation today) not fetched in last 24h
      2. ASINs never fetched
      3. ASINs with oldest last_fetched_at
    """
    # Step 1: ensure asin_api_state has a row for every dim_product
    conn.execute("""
        INSERT OR IGNORE INTO asin_api_state (asin)
        SELECT asin FROM dim_product
    """)
    conn.commit()

    # Step 2: pick — uses a single SELECT with ORDER BY that encodes priority.
    # Hot-list = ASINs flagged SHIP_NOW or HOLD in last computed recommendations.
    # We don't have a persistent recommendations table yet, so for now the
    # hot-list = ASINs with low FBA stock OR high recent velocity.
    rows = conn.execute(f"""
        SELECT s.asin,
               -- Hot priority: low FBA stock + had recent velocity = high priority
               CASE
                 WHEN (k.fba_stock IS NOT NULL AND k.fba_stock < 20)
                   OR k.fba_stock = 0
                 THEN 1 ELSE 0
               END AS is_hot,
               s.last_fetched_at
        FROM asin_api_state s
        LEFT JOIN fct_keepa_daily k
               ON k.asin = s.asin
              AND k.snapshot_date = (SELECT MAX(snapshot_date) FROM fct_keepa_daily)
        ORDER BY
          -- Hot ASINs first (only if not fetched in last 24h, otherwise lose priority)
          CASE WHEN is_hot = 1
                    AND (s.last_fetched_at IS NULL
                         OR julianday('now') - julianday(s.last_fetched_at) > 1.0)
               THEN 0 ELSE 1 END,
          -- Then never-fetched
          CASE WHEN s.last_fetched_at IS NULL THEN 0 ELSE 1 END,
          -- Then oldest
          s.last_fetched_at ASC
        LIMIT ?
    """, (batch_size,)).fetchall()
    return [r["asin"] for r in rows]


# ── Parsing & save ────────────────────────────────────────────────────────
def parse_and_save(conn: sqlite3.Connection, products: list[dict]) -> dict:
    """
    Parse Keepa /product response → upsert into:
      - fct_keepa_seller_history (stock & price change events)
      - dim_keepa_seller (seller metadata)
      - asin_api_state (fetch success bookkeeping)
    Returns counts dict for the run.
    """
    now = datetime.now(timezone.utc).isoformat()
    stats = {"asins": 0, "sellers_seen": 0, "stock_events": 0, "price_events": 0}

    for p in products:
        asin = p["asin"]
        offers = p.get("offers") or []
        live_ids = set(p.get("liveOffersOrder") or [])

        # Update fetch state for this ASIN (success path)
        conn.execute("""
            INSERT INTO asin_api_state (asin, last_fetched_at, last_attempted_at,
                                        fetch_success, error_msg, offer_count)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(asin) DO UPDATE SET
              last_fetched_at = excluded.last_fetched_at,
              last_attempted_at = excluded.last_attempted_at,
              fetch_success = excluded.fetch_success,
              error_msg = NULL,
              offer_count = excluded.offer_count
        """, (asin, now, now, 1, None, sum(1 for o in offers if o.get("offerId") in live_ids)))

        for o in offers:
            sid = o.get("sellerId")
            if not sid:
                continue
            stats["sellers_seen"] += 1

            # Upsert seller dim (we only have name in offer.sellerName when present)
            seller_name = o.get("sellerName")
            conn.execute("""
                INSERT INTO dim_keepa_seller (seller_id, seller_name, is_amazon, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(seller_id) DO UPDATE SET
                  seller_name = COALESCE(excluded.seller_name, seller_name),
                  is_amazon   = COALESCE(excluded.is_amazon, is_amazon),
                  updated_at  = excluded.updated_at
            """, (sid, seller_name, 1 if o.get("isAmazon") else 0, now))

            is_fba   = 1 if o.get("isFBA") else 0
            is_prime = 1 if o.get("isPrime") else 0

            # stockCSV format: [time1, stock1, time2, stock2, ...]
            stock_csv = o.get("stockCSV") or []
            for i in range(0, len(stock_csv), 2):
                if i + 1 >= len(stock_csv):
                    break
                tmin, stk = stock_csv[i], stock_csv[i + 1]
                if stk is None or stk < 0:
                    continue
                change_time = keepa_minute_to_datetime(tmin).isoformat()
                conn.execute("""
                    INSERT OR IGNORE INTO fct_keepa_seller_history
                        (asin, seller_id, change_time, stock, is_fba, is_prime)
                    VALUES (?,?,?,?,?,?)
                """, (asin, sid, change_time, int(stk), is_fba, is_prime))
                stats["stock_events"] += 1

            # offerCSV format: [time, price_cents, shipping_cents, time, ...]
            offer_csv = o.get("offerCSV") or []
            for i in range(0, len(offer_csv), 3):
                if i + 2 >= len(offer_csv):
                    break
                tmin, pcents, ship = offer_csv[i], offer_csv[i + 1], offer_csv[i + 2]
                if pcents is None or pcents < 0:
                    continue
                change_time = keepa_minute_to_datetime(tmin).isoformat()
                # Merge with stock row if same (asin, seller, time) — else new
                conn.execute("""
                    INSERT INTO fct_keepa_seller_history
                        (asin, seller_id, change_time, price_cents, shipping_cents,
                         is_fba, is_prime)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(asin, seller_id, change_time) DO UPDATE SET
                      price_cents    = COALESCE(excluded.price_cents, price_cents),
                      shipping_cents = COALESCE(excluded.shipping_cents, shipping_cents)
                """, (asin, sid, change_time, int(pcents),
                      int(ship) if ship and ship >= 0 else None,
                      is_fba, is_prime))
                stats["price_events"] += 1

        stats["asins"] += 1

    conn.commit()
    return stats


def mark_failures(conn: sqlite3.Connection, asins: list[str], err: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for a in asins:
        conn.execute("""
            INSERT INTO asin_api_state (asin, last_attempted_at, fetch_success, error_msg)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(asin) DO UPDATE SET
              last_attempted_at = excluded.last_attempted_at,
              fetch_success = 0,
              error_msg = excluded.error_msg
        """, (a, now, err[:200]))
    conn.commit()


# ── Status / queue inspection ─────────────────────────────────────────────
def show_status(conn: sqlite3.Connection) -> None:
    print("=== Keepa API Offers — Queue Status ===")
    try:
        tokens = get_tokens_left()
        print(f"Tokens left: {tokens}")
    except Exception as e:
        print(f"Token check failed: {e}")

    total = conn.execute("SELECT COUNT(*) FROM dim_product").fetchone()[0]
    fetched = conn.execute(
        "SELECT COUNT(*) FROM asin_api_state WHERE last_fetched_at IS NOT NULL"
    ).fetchone()[0]
    never = total - fetched
    print(f"ASINs in catalog: {total} ({never} never fetched, {fetched} fetched)")

    if fetched:
        oldest, newest = conn.execute("""
            SELECT MIN(last_fetched_at), MAX(last_fetched_at)
            FROM asin_api_state WHERE last_fetched_at IS NOT NULL
        """).fetchone()
        print(f"Oldest fetch: {oldest}")
        print(f"Newest fetch: {newest}")

    sellers = conn.execute("SELECT COUNT(*) FROM dim_keepa_seller").fetchone()[0]
    stock_evts = conn.execute("SELECT COUNT(*) FROM fct_keepa_seller_history WHERE stock IS NOT NULL").fetchone()[0]
    price_evts = conn.execute("SELECT COUNT(*) FROM fct_keepa_seller_history WHERE price_cents IS NOT NULL").fetchone()[0]
    print(f"Sellers known: {sellers}")
    print(f"Stock change events: {stock_evts:,}")
    print(f"Price change events: {price_evts:,}")

    # Preview next batch
    batch = pick_next_batch(conn, batch_size=10)
    print(f"\nNext 10 ASINs to fetch (priority order):")
    for a in batch:
        row = conn.execute(
            "SELECT last_fetched_at FROM asin_api_state WHERE asin=?", (a,)
        ).fetchone()
        last = row["last_fetched_at"] if row else None
        print(f"  {a}  last_fetched={last or '(never)'}")


# ── Main run ──────────────────────────────────────────────────────────────
def tick(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """One tick: pick a batch, fetch, save. Returns # ASINs processed."""
    if not API_KEY:
        log.error("KEEPA_API_KEY not set in environment / .env")
        return 0

    # 1. Token check
    try:
        balance = get_tokens_left()
    except Exception as e:
        log.error("Token check failed: %s — aborting tick", e)
        return 0
    log.info("Token balance: %d", balance)

    if balance < MIN_TOKENS_TO_RUN:
        log.info("Balance below MIN_TOKENS_TO_RUN=%d — skipping (will retry next tick)", MIN_TOKENS_TO_RUN)
        return 0

    # 2. Pick the batch
    asins = pick_next_batch(conn, batch_size=MAX_BATCH_SIZE)
    if not asins:
        log.info("No ASINs eligible for fetch (catalog empty?)")
        return 0

    # Cap batch size based on token budget — assume worst-case 6 tokens/ASIN
    spendable = max(0, balance - SAFETY_BUFFER)
    max_affordable = spendable // 6
    if max_affordable < len(asins):
        log.info("Token-capped batch: %d → %d (balance=%d, buffer=%d)",
                 len(asins), max_affordable, balance, SAFETY_BUFFER)
        asins = asins[:max_affordable]
    if not asins:
        log.info("Not enough tokens for even 1 ASIN — skipping tick")
        return 0

    if dry_run:
        print(f"[DRY RUN] Would fetch {len(asins)} ASINs:")
        for a in asins[:10]:
            print(f"  {a}")
        if len(asins) > 10:
            print(f"  ... and {len(asins) - 10} more")
        return 0

    # 3. Fetch
    try:
        data = fetch_products(asins)
    except Exception as e:
        log.error("API fetch failed: %s", e)
        mark_failures(conn, asins, str(e))
        return 0

    products = data.get("products", []) or []

    # 4. Parse + save
    stats = parse_and_save(conn, products)
    log.info("Saved: %d ASINs · %d stock events · %d price events",
             stats["asins"], stats["stock_events"], stats["price_events"])
    return stats["asins"]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tick", action="store_true",
                   help="Take one batch of stalest ASINs (use this from cron)")
    g.add_argument("--asins", type=str,
                   help="Comma-separated ASINs to fetch immediately")
    g.add_argument("--status", action="store_true",
                   help="Show token balance + queue stats, no API call")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what WOULD be fetched without spending tokens")
    args = ap.parse_args()

    conn = get_conn()
    try:
        if args.status:
            show_status(conn)
            return 0
        if args.asins:
            asins = [a.strip() for a in args.asins.split(",") if a.strip()]
            if args.dry_run:
                print(f"[DRY RUN] Would fetch: {asins}")
                return 0
            data = fetch_products(asins)
            stats = parse_and_save(conn, data.get("products", []) or [])
            log.info("Saved: %d ASINs · %d stock events · %d price events",
                     stats["asins"], stats["stock_events"], stats["price_events"])
            return 0
        if args.tick:
            tick(conn, dry_run=args.dry_run)
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

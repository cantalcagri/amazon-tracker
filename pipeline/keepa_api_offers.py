"""
Keepa API per-seller offers collector.

Each ASIN's `stockCSV` and `offerCSV` time-series (returned by Keepa's
`/product?offers=20&stock=1` endpoint) is parsed into one row per change
event in `fct_keepa_seller_history` — giving us a true per-seller stock
& price history.

Run modes:
  --tick           : take ONE batch (up to 100 stalest ASINs), exit. For cron.
  --loop           : run continuously, self-pacing on the token balance.
  --asins X,Y,Z    : fetch specific ASINs once.
  --status         : print token balance + queue stats + recent cost, no API call.
  --dry-run        : show what WOULD be fetched without spending tokens.

Cost-optimal architecture (DEFAULT):
  - The free Keepa Product Viewer CSV owns fct_keepa_daily (BSR, prices, offer
    counts, OOS, monthly sold, product codes — 0 API tokens).
  - This API fetches ONLY per-seller stock/price (offers=20 + stock=1, NO
    history) → ~4 tokens/ASIN. That's the one thing the CSV can't give.
  - Pass --history (~9 tokens/ASIN) only for a fully headless pipeline with no
    CSV; then the API also populates fct_keepa_daily.

Token economics (your plan):
  - 5 tokens/min refill = 7,200/day cap; burst cap = 300 (over-refill is lost)
  - Cost/ASIN VARIES with offers returned + history depth — see --status for the
    measured average (recorded per call in api_token_log).
  - Default (no history): ~4 tok/ASIN → 5K ASINs ≈ 20K tokens ≈ ~2.8 days/sweep
  - With --history: ~9 tok/ASIN → 5K ASINs ≈ ~6-7 days/sweep
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

# Amazon's own seller IDs (Amazon-as-seller always wins the buy box and has
# effectively infinite stock). domain 1 = amazon.com → ATVPDKIKX0DER. Add other
# marketplaces' Amazon IDs here when DOMAIN becomes configurable.
AMAZON_SELLER_IDS = {"ATVPDKIKX0DER"}

# Token-budget guardrails
MIN_TOKENS_TO_RUN = int(os.environ.get("KEEPA_MIN_TOKENS", 150))  # don't start a batch below this
MAX_BATCH_SIZE    = 100   # Keepa's hard max per call
SAFETY_BUFFER     = 50    # keep this many tokens unspent
TOKENS_PER_MIN    = 5     # your plan's refill rate (used by --loop to time sleeps)

# How many offers we ask Keepa for per product. ASINs with more sellers than
# this are returned partially — we flag those rows offers_truncated=1 so the
# per-seller totals/sales for them are treated as a lower bound, not complete.
OFFERS_PER_PRODUCT = 20

# Keepa epoch: their timestamps are minutes since 2011-01-01 (UTC)
KEEPA_EPOCH_MIN = 21564000  # offset to convert keepa-minute → unix-minute

# Cutoff: only store events newer than this many days. Keepa returns up to
# 9 years of history per call, but we don't need it — 90 days is enough for
# velocity, restock detection, and recent-trend charts. Saves DB space.
HISTORY_RETENTION_DAYS = 90


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(HERE / "keepa_api.log"), logging.StreamHandler()],
)
log = logging.getLogger("keepa_api_offers")


# ── DB helpers ────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    # WAL + busy_timeout so the collector can write while dashboards read.
    # DB must live on a local APFS path (not iCloud/network) — see db.get_conn.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def keepa_minute_to_datetime(keepa_min: int) -> datetime:
    """Keepa timestamps are minutes since 2011-01-01 UTC."""
    unix_seconds = (keepa_min + KEEPA_EPOCH_MIN) * 60
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def _table_exists_local(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _parse_magnitude(val) -> int | None:
    """Parse Keepa magnitude strings like '50+', '1K+', '2.5K', '3M' → int."""
    import re as _re
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("+", "").replace("#", "")
    if not s or s.lower() in {"-", "?", "n/a"}:
        return None
    m = _re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([kKmM]?)\s*$", s)
    if not m:
        m2 = _re.search(r"-?\d+", s)
        return int(m2.group(0)) if m2 else None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(round(float(m.group(1)) * mult))


def _last_val(csv: list | None) -> int | None:
    """Return the most recent value from a [time, val, time, val, ...] Keepa CSV."""
    if not csv or len(csv) < 2:
        return None
    return csv[-1] if csv[-1] is not None else None


def _cents_to_dollars(v: int | None) -> float | None:
    return v / 100.0 if v is not None and v >= 0 else None


def _extract_daily_snapshot(p: dict, today: str) -> dict:
    """
    Extract fct_keepa_daily fields from a Keepa /product API response object.

    This is the same data the Keepa Product Viewer CSV export provides, but
    sourced directly from the API — no Selenium, no logged-in Chrome needed.

    The API returns current values for most fields we want. For fields that are
    only in the CSV (e.g. monthly_sold), we do our best from what's available.
    """
    stats = p.get("stats") or {}
    csv_data = p.get("csv") or []          # price/rank history by type index
    offers = p.get("offers") or []
    live_ids = set(p.get("liveOffersOrder") or [])
    live_offers = [o for o in offers if o.get("offerId") in live_ids]

    # BSR: prefer the direct field (always present), fall back to csv[3] history.
    # salesRankCurrent is returned by Keepa regardless of history=0/1.
    bsr_current = p.get("salesRankCurrent")
    if not bsr_current:
        bsr_csv = csv_data[3] if len(csv_data) > 3 else None
        bsr_current = _last_val(bsr_csv)

    # Buy-box price: prefer offerCSV of the buy-box winner (available without
    # history), fall back to csv[18] when history=1 was requested.
    # We compute this below after live_offers is built.
    bb_csv = csv_data[18] if len(csv_data) > 18 else None
    bb_price_from_history = _cents_to_dollars(_last_val(bb_csv))

    # Buy-box stock + price: from the buy-box winner offer (always available).
    bb_stock = None
    bb_seller_name = None
    bb_price_from_offer = None
    if live_offers:
        bb_offer = live_offers[0]
        stk_csv = bb_offer.get("stockCSV") or []
        bb_stock = _last_val(stk_csv)
        if bb_stock is not None and bb_stock < 0:
            bb_stock = 0
        bb_seller_name = bb_offer.get("sellerName") or bb_offer.get("sellerId")
        # Price from offerCSV: format is [t, price_cents, ship_cents, ...]
        ocsv = bb_offer.get("offerCSV") or []
        if len(ocsv) >= 3:
            # last triple: ocsv[-3]=time, ocsv[-2]=price_cents, ocsv[-1]=ship_cents
            bb_price_from_offer = _cents_to_dollars(ocsv[-2]) if ocsv[-2] and ocsv[-2] > 0 else None
    bb_price = bb_price_from_offer or bb_price_from_history

    # FBA / FBM offer counts from live offers
    fba_offers_count = sum(1 for o in live_offers if o.get("isFBA"))
    fbm_offers_count = sum(1 for o in live_offers if not o.get("isFBA"))
    total_offers_count = len(live_offers)

    # Aggregate FBA stock = sum of last stock for all live FBA offers
    fba_stock_total = None
    fba_prices = []
    fbm_prices = []
    for o in live_offers:
        stk_csv = o.get("stockCSV") or []
        stk = _last_val(stk_csv)
        if stk is not None and stk >= 0:
            fba_stock_total = (fba_stock_total or 0) + (stk if o.get("isFBA") else 0)
        price_csv = o.get("offerCSV") or []
        if len(price_csv) >= 2:
            last_p = price_csv[-2]  # offerCSV is [t, price, ship, ...]; last pair at -3,-2,-1
            if last_p and last_p > 0:
                (fba_prices if o.get("isFBA") else fbm_prices).append(last_p)

    fba_price = _cents_to_dollars(min(fba_prices)) if fba_prices else None
    fbm_price = _cents_to_dollars(min(fbm_prices)) if fbm_prices else None

    # OOS 90d % from stats
    oos_90d = stats.get("outOfStockPercentage90") or stats.get("outOfStockPercentage")
    if isinstance(oos_90d, (int, float)):
        oos_90d = oos_90d / 100.0  # Keepa returns it as 0-100 integer

    # Monthly sold: Keepa returns it in stats.monthlySold (string like "50+")
    monthly_sold_raw = str(stats.get("monthlySold") or "").strip() or None

    # Top seller 30d/90d
    pct_top_30 = stats.get("buyBoxSeller30daysPercentage")
    pct_top_90 = stats.get("buyBoxSeller90daysPercentage")

    # Display group / category
    display_group = p.get("productGroup") or p.get("categoryTree", [{}])[0].get("name") if p.get("categoryTree") else None

    return {
        "snapshot_date":      today,
        "asin":               p.get("asin"),
        "sales_rank_current": bsr_current,
        "sales_rank_30d_avg": stats.get("avg30") if isinstance(stats.get("avg30"), int) else None,
        "display_group":      display_group,
        "monthly_sold":       monthly_sold_raw,
        "monthly_sold_num":   None,     # filled below by caller via parse_magnitude
        "monthly_sold_date":  None,
        "buy_box_price":      bb_price,
        "buy_box_stock":      bb_stock,
        "oos_90d_pct":        oos_90d,
        "buy_box_seller":     bb_seller_name,
        "pct_top_seller_30d": pct_top_30,
        "pct_top_seller_90d": pct_top_90,
        "is_fba_pct":         None,
        "fba_offers":         fba_offers_count or None,
        "fbm_offers":         fbm_offers_count or None,
        "total_offers":       total_offers_count or None,
        "fba_stock":          fba_stock_total,
        "fba_price":          fba_price,
        "fbm_price":          fbm_price,
        "raw_json":           None,   # we don't dump full JSON here; CSV path does
    }


# ── API ───────────────────────────────────────────────────────────────────
def get_tokens_left() -> int:
    r = requests.get(f"{KEEPA_BASE}/token", params={"key": API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()["tokensLeft"]


def log_token_usage(conn: sqlite3.Connection, endpoint: str, asin_count: int,
                    data: dict, with_history: bool = False) -> None:
    """Record one API call's token cost so the real per-ASIN average is auditable.

    Keepa bills by data returned, so cost varies a lot batch to batch. Best-effort
    and never fatal — a logging failure must not abort a fetch.
    """
    try:
        conn.execute(
            """INSERT INTO api_token_log
                   (ts, endpoint, asin_count, tokens_consumed, tokens_left, with_history)
               VALUES (?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), endpoint, asin_count,
             data.get("tokensConsumed"), data.get("tokensLeft"),
             1 if with_history else 0),
        )
        conn.commit()
    except Exception as e:
        log.debug("token-usage logging skipped: %s", e)


def fetch_products(asins: list[str], with_history: bool = False) -> dict:
    """One call for up to 100 ASINs. Returns the full Keepa JSON response.

    with_history=True (+5 tokens/ASIN) returns the csv[] arrays needed to populate
    BSR, buy-box price, and OOS% in fct_keepa_daily. with_history=False is the
    cheaper "per-seller only" mode (~4 tokens/ASIN) — stockCSV/offerCSV still come
    back from offers+stock, so seller activity is fully captured either way.
    """
    params = {
        "key": API_KEY, "domain": DOMAIN,
        "asin": ",".join(asins[:MAX_BATCH_SIZE]),
        "offers": OFFERS_PER_PRODUCT,        # +6 tokens/ASIN
        "stock":  1,                         # +3 tokens/ASIN
        "history": 1 if with_history else 0, # +5 tokens/ASIN when on
    }
    log.info("API call: %d ASINs (offers=%d, stock=1, history=%d)",
             len(asins), OFFERS_PER_PRODUCT, params["history"])
    t0 = time.time()
    r = requests.get(f"{KEEPA_BASE}/product", params=params, timeout=180)
    r.raise_for_status()
    elapsed = time.time() - t0
    data = r.json()
    log.info("  → HTTP %d in %.1fs · tokensConsumed=%s · tokensLeft=%s · products=%d",
             r.status_code, elapsed, data.get("tokensConsumed"),
             data.get("tokensLeft"), len(data.get("products", [])))
    return data


def fetch_seller_names(seller_ids: list[str]) -> dict:
    """Look up seller names + ratings. Costs 1 token per seller, up to 100/call."""
    params = {
        "key": API_KEY, "domain": DOMAIN,
        "seller": ",".join(seller_ids[:100]),
    }
    log.info("Seller lookup: %d sellers", len(seller_ids))
    r = requests.get(f"{KEEPA_BASE}/seller", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    log.info("  → tokensConsumed=%s · tokensLeft=%s · sellers returned=%d",
             data.get("tokensConsumed"), data.get("tokensLeft"),
             len(data.get("sellers", {})))
    return data


def update_seller_names(conn: sqlite3.Connection, only_missing: bool = True,
                         only_active_days: int = 90) -> int:
    """Fetch + persist seller name/rating for sellers with recent activity.

    If only_missing=True (default), skips sellers we already have a name for.
    If only_active_days set, restricts to sellers with stock events in that window
    (avoids spending tokens on dormant sellers).
    Returns # of sellers updated.
    """
    # Get list of seller_ids to fetch — restricted to those with recent activity
    where_parts = []
    if only_missing:
        where_parts.append("(d.seller_name IS NULL OR d.seller_name = '')")
    if only_active_days:
        where_parts.append(
            f"EXISTS (SELECT 1 FROM fct_keepa_seller_history h "
            f"        WHERE h.seller_id = d.seller_id "
            f"          AND h.change_time >= datetime('now', '-{only_active_days} days'))"
        )
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    rows = conn.execute(f"SELECT d.seller_id FROM dim_keepa_seller d {where}").fetchall()
    seller_ids = [r["seller_id"] for r in rows]
    if not seller_ids:
        log.info("No sellers need name lookup.")
        return 0
    log.info("Scope: %d sellers (only_missing=%s, only_active_days=%s)",
             len(seller_ids), only_missing, only_active_days)

    # Token guard — need ~1 per seller plus a small buffer
    try:
        balance = get_tokens_left()
    except Exception as e:
        log.error("Token check failed: %s", e)
        return 0
    needed = len(seller_ids) + SAFETY_BUFFER
    if balance < needed:
        # Reduce to what we can afford
        max_affordable = max(0, balance - SAFETY_BUFFER)
        log.warning("Token-capped: budget %d, need %d. Will fetch %d / %d sellers.",
                    balance, needed, max_affordable, len(seller_ids))
        seller_ids = seller_ids[:max_affordable]
    if not seller_ids:
        log.info("Not enough tokens to fetch any seller names.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    # Batch in 100s (Keepa max per call)
    for chunk_start in range(0, len(seller_ids), 100):
        chunk = seller_ids[chunk_start:chunk_start + 100]
        data = fetch_seller_names(chunk)
        log_token_usage(conn, "seller", len(chunk), data)
        sellers = data.get("sellers", {}) or {}
        for sid, info in sellers.items():
            name = info.get("sellerName")
            rating = info.get("currentRating")
            rating_count = info.get("currentRatingCount")
            is_amazon = 1 if sid in AMAZON_SELLER_IDS else 0
            conn.execute("""
                INSERT INTO dim_keepa_seller (seller_id, seller_name, rating_pct,
                                              review_count, is_amazon, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(seller_id) DO UPDATE SET
                    seller_name  = COALESCE(excluded.seller_name, seller_name),
                    rating_pct   = COALESCE(excluded.rating_pct, rating_pct),
                    review_count = COALESCE(excluded.review_count, review_count),
                    -- sticky: once known to be Amazon, never flip back to 0
                    is_amazon    = MAX(COALESCE(is_amazon, 0), COALESCE(excluded.is_amazon, 0)),
                    updated_at   = excluded.updated_at
            """, (sid, name, rating, rating_count, is_amazon, now))
            updated += 1
        conn.commit()
        # Politeness pause between batches
        if chunk_start + 100 < len(seller_ids):
            time.sleep(1)
    log.info("Updated %d seller names", updated)
    return updated


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
          -- Then oldest successful fetch
          s.last_fetched_at ASC,
          -- Final tie-break: within never-fetched, an ASIN we JUST attempted
          -- (and failed) has a recent last_attempted_at and sorts last, so dead
          -- ASINs don't get re-requested every tick and block fresh ones.
          s.last_attempted_at ASC
        LIMIT ?
    """, (batch_size,)).fetchall()
    return [r["asin"] for r in rows]


# ── Parsing & save ────────────────────────────────────────────────────────
def parse_and_save(conn: sqlite3.Connection, products: list[dict],
                   write_daily: bool = False) -> dict:
    """
    Parse Keepa /product response → upsert into:
      - fct_keepa_seller_history (stock & price change events)
      - dim_keepa_seller (seller metadata)
      - dim_product (title/brand/image — always, cheap, COALESCE merge)
      - asin_api_state (fetch success bookkeeping)
      - fct_keepa_daily (ONLY when write_daily=True)

    write_daily should be True only when this call was made with history=1 (so
    BSR/buy-box price/OOS are populated). In the cost-optimal setup the free CSV
    owns fct_keepa_daily, so the per-seller API runs with write_daily=False to
    avoid overwriting rich CSV rows with sparse ones.

    Writes are buffered and flushed with executemany() in one transaction, so a
    100-ASIN batch is a handful of statements instead of millions of execute()s.
    Returns a counts dict; stats["processed_asins"] is the set of ASINs Keepa
    actually returned (used by the caller to mark missing ASINs as failed).
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    # Only keep events from the last HISTORY_RETENTION_DAYS — Keepa returns 9+ years
    # of history per call but we only need recent for velocity / restock detection.
    cutoff_keepa_min = int((now_dt.timestamp() / 60) - KEEPA_EPOCH_MIN
                           - HISTORY_RETENTION_DAYS * 24 * 60)
    stats = {"asins": 0, "sellers_seen": 0, "stock_events": 0, "price_events": 0,
             "gone_events": 0, "skipped_old": 0, "truncated_asins": 0}
    processed: set[str] = set()

    today = now_dt.strftime("%Y-%m-%d")
    seller_rows:  list[tuple] = []   # dim_keepa_seller
    state_rows:   list[tuple] = []   # asin_api_state
    stock_rows:   list[tuple] = []   # (asin, sid, change_time, stock, is_fba, is_prime)
    price_rows:   list[tuple] = []   # (asin, sid, change_time, pcents, ship, is_fba, is_prime)
    daily_rows:   list[tuple] = []   # fct_keepa_daily — one row per ASIN for today
    product_rows: list[tuple] = []   # dim_product title/brand/image updates

    for p in products:
        asin = p.get("asin")
        if not asin:
            continue
        processed.add(asin)
        offers = p.get("offers") or []
        live_ids = set(p.get("liveOffersOrder") or [])
        live_seller_ids = {o.get("sellerId") for o in offers
                           if o.get("offerId") in live_ids and o.get("sellerId")}

        # P0-2: flag partial data. If Keepa captured more offers than the
        # offers=N cap returned (or we received the cap exactly), this ASIN's
        # per-seller picture is incomplete — downstream must treat its totals as
        # a lower bound and must NOT infer "seller gone" for absent sellers.
        returned = len(offers)
        offers_successful = p.get("offersSuccessful")
        offers_truncated = 1 if (
            (offers_successful is not None and offers_successful > returned)
            or returned >= OFFERS_PER_PRODUCT
        ) else 0
        if offers_truncated:
            stats["truncated_asins"] += 1
        live_count = sum(1 for o in offers if o.get("offerId") in live_ids)
        state_rows.append((asin, now, now, 1, None, live_count,
                           offers_successful, offers_truncated))

        for o in offers:
            sid = o.get("sellerId")
            if not sid:
                continue
            stats["sellers_seen"] += 1
            is_amazon = 1 if (o.get("isAmazon") or sid in AMAZON_SELLER_IDS) else 0
            seller_rows.append((sid, o.get("sellerName"), is_amazon, now))

            is_fba   = 1 if o.get("isFBA") else 0
            is_prime = 1 if o.get("isPrime") else 0

            # stockCSV format: [time1, stock1, time2, stock2, ...]
            last_tmin = last_stock = None
            stock_csv = o.get("stockCSV") or []
            for i in range(0, len(stock_csv) - 1, 2):
                tmin, stk = stock_csv[i], stock_csv[i + 1]
                if stk is None:
                    continue
                # Keepa emits stk = -1 when the seller stops offering. Map to 0
                # so v_asin_daily_sales credits the prior stock as sold.
                if stk < 0:
                    stk = 0
                last_tmin, last_stock = tmin, int(stk)
                if tmin < cutoff_keepa_min:
                    stats["skipped_old"] += 1
                    continue
                stock_rows.append((asin, sid, keepa_minute_to_datetime(tmin).isoformat(),
                                   int(stk), is_fba, is_prime))
                stats["stock_events"] += 1

            # P0-1: seller-disappeared. A dead offer (offerId not live, and the
            # seller has no other live offer) whose last observed stock was > 0
            # means Keepa never wrote the -1 terminal marker. Synthesize a
            # stock=0 just after its last sighting so the prior stock is credited
            # as sold. Skip when offers_truncated — an absent seller there may
            # simply have fallen below the offers cap, not sold out.
            if (not offers_truncated
                    and o.get("offerId") not in live_ids
                    and sid not in live_seller_ids
                    and last_stock and last_stock > 0):
                gone_tmin = last_tmin + 1
                if gone_tmin >= cutoff_keepa_min:
                    stock_rows.append((asin, sid,
                                       keepa_minute_to_datetime(gone_tmin).isoformat(),
                                       0, is_fba, is_prime))
                    stats["gone_events"] += 1

            # offerCSV format: [time, price_cents, shipping_cents, time, ...]
            offer_csv = o.get("offerCSV") or []
            for i in range(0, len(offer_csv) - 2, 3):
                tmin, pcents, ship = offer_csv[i], offer_csv[i + 1], offer_csv[i + 2]
                if pcents is None or pcents < 0:
                    continue
                if tmin < cutoff_keepa_min:
                    stats["skipped_old"] += 1
                    continue
                price_rows.append((asin, sid, keepa_minute_to_datetime(tmin).isoformat(),
                                   int(pcents), int(ship) if ship and ship >= 0 else None,
                                   is_fba, is_prime))
                stats["price_events"] += 1

        # ── dim_product metadata from API (title, brand, image) ────────────
        # The API returns richer product info than the ASIN list. Update on every
        # fetch so newly-added products get their metadata filled in automatically.
        title     = p.get("title")
        brand     = p.get("brand") or (p.get("manufacturer"))
        image_csv = p.get("imagesCSV") or ""
        image_url = None
        if image_csv:
            first_img = image_csv.split(",")[0].strip()
            if first_img:
                image_url = f"https://m.media-amazon.com/images/I/{first_img}"
        parent_asin = p.get("parentAsin")
        # variationCSV is a comma-separated string of sibling ASINs
        product_rows.append((asin, title, brand, image_url, parent_asin))

        # ── fct_keepa_daily snapshot from API ────────────────────────────────
        # Always write: even without history=1, the API gives us salesRankCurrent
        # (BSR), buy-box price (from offerCSV), offer counts, and FBA stock for
        # free as part of the offers+stock call. The CSV path can still overwrite
        # with richer data (OOS%, monthly sold, 30d avg) via INSERT OR REPLACE.
        snap = _extract_daily_snapshot(p, today)
        snap["monthly_sold_num"] = _parse_magnitude(snap.get("monthly_sold"))
        daily_rows.append((
            snap["snapshot_date"], snap["asin"],
            snap["sales_rank_current"], snap["sales_rank_30d_avg"],
            snap["display_group"],
            snap["monthly_sold"], snap["monthly_sold_num"], snap["monthly_sold_date"],
            snap["buy_box_price"], snap["buy_box_stock"],
            snap["oos_90d_pct"], snap["buy_box_seller"],
            snap["pct_top_seller_30d"], snap["pct_top_seller_90d"],
            snap["is_fba_pct"],
            snap["fba_offers"], snap["fbm_offers"], snap["total_offers"],
            snap["fba_stock"], snap["fba_price"], snap["fbm_price"],
            snap["raw_json"],
        ))

        stats["asins"] += 1

    # ── Bulk write (single transaction) ─────────────────────────────────────
    if seller_rows:
        conn.executemany("""
            INSERT INTO dim_keepa_seller (seller_id, seller_name, is_amazon, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(seller_id) DO UPDATE SET
              seller_name = COALESCE(excluded.seller_name, seller_name),
              is_amazon   = MAX(COALESCE(is_amazon, 0), COALESCE(excluded.is_amazon, 0)),
              updated_at  = excluded.updated_at
        """, seller_rows)
    if state_rows:
        conn.executemany("""
            INSERT INTO asin_api_state (asin, last_fetched_at, last_attempted_at,
                                        fetch_success, error_msg, offer_count,
                                        offers_successful, offers_truncated)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(asin) DO UPDATE SET
              last_fetched_at   = excluded.last_fetched_at,
              last_attempted_at = excluded.last_attempted_at,
              fetch_success     = excluded.fetch_success,
              error_msg         = NULL,
              offer_count       = excluded.offer_count,
              offers_successful = excluded.offers_successful,
              offers_truncated  = excluded.offers_truncated
        """, state_rows)
    # Stock rows first so the price upsert can merge onto the same PK row.
    if stock_rows:
        conn.executemany("""
            INSERT OR IGNORE INTO fct_keepa_seller_history
                (asin, seller_id, change_time, stock, is_fba, is_prime)
            VALUES (?,?,?,?,?,?)
        """, stock_rows)
    if price_rows:
        conn.executemany("""
            INSERT INTO fct_keepa_seller_history
                (asin, seller_id, change_time, price_cents, shipping_cents, is_fba, is_prime)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(asin, seller_id, change_time) DO UPDATE SET
              price_cents    = COALESCE(excluded.price_cents, price_cents),
              shipping_cents = COALESCE(excluded.shipping_cents, shipping_cents)
        """, price_rows)
    if product_rows:
        conn.executemany("""
            INSERT INTO dim_product (asin, title, brand, image_url, parent_asin)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(asin) DO UPDATE SET
              title       = COALESCE(excluded.title, title),
              brand       = COALESCE(excluded.brand, brand),
              image_url   = COALESCE(excluded.image_url, image_url),
              parent_asin = COALESCE(excluded.parent_asin, parent_asin)
        """, product_rows)
    if daily_rows:
        conn.executemany("""
            INSERT OR REPLACE INTO fct_keepa_daily (
                snapshot_date, asin, sales_rank_current, sales_rank_30d_avg,
                display_group, monthly_sold, monthly_sold_num, monthly_sold_date,
                buy_box_price, buy_box_stock, oos_90d_pct, buy_box_seller,
                pct_top_seller_30d, pct_top_seller_90d, is_fba_pct,
                fba_offers, fbm_offers, total_offers,
                fba_stock, fba_price, fbm_price, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, daily_rows)
    conn.commit()

    stats["processed_asins"] = processed
    stats["daily_rows"] = len(daily_rows)
    return stats


def mark_failures(conn: sqlite3.Connection, asins: list[str], err: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany("""
        INSERT INTO asin_api_state (asin, last_attempted_at, fetch_success, error_msg)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(asin) DO UPDATE SET
          last_attempted_at = excluded.last_attempted_at,
          fetch_success = 0,
          error_msg = excluded.error_msg
    """, [(a, now, err[:200]) for a in asins])
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

    # ── Token-usage accounting (the real, measured per-ASIN cost) ──
    if _table_exists_local(conn, "api_token_log"):
        row = conn.execute("""
            SELECT COUNT(*) batches,
                   SUM(tokens_consumed) tok,
                   SUM(asin_count) asins,
                   SUM(CASE WHEN with_history=1 THEN 1 ELSE 0 END) hist_batches
            FROM api_token_log
            WHERE endpoint='product' AND ts >= datetime('now','-7 days')
        """).fetchone()
        if row and row["batches"]:
            tok = row["tok"] or 0
            asins = row["asins"] or 0
            per_asin = tok / asins if asins else 0
            per_100 = per_asin * 100
            print("\n--- Token usage (product calls, last 7d) ---")
            print(f"  {row['batches']} batches · {asins:,} ASIN-fetches · {tok:,} tokens")
            print(f"  Avg cost: {per_asin:.1f} tok/ASIN  (~{per_100:.0f} tok / 100 ASINs)")
            print(f"  History-mode batches: {row['hist_batches']}/{row['batches']}")
        last24 = conn.execute("""
            SELECT SUM(tokens_consumed) FROM api_token_log
            WHERE ts >= datetime('now','-1 day')
        """).fetchone()[0]
        if last24:
            print(f"  Spent last 24h (all endpoints): {last24:,} tokens "
                  f"(refill cap ~{TOKENS_PER_MIN*60*24:,}/day)")

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
def tick(conn: sqlite3.Connection, dry_run: bool = False,
         batch_size: int = MAX_BATCH_SIZE, with_history: bool = False) -> int:
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
    batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))
    asins = pick_next_batch(conn, batch_size=batch_size)
    if not asins:
        log.info("No ASINs eligible for fetch (catalog empty?)")
        return 0

    # Keepa lets a single call go negative on tokens (call succeeds, then we
    # wait for refill). So once we're above MIN_TOKENS_TO_RUN, just send the
    # full 100. The MIN_TOKENS_TO_RUN gate above (150) ensures the deficit
    # afterward is bounded (~150 - 400 = -250, refills in ~50 min at 5/min).
    log.info("Sending full batch of %d ASINs (balance may go negative, refills 5/min)", len(asins))

    if dry_run:
        print(f"[DRY RUN] Would fetch {len(asins)} ASINs:")
        for a in asins[:10]:
            print(f"  {a}")
        if len(asins) > 10:
            print(f"  ... and {len(asins) - 10} more")
        return 0

    # 3. Fetch
    try:
        data = fetch_products(asins, with_history=with_history)
    except Exception as e:
        log.error("API fetch failed: %s", e)
        mark_failures(conn, asins, str(e))
        return 0

    log_token_usage(conn, "product", len(asins), data, with_history)
    products = data.get("products", []) or []

    # 4. Parse + save (write fct_keepa_daily only when we paid for history)
    stats = parse_and_save(conn, products, write_daily=with_history)
    log.info("Saved: %d ASINs · %d stock · %d price · %d seller-gone · "
             "%d daily-rows · %d truncated (skipped %d older than %dd)",
             stats["asins"], stats["stock_events"], stats["price_events"],
             stats.get("gone_events", 0), stats.get("daily_rows", 0),
             stats.get("truncated_asins", 0),
             stats.get("skipped_old", 0), HISTORY_RETENTION_DAYS)

    # 4b. Reconcile: ASINs we asked for but Keepa didn't return (dead/invalid
    # ASIN, domain mismatch). Mark them failed so last_attempted_at advances and
    # they drop to the back of the queue instead of being re-requested forever.
    missing = [a for a in asins if a not in stats.get("processed_asins", set())]
    if missing:
        log.warning("%d/%d requested ASINs not returned by Keepa — marking failed: %s",
                    len(missing), len(asins), ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
        mark_failures(conn, missing, "no product returned by Keepa")

    # 5. Auto-resolve names for any brand-new seller_ids we just discovered.
    # only_missing=True skips sellers we already have a name for, so we spend
    # exactly 1 token per truly-new seller — name mapping is permanent.
    try:
        n_named = update_seller_names(conn, only_missing=True, only_active_days=HISTORY_RETENTION_DAYS)
        if n_named:
            log.info("Auto-resolved %d new seller name(s)", n_named)
    except Exception as e:
        log.warning("Auto seller-name resolution failed (non-fatal): %s", e)

    return stats["asins"]


def run_loop(conn: sqlite3.Connection, batch_size: int = MAX_BATCH_SIZE,
             with_history: bool = False, min_tokens: int = MIN_TOKENS_TO_RUN,
             max_batches: int | None = None) -> int:
    """
    Token-aware continuous runner (the automated pipeline).

    Loop:
      1. Check the token balance (free, 0 tokens).
      2. If balance >= min_tokens → run ONE batch of `batch_size` stalest ASINs.
         Keepa lets that call drive the balance negative; the deficit is just
         paid down by the 5/min refill, so no refill is ever wasted.
      3. Else → sleep exactly long enough for refill to reach min_tokens
         (deficit ÷ TOKENS_PER_MIN), then loop. No busy-polling, no wasted
         wake-ups, and the balance never sits idle at the 300 burst cap.

    Runs forever by default (supervise with launchd KeepAlive — see
    scripts/com.amazontracker.loop.plist). `max_batches` bounds it for testing.
    Returns the number of batches actually run.
    """
    if not API_KEY:
        log.error("KEEPA_API_KEY not set — cannot start loop")
        return 0

    log.info("=== loop START (batch=%d, min_tokens=%d, history=%s) ===",
             batch_size, min_tokens, with_history)
    batches = 0
    while max_batches is None or batches < max_batches:
        try:
            balance = get_tokens_left()
        except Exception as e:
            log.error("Token check failed: %s — retrying in 60s", e)
            time.sleep(60)
            continue

        if balance >= min_tokens:
            n = tick(conn, batch_size=batch_size, with_history=with_history)
            batches += 1
            if n == 0:
                # Nothing fetched (empty catalog or transient error) — back off
                # a little so we don't spin.
                time.sleep(30)
            else:
                time.sleep(5)   # let tokensLeft settle before re-checking
        else:
            deficit = min_tokens - balance
            wait_s = (deficit / TOKENS_PER_MIN) * 60 + 30   # +30s safety margin
            log.info("Balance %d < %d — sleeping %.0fs (%.1f min) for refill",
                     balance, min_tokens, wait_s, wait_s / 60)
            time.sleep(wait_s)

    log.info("=== loop END (%d batches) ===", batches)
    return batches


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tick", action="store_true",
                   help="Take ONE batch of stalest ASINs and exit (use from cron)")
    g.add_argument("--loop", action="store_true",
                   help="Run continuously: batch whenever balance >= min-tokens, "
                        "else sleep precisely for refill. The automated runner.")
    g.add_argument("--asins", type=str,
                   help="Comma-separated ASINs to fetch immediately")
    g.add_argument("--status", action="store_true",
                   help="Show token balance + queue stats, no API call")
    g.add_argument("--seller-names", action="store_true",
                   help="Fetch missing seller names via /seller endpoint (1 token/seller)")
    ap.add_argument("--refresh-all-names", action="store_true",
                    help="With --seller-names: re-fetch ALL seller names (not just missing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what WOULD be fetched without spending tokens")
    ap.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE,
                    help=f"ASINs per call (1-{MAX_BATCH_SIZE}, default {MAX_BATCH_SIZE}). "
                         "100 gives the best per-ASIN bulk discount.")
    ap.add_argument("--history", action="store_true",
                    help="Add history=1 (~9 tok/ASIN) so the API also populates BSR/"
                         "buy-box/OOS in fct_keepa_daily — only needed for a fully "
                         "headless pipeline. DEFAULT off: the free CSV owns those "
                         "fields and the API fetches per-seller stock only (~4 tok/ASIN).")
    ap.add_argument("--max-batches", type=int, default=None,
                    help="With --loop: stop after N batches (for testing). Default: run forever.")
    args = ap.parse_args()
    with_history = args.history

    conn = get_conn()
    try:
        if args.status:
            show_status(conn)
            return 0
        if args.seller_names:
            n = update_seller_names(conn, only_missing=not args.refresh_all_names)
            print(f"Updated {n} seller names")
            return 0
        if args.asins:
            asins = [a.strip() for a in args.asins.split(",") if a.strip()]
            if args.dry_run:
                print(f"[DRY RUN] Would fetch: {asins}")
                return 0
            data = fetch_products(asins, with_history=with_history)
            log_token_usage(conn, "product", len(asins), data, with_history)
            stats = parse_and_save(conn, data.get("products", []) or [],
                                   write_daily=with_history)
            log.info("Saved: %d ASINs · %d stock events · %d price events (skipped %d older than 90d)",
                     stats["asins"], stats["stock_events"], stats["price_events"], stats.get("skipped_old", 0))
            return 0
        if args.tick:
            tick(conn, dry_run=args.dry_run, batch_size=args.batch_size,
                 with_history=with_history)
            return 0
        if args.loop:
            run_loop(conn, batch_size=args.batch_size, with_history=with_history,
                     max_batches=args.max_batches)
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

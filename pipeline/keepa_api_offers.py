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

# Active API key — set via --slot CLI flag or KEEPA_API_KEY_SLOT env var.
# Each slot gets its own shard of the ASIN queue (by ASIN hash) so the two
# loop instances never request the same ASIN and never burn each other's tokens.
_SLOT = int(os.environ.get("KEEPA_API_KEY_SLOT", "1"))
API_KEY = os.environ.get("KEEPA_API_KEY")

# Total number of key slots configured (1 = single key, 2 = dual key, etc.)
# Used to shard the queue: slot N owns ASINs where hash(asin) % N_SLOTS == N-1
N_SLOTS = int(os.environ.get("KEEPA_N_SLOTS", "1"))


def set_slot(slot: int | None = None, n_slots: int | None = None) -> None:
    """Resolve slot/key globals. The --slot CLI flag (visible in `ps`, unlike env
    vars) wins over KEEPA_API_KEY_SLOT so supervisors can tell loops apart."""
    global _SLOT, N_SLOTS, API_KEY
    if slot is not None:
        _SLOT = slot
    if n_slots is not None:
        N_SLOTS = n_slots
    if _SLOT == 2:
        API_KEY = os.environ.get("KEEPA_API_KEY_2") or os.environ.get("KEEPA_API_KEY")
    else:
        API_KEY = os.environ.get("KEEPA_API_KEY")


set_slot()

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

# One-time historical-BSR backfill: per tick, at most this many ASINs that have
# never had a history=1 pass (asin_api_state.history_done=0) are upgraded to
# history=1 (+5 tok each, paid ONCE per ASIN ever). Spreads the catalog-wide
# backfill over a few sweeps without starving per-seller freshness.
HISTORY_BACKFILL_PER_TICK = int(os.environ.get("KEEPA_HISTORY_PER_TICK", 25))


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


def _extract_daily_snapshots(p: dict, today: str,
                             full_history: bool = False,
                             retention_days: int = HISTORY_RETENTION_DAYS) -> list[dict]:
    """
    Extract fct_keepa_daily rows from a Keepa /product API response.

    full_history=False (default, history=0 call):
        Returns [today_snapshot] — current BSR/price/offers only.

    full_history=True (history=1 call, new ASINs):
        Returns a list of daily snapshots going back `retention_days`.
        Parses the full csv[3] BSR time-series and csv[18] buy-box time-series,
        bucketing events into calendar days (last observation wins per day).
        Non-BSR fields (offers, stock, OOS) are only set on today's row since
        that data isn't available in historical time-series from offers=20.

    The CSV Keepa Product Viewer export always wins when it exists — historical
    rows use INSERT OR IGNORE so they never overwrite richer CSV rows.
    """
    stats = p.get("stats") or {}
    csv_data = p.get("csv") or []
    offers = p.get("offers") or []
    live_ids = set(p.get("liveOffersOrder") or [])
    live_offers = [o for o in offers if o.get("offerId") in live_ids]

    # stats.current is an array indexed like the csv[] types (3=BSR, 18=buy box
    # incl. shipping); -1 means "no data". Present whenever stats=N is requested,
    # even with history=0 — the primary BSR source for cheap known-ASIN fetches.
    stats_current = stats.get("current") or []

    def _stat_at(idx: int):
        if len(stats_current) > idx and stats_current[idx] is not None \
                and stats_current[idx] >= 0:
            return stats_current[idx]
        return None

    # ── Today's aggregate fields (current state from live offers) ────────────
    bsr_current = p.get("salesRankCurrent") or _stat_at(3)
    if not bsr_current:
        bsr_csv = csv_data[3] if len(csv_data) > 3 else None
        bsr_current = _last_val(bsr_csv)

    bb_csv = csv_data[18] if len(csv_data) > 18 else None
    bb_price_from_history = _cents_to_dollars(_last_val(bb_csv) or _stat_at(18))

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

    # OOS 90d % from stats. Keepa returns it as an array indexed like csv[]
    # types (0=Amazon, 1=New, ...) — use New; -1 means no data.
    oos_90d = stats.get("outOfStockPercentage90") or stats.get("outOfStockPercentage")
    if isinstance(oos_90d, list):
        oos_90d = oos_90d[1] if len(oos_90d) > 1 else None
    if isinstance(oos_90d, (int, float)):
        oos_90d = oos_90d / 100.0 if oos_90d >= 0 else None  # 0-100 int → fraction

    # Monthly sold: top-level product field (int) or stats (string like "50+")
    monthly_sold_raw = str(p.get("monthlySold") or stats.get("monthlySold") or "").strip() or None

    # Top seller 30d/90d
    pct_top_30 = stats.get("buyBoxSeller30daysPercentage")
    pct_top_90 = stats.get("buyBoxSeller90daysPercentage")

    # Display group / category
    display_group = p.get("productGroup") or p.get("categoryTree", [{}])[0].get("name") if p.get("categoryTree") else None

    asin = p.get("asin")

    def _base(date_str: str, bsr: int | None, bb: float | None) -> dict:
        """Minimal row for a historical date — only BSR + buy-box price."""
        return {
            "snapshot_date": date_str, "asin": asin,
            "sales_rank_current": bsr, "sales_rank_30d_avg": None,
            "display_group": None, "monthly_sold": None,
            "monthly_sold_num": None, "monthly_sold_date": None,
            "buy_box_price": bb, "buy_box_stock": None,
            "oos_90d_pct": None, "buy_box_seller": None,
            "pct_top_seller_30d": None, "pct_top_seller_90d": None,
            "is_fba_pct": None, "fba_offers": None, "fbm_offers": None,
            "total_offers": None, "fba_stock": None,
            "fba_price": None, "fbm_price": None, "raw_json": None,
        }

    # ── Today's full snapshot ────────────────────────────────────────────────
    today_row = _base(today, bsr_current, bb_price)
    today_row.update({
        "sales_rank_30d_avg": (stats.get("avg30")[3]
                               if isinstance(stats.get("avg30"), list)
                                  and len(stats.get("avg30")) > 3
                                  and stats.get("avg30")[3] is not None
                                  and stats.get("avg30")[3] >= 0
                               else None),
        "display_group":      display_group,
        "monthly_sold":       monthly_sold_raw,
        "monthly_sold_num":   _parse_magnitude(monthly_sold_raw),
        "buy_box_stock":      bb_stock,
        "oos_90d_pct":        oos_90d,
        "buy_box_seller":     bb_seller_name,
        "pct_top_seller_30d": pct_top_30,
        "pct_top_seller_90d": pct_top_90,
        "fba_offers":         fba_offers_count or None,
        "fbm_offers":         fbm_offers_count or None,
        "total_offers":       total_offers_count or None,
        "fba_stock":          fba_stock_total,
        "fba_price":          fba_price,
        "fbm_price":          fbm_price,
    })

    if not full_history:
        return [today_row]

    # ── Full historical BSR + buy-box price (csv[3] and csv[18]) ────────────
    # Keepa returns up to 10 years; we cap at retention_days to keep DB lean.
    # One row per calendar day; last observed value that day wins.
    # Uses INSERT OR IGNORE so it never overwrites richer CSV-imported rows.
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - __import__('datetime').timedelta(days=retention_days)).date()

    bsr_by_day: dict[str, int] = {}
    bsr_csv_raw = csv_data[3] if len(csv_data) > 3 else []
    if bsr_csv_raw:
        for i in range(0, len(bsr_csv_raw) - 1, 2):
            tmin, bsr_v = bsr_csv_raw[i], bsr_csv_raw[i + 1]
            if bsr_v is None or bsr_v < 0:
                continue
            day = keepa_minute_to_datetime(tmin).date()
            if day < cutoff:
                continue
            bsr_by_day[day.isoformat()] = int(bsr_v)

    bb_by_day: dict[str, float] = {}
    bb_csv_raw = csv_data[18] if len(csv_data) > 18 else []
    if bb_csv_raw:
        for i in range(0, len(bb_csv_raw) - 1, 2):
            tmin, cents = bb_csv_raw[i], bb_csv_raw[i + 1]
            if cents is None or cents < 0:
                continue
            day = keepa_minute_to_datetime(tmin).date()
            if day < cutoff:
                continue
            bb_by_day[day.isoformat()] = cents / 100.0

    # Merge the two day sets; skip today (already in today_row)
    all_days = sorted((set(bsr_by_day) | set(bb_by_day)) - {today})
    rows: list[dict] = [_base(d, bsr_by_day.get(d), bb_by_day.get(d))
                        for d in all_days]
    rows.append(today_row)   # today always last (richest data)
    return rows


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
        # stats costs 0 extra tokens and returns stats.current (BSR, buy box,
        # OOS%) even with history=0 — without it, history=0 daily rows have
        # NULL BSR, which is exactly the gap that hit June 2-9 2026.
        "stats": 90,
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
    # When N_SLOTS > 1, each slot only picks ASINs from its shard (by ASIN hash)
    # so two parallel loop instances (each with a different API key) never overlap.
    shard_filter = ""
    if N_SLOTS > 1:
        shard_filter = f"AND (ABS(CAST(SUBSTR(s.asin,3,8) AS INTEGER)) % {N_SLOTS}) = {_SLOT - 1}"

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
        LEFT JOIN (
            SELECT asin, MAX(sales_rank_current IS NOT NULL) AS has_bsr
            FROM fct_keepa_daily GROUP BY asin
        ) b ON b.asin = s.asin
        WHERE 1=1 {shard_filter}
        ORDER BY
          -- Dead ASINs last: never showed a BSR and no offers on the latest
          -- fetch → refetch weekly instead of every sweep, freeing tokens for
          -- live listings. (They stay in the catalog and still get checked.)
          CASE WHEN COALESCE(b.has_bsr, 0) = 0
                    AND COALESCE(s.offer_count, 0) = 0
                    AND s.last_fetched_at IS NOT NULL
                    AND julianday('now') - julianday(s.last_fetched_at) < 7.0
               THEN 1 ELSE 0 END,
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

    # Local calendar date, matching keepa_csv_importer's date.today() — UTC here
    # would put evening fetches (after 5pm PDT) on tomorrow's snapshot_date and
    # split one real day across two rows.
    today = datetime.now().strftime("%Y-%m-%d")
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

        # ── fct_keepa_daily: today + optional full history ───────────────────
        # _extract_daily_snapshots returns [today] normally, or
        # [hist_day_1, ..., hist_day_N, today] when write_daily=full_history.
        snaps = _extract_daily_snapshots(p, today, full_history=write_daily)
        for snap in snaps:
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
        stats["daily_rows"] = stats.get("daily_rows", 0) + len(snaps)

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
        # Historical rows (past days): INSERT OR IGNORE — never overwrite richer
        # CSV-imported data. Today's row: full merge — update any NULL fields but
        # preserve non-NULL values already there (e.g. OOS% from CSV import).
        hist_rows = [r for r in daily_rows if r[0] != today]
        today_rows = [r for r in daily_rows if r[0] == today]
        if hist_rows:
            conn.executemany("""
                INSERT OR IGNORE INTO fct_keepa_daily (
                    snapshot_date, asin, sales_rank_current, sales_rank_30d_avg,
                    display_group, monthly_sold, monthly_sold_num, monthly_sold_date,
                    buy_box_price, buy_box_stock, oos_90d_pct, buy_box_seller,
                    pct_top_seller_30d, pct_top_seller_90d, is_fba_pct,
                    fba_offers, fbm_offers, total_offers,
                    fba_stock, fba_price, fbm_price, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, hist_rows)
        if today_rows:
            conn.executemany("""
                INSERT INTO fct_keepa_daily (
                    snapshot_date, asin, sales_rank_current, sales_rank_30d_avg,
                    display_group, monthly_sold, monthly_sold_num, monthly_sold_date,
                    buy_box_price, buy_box_stock, oos_90d_pct, buy_box_seller,
                    pct_top_seller_30d, pct_top_seller_90d, is_fba_pct,
                    fba_offers, fbm_offers, total_offers,
                    fba_stock, fba_price, fbm_price, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_date, asin) DO UPDATE SET
                    sales_rank_current = COALESCE(sales_rank_current, excluded.sales_rank_current),
                    buy_box_price      = COALESCE(buy_box_price,      excluded.buy_box_price),
                    buy_box_stock      = COALESCE(buy_box_stock,      excluded.buy_box_stock),
                    buy_box_seller     = COALESCE(buy_box_seller,     excluded.buy_box_seller),
                    fba_offers         = COALESCE(fba_offers,         excluded.fba_offers),
                    fbm_offers         = COALESCE(fbm_offers,         excluded.fbm_offers),
                    total_offers       = COALESCE(total_offers,       excluded.total_offers),
                    fba_stock          = COALESCE(fba_stock,          excluded.fba_stock),
                    fba_price          = COALESCE(fba_price,          excluded.fba_price),
                    fbm_price          = COALESCE(fbm_price,          excluded.fbm_price)
            """, today_rows)
    conn.commit()

    stats["processed_asins"] = processed
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
def _ensure_history_done_column(conn: sqlite3.Connection) -> None:
    """Idempotent: add asin_api_state.history_done (1 = this ASIN already had
    its one-time history=1 BSR backfill, never pay the +5 tokens again).

    On first run, ASINs that already have deep BSR history (rows older than
    45 days) are stamped done so we don't re-buy what we have.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(asin_api_state)")]
    if "history_done" in cols:
        return
    conn.execute("ALTER TABLE asin_api_state ADD COLUMN history_done INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        UPDATE asin_api_state SET history_done = 1 WHERE asin IN (
            SELECT asin FROM fct_keepa_daily
            WHERE sales_rank_current IS NOT NULL
              AND snapshot_date < date('now', '-45 day')
            GROUP BY asin)
    """)
    conn.commit()
    n = conn.execute("SELECT SUM(history_done=0) FROM asin_api_state").fetchone()[0]
    log.info("history_done column added — %s ASINs queued for one-time BSR backfill", n)


def _mark_history_done(conn: sqlite3.Connection, asins) -> None:
    asins = list(asins)
    if asins:
        conn.executemany("UPDATE asin_api_state SET history_done=1 WHERE asin=?",
                         [(a,) for a in asins])
        conn.commit()


def _new_asins(conn: sqlite3.Connection, asins: list[str]) -> set[str]:
    """Return the subset of ASINs that have never been fetched before.
    These get history=1 so we backfill their full BSR/price timeline.
    """
    rows = conn.execute(
        "SELECT asin FROM asin_api_state WHERE asin IN ({}) "
        "AND last_fetched_at IS NOT NULL".format(",".join("?" * len(asins))),
        asins,
    ).fetchall()
    already_fetched = {r["asin"] for r in rows}
    return set(asins) - already_fetched


def _history_pending(conn: sqlite3.Connection, asins: list[str]) -> set[str]:
    """ASINs in this batch still owed their one-time history=1 backfill."""
    rows = conn.execute(
        "SELECT asin FROM asin_api_state WHERE asin IN ({}) "
        "AND COALESCE(history_done, 0) = 0".format(",".join("?" * len(asins))),
        asins,
    ).fetchall()
    return {r["asin"] for r in rows}


def tick(conn: sqlite3.Connection, dry_run: bool = False,
         batch_size: int = MAX_BATCH_SIZE, with_history: bool = False) -> int:
    """One tick: pick a batch, fetch, save. Returns # ASINs processed.

    Smart history mode (default):
      - ASINs owed their ONE-TIME history=1 pass (never fetched before, or
        history_done=0 from before the smart-history era) → full 90-day BSR
        backfill, up to HISTORY_BACKFILL_PER_TICK per tick, then stamped
        history_done so the +5 tok is never paid again for that ASIN.
      - All other ASINs → history=0 (cost-optimal, just today's snapshot).
    Pass with_history=True to force history=1 for all ASINs in the batch.
    """
    if not API_KEY:
        log.error("KEEPA_API_KEY not set in environment / .env")
        return 0
    _ensure_history_done_column(conn)

    # 1. Token check
    try:
        balance = get_tokens_left()
    except Exception as e:
        log.error("Token check failed: %s — aborting tick", e)
        return 0
    log.info("Token balance: %d", balance)

    if balance < MIN_TOKENS_TO_RUN:
        log.info("Balance below MIN_TOKENS_TO_RUN=%d — skipping", MIN_TOKENS_TO_RUN)
        return 0

    # 2. Pick the batch
    batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))
    asins = pick_next_batch(conn, batch_size=batch_size)
    if not asins:
        log.info("No ASINs eligible for fetch (catalog empty?)")
        return 0

    if dry_run:
        print(f"[DRY RUN] Would fetch {len(asins)} ASINs:")
        for a in asins[:10]: print(f"  {a}")
        if len(asins) > 10: print(f"  ... and {len(asins) - 10} more")
        return 0

    total_processed = 0

    # 3a. One-time history=1 backfill: new ASINs + ASINs never stamped
    # history_done (capped per tick to bound token spend at +5 tok each)
    if not with_history:
        hist = sorted(_new_asins(conn, asins) | _history_pending(conn, asins))
        hist = hist[:HISTORY_BACKFILL_PER_TICK]
        if hist:
            log.info("History backfill in batch: %d ASINs → history=1 (one-time)", len(hist))
            try:
                data = fetch_products(hist, with_history=True)
            except Exception as e:
                log.error("History fetch failed: %s", e)
                mark_failures(conn, hist, str(e))
                hist = []
            if hist:
                log_token_usage(conn, "product", len(hist), data, True)
                products = data.get("products", []) or []
                stats = parse_and_save(conn, products, write_daily=True)
                log.info("History backfill: %d ASINs · %d daily-rows · %d stock",
                         stats["asins"], stats.get("daily_rows", 0), stats["stock_events"])
                missing = [a for a in hist if a not in stats.get("processed_asins", set())]
                if missing: mark_failures(conn, missing, "no product returned by Keepa")
                # Stamp even the missing ones: Keepa has no product for them, so
                # retrying the +5 tok history pass every sweep would buy nothing.
                _mark_history_done(conn, hist)
                total_processed += stats["asins"]
        # Remove history-pass ASINs from the main batch
        known_asins = [a for a in asins if a not in set(hist)]
    else:
        known_asins = asins

    # 3b. Known ASINs: history=0 (cost-optimal)
    if known_asins:
        log.info("Known ASINs: %d → fetching with history=0 (today only)", len(known_asins))
        try:
            data = fetch_products(known_asins, with_history=with_history)
        except Exception as e:
            log.error("API fetch failed: %s", e)
            mark_failures(conn, known_asins, str(e))
            return total_processed

        log_token_usage(conn, "product", len(known_asins), data, with_history)
        products = data.get("products", []) or []
        stats = parse_and_save(conn, products, write_daily=with_history)
        log.info("Known ASINs saved: %d · %d stock · %d price · %d seller-gone · "
                 "%d daily-rows · %d truncated (skipped %d older than %dd)",
                 stats["asins"], stats["stock_events"], stats["price_events"],
                 stats.get("gone_events", 0), stats.get("daily_rows", 0),
                 stats.get("truncated_asins", 0),
                 stats.get("skipped_old", 0), HISTORY_RETENTION_DAYS)
        # Reconcile missing ASINs
        missing = [a for a in known_asins if a not in stats.get("processed_asins", set())]
        if missing:
            log.warning("%d ASINs not returned by Keepa — marking failed", len(missing))
            mark_failures(conn, missing, "no product returned by Keepa")
        if with_history:
            _mark_history_done(conn, known_asins)
        total_processed += stats["asins"]

    # 5. Auto-resolve seller names
    try:
        n_named = update_seller_names(conn, only_missing=True, only_active_days=HISTORY_RETENTION_DAYS)
        if n_named:
            log.info("Auto-resolved %d new seller name(s)", n_named)
    except Exception as e:
        log.warning("Auto seller-name resolution failed (non-fatal): %s", e)

    return total_processed


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
    ap.add_argument("--slot", type=int, choices=[1, 2], default=None,
                    help="API key slot (1 or 2). Visible in `ps`, unlike the "
                         "KEEPA_API_KEY_SLOT env var, so supervisors can tell "
                         "the two loops apart. Overrides the env var.")
    ap.add_argument("--n-slots", type=int, default=None,
                    help="Total key slots (queue shards). Overrides KEEPA_N_SLOTS.")
    args = ap.parse_args()
    set_slot(args.slot, args.n_slots)
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

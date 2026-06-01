-- ============================================================
-- Amazon Seller Tracker Schema
-- dim_product  — static product info (one row per child ASIN)
-- fct_asin_daily — daily snapshot per ASIN (fact table, append-only)
-- ============================================================
--
-- CONTRACT: this file is SAFE TO RE-RUN at any time.
--   * All CREATE TABLE statements use `IF NOT EXISTS` (never drop data).
--   * All views are `DROP VIEW IF EXISTS` then `CREATE VIEW` (always
--     refreshed to the latest definition — views hold no data).
--   * Applied via `db.init_db()` or `sqlite3 amazon_tracker.db < schema.sql`.
-- If you add a MATERIALIZED table that replaces a view, update db.init_db()
-- and document the migration here — re-running must stay non-destructive.
-- ============================================================

-- dim_product: one row per tracked child ASIN
--
-- Multi-marketplace identity (see CLAUDE.md "Multi-marketplace / Costco"):
--   marketplace — 'amazon_us' (default), 'amazon_ca', 'costco_us', ...
--   item_uid    — marketplace-neutral product key; same physical product shares
--                 one item_uid across marketplaces. NULL until assigned.
--   gtin        — UPC/EAN, the bridge to external sources like the Costco pipeline.
-- The Amazon ASIN is NOT unique across marketplaces; join cross-marketplace on
-- item_uid / gtin, never on asin alone.
CREATE TABLE IF NOT EXISTS dim_product (
    asin           TEXT PRIMARY KEY,
    marketplace    TEXT NOT NULL DEFAULT 'amazon_us',
    parent_asin    TEXT,
    title          TEXT,
    brand          TEXT,
    variation_size  TEXT,
    variation_color TEXT,
    image_url       TEXT,  -- Amazon CDN URL (from Keepa "Swatch Image" column)
    gtin            TEXT,   -- GTIN (marketplace-neutral; bridges to Costco etc.)
    upc             TEXT,   -- UPC code (Product Codes: UPC)
    ean             TEXT,   -- EAN code (Product Codes: EAN)
    part_number     TEXT,   -- manufacturer part number
    weight_g        REAL,   -- item weight in grams (for shipping/FBA fee calc)
    referral_fee_pct  REAL, -- Amazon referral fee %
    fba_pick_pack_fee REAL, -- FBA pick & pack fee ($)
    item_uid        TEXT    -- canonical product id shared across marketplaces
);
CREATE INDEX IF NOT EXISTS idx_dim_product_item_uid ON dim_product(item_uid);
CREATE INDEX IF NOT EXISTS idx_dim_product_gtin     ON dim_product(gtin);
CREATE INDEX IF NOT EXISTS idx_dim_product_upc      ON dim_product(upc);

-- fct_asin_daily: DEPRECATED — output of the deleted Selenium AOD scraper.
-- Nothing writes this table anymore. Kept only so old explore.ipynb queries
-- don't crash. Do not build new code on it; use fct_keepa_daily +
-- fct_keepa_seller_history instead. Safe to DROP once no notebook references it.
-- sellers: JSON array [{name, fulfillment, price, inventory}, ...]
-- units_sold: diff vs previous day — NULL on first capture (no baseline)
CREATE TABLE IF NOT EXISTS fct_asin_daily (
    snapshot_date      DATE NOT NULL,
    asin               TEXT NOT NULL REFERENCES dim_product(asin),
    bsr_rank           INTEGER,
    bsr_category       TEXT,
    sellers            TEXT,
    total_sellers      INTEGER,
    fba_sellers        INTEGER,
    fbm_sellers        INTEGER,
    min_price          REAL,
    max_price          REAL,
    avg_price_weighted REAL,
    total_inventory    INTEGER,
    units_sold         INTEGER,
    fba_units_sold     INTEGER,
    fbm_units_sold     INTEGER,
    PRIMARY KEY (snapshot_date, asin)
);
CREATE INDEX IF NOT EXISTS idx_fct_asin_daily_asin ON fct_asin_daily(asin);

-- fct_keepa_daily: cumulative daily snapshot from Keepa Product Viewer CSV.
-- One row per ASIN per day. Never purged. Typed columns for the fields we
-- chart most; raw_json keeps every other CSV column so we never lose data
-- if Keepa adds new columns or we want to add a chart later.
CREATE TABLE IF NOT EXISTS fct_keepa_daily (
    snapshot_date         DATE NOT NULL,
    asin                  TEXT NOT NULL REFERENCES dim_product(asin),
    sales_rank_current    INTEGER,
    sales_rank_30d_avg    INTEGER,
    display_group         TEXT,
    monthly_sold          TEXT,
    monthly_sold_num      INTEGER,   -- numeric form of monthly_sold (50, 100, 200, ...)
    monthly_sold_date     TEXT,
    buy_box_price         REAL,
    buy_box_stock         INTEGER,   -- buy-box-seller-specific stock
    oos_90d_pct           REAL,
    buy_box_seller        TEXT,
    pct_top_seller_30d    REAL,
    pct_top_seller_90d    REAL,
    is_fba_pct            REAL,      -- (Keepa column "Buy Box: Is FBA" — yes/no; legacy name)
    fba_offers            INTEGER,   -- New FBA Offer Count: Current
    fbm_offers            INTEGER,   -- New FBM Offer Count: Current
    total_offers          INTEGER,   -- Total Offer Count
    fba_stock             INTEGER,   -- New, 3rd Party FBA: Stock (aggregate)
    fba_price             REAL,      -- New, 3rd Party FBA: Current
    fbm_price             REAL,      -- New, 3rd Party FBM: Current
    new_offer_count       INTEGER,   -- New Offer Count: Current
    rating                REAL,      -- Reviews: Rating (stars)
    rating_count          INTEGER,   -- Reviews: Rating Count
    bought_past_month     INTEGER,   -- Monthly Sales Trends: Bought in past month
    monthly_sold_peak     INTEGER,   -- Monthly Sales Trends: Monthly Sold (Peak)
    pct_amazon_30d        REAL,      -- Buy Box: % Amazon 30 days (Amazon-as-competitor)
    pct_amazon_90d        REAL,      -- Buy Box: % Amazon 90 days
    return_rate           REAL,      -- Return Rate
    raw_json              TEXT,
    PRIMARY KEY (snapshot_date, asin)
);
CREATE INDEX IF NOT EXISTS idx_fct_keepa_daily_asin ON fct_keepa_daily(asin);

-- ════════════════════════════════════════════════════════════════════════
-- PER-SELLER PIPELINE (Keepa API /product?offers=20&stock=1)
-- ════════════════════════════════════════════════════════════════════════

-- Cumulative per-seller stock & price change history.
-- Derived from parsing each offer's stockCSV/offerCSV time-series.
-- One row per change EVENT (when Keepa observed a stock or price change).
CREATE TABLE IF NOT EXISTS fct_keepa_seller_history (
    asin              TEXT NOT NULL,
    seller_id         TEXT NOT NULL,
    change_time       DATETIME NOT NULL,   -- UTC, from Keepa minute epoch
    stock             INTEGER,             -- units (NULL = price-only change)
    price_cents       INTEGER,             -- price in cents
    shipping_cents    INTEGER,
    is_fba            INTEGER,             -- 1/0
    is_prime          INTEGER,             -- 1/0
    PRIMARY KEY (asin, seller_id, change_time)
);
CREATE INDEX IF NOT EXISTS idx_seller_hist_asin_time
    ON fct_keepa_seller_history(asin, change_time);
CREATE INDEX IF NOT EXISTS idx_seller_hist_seller_time
    ON fct_keepa_seller_history(seller_id, change_time);
-- Drives the retention purge (health_check.py --purge): DELETE WHERE change_time < cutoff.
CREATE INDEX IF NOT EXISTS idx_seller_hist_time
    ON fct_keepa_seller_history(change_time);

-- Seller dimension: stable seller_id → name + rating.
-- Updated on each API fetch with the latest seen data.
CREATE TABLE IF NOT EXISTS dim_keepa_seller (
    seller_id       TEXT PRIMARY KEY,
    seller_name     TEXT,
    rating_pct      INTEGER,    -- past 12 months %
    review_count    INTEGER,
    is_amazon       INTEGER,
    updated_at      DATETIME
);

-- API fetch state per ASIN: drives the "oldest-first" priority queue.
CREATE TABLE IF NOT EXISTS asin_api_state (
    asin            TEXT PRIMARY KEY,
    last_fetched_at DATETIME,        -- most recent successful fetch
    last_attempted_at DATETIME,      -- most recent attempt (success or fail)
    fetch_success   INTEGER,         -- 1 if last attempt succeeded
    error_msg       TEXT,
    offer_count     INTEGER,         -- live offers seen on last fetch
    offers_successful INTEGER,       -- offers Keepa captured (may exceed returned)
    offers_truncated  INTEGER        -- 1 = >offers cap; per-seller data is partial
);

-- Pipeline run log: one row per orchestrated daily run (written by
-- scripts/daily_pipeline.sh). Drives the dashboard's "last run" indicator
-- and gives a quick audit trail of what ran when.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      DATETIME NOT NULL,
    finished_at     DATETIME,
    status          TEXT,            -- 'success' | 'partial' | 'failed'
    csv_rows_today  INTEGER,         -- fct_keepa_daily rows for today's date
    seller_events   INTEGER,         -- total rows in fct_keepa_seller_history
    notes           TEXT
);

-- API token-usage log: one row per Keepa API call. Keepa bills by data returned
-- (offers + history depth), so cost/ASIN VARIES a lot — this table makes the real
-- average measurable (see keepa_api_offers.py --status). 0 tokens to maintain.
CREATE TABLE IF NOT EXISTS api_token_log (
    ts              DATETIME NOT NULL,
    endpoint        TEXT,            -- 'product' | 'seller'
    asin_count      INTEGER,         -- ASINs/sellers in the call
    tokens_consumed INTEGER,
    tokens_left     INTEGER,         -- balance reported AFTER the call
    with_history    INTEGER          -- 1 if history=1 was requested (product calls)
);
CREATE INDEX IF NOT EXISTS idx_api_token_log_ts ON api_token_log(ts);

-- ────────────────────────────────────────────────────────────────────────
-- v_asin_daily_sales: TRUE daily units sold per ASIN, computed from
-- per-seller stock changes using the spec:
--   * Seller's stock decreased  → units_sold = (prev_stock - new_stock)
--   * Seller's stock increased  → restock, ignored (returns 0)
--   * Brand-new seller (no prior event) → excluded (first observation)
--   * Seller disappeared → the collector writes a synthetic stock=0 event
--     (Keepa's -1 marker, or P0-1 dead-offer detection in keepa_api_offers.py),
--     so the final stock-down to 0 is credited as sold here automatically.
-- Summed across all sellers for each (asin, change_date).
-- ────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS v_asin_daily_sales;
CREATE VIEW v_asin_daily_sales AS
WITH events AS (
    SELECT
        asin,
        seller_id,
        DATE(change_time) AS sale_date,
        stock,
        LAG(stock) OVER (PARTITION BY asin, seller_id ORDER BY change_time) AS prev_stock
    FROM fct_keepa_seller_history
    WHERE stock IS NOT NULL
),
per_seller_sales AS (
    SELECT
        asin,
        seller_id,
        sale_date,
        SUM(CASE WHEN prev_stock IS NOT NULL AND prev_stock > stock
                 THEN prev_stock - stock
                 ELSE 0
            END) AS units_sold,
        SUM(CASE WHEN prev_stock IS NOT NULL AND stock > prev_stock
                 THEN stock - prev_stock
                 ELSE 0
            END) AS units_restocked
    FROM events
    GROUP BY asin, seller_id, sale_date
)
SELECT
    asin,
    sale_date,
    SUM(units_sold)        AS units_sold,
    SUM(units_restocked)   AS units_restocked,
    COUNT(DISTINCT CASE WHEN units_sold > 0 THEN seller_id END) AS selling_seller_count
FROM per_seller_sales
GROUP BY asin, sale_date;

-- v_daily_sales: derived per-ASIN per-day units sold from FBA stock deltas.
-- When today's stock > yesterday's, a restock happened — we can't measure
-- sales in that window, so units_sold is NULL. When stock decreased, the
-- delta is treated as units sold (per *day* between snapshots, so weekly
-- gaps still produce a daily rate).
DROP VIEW IF EXISTS v_daily_sales;
CREATE VIEW v_daily_sales AS
WITH lagged AS (
    SELECT
        snapshot_date,
        asin,
        fba_stock,
        LAG(fba_stock)     OVER w AS fba_stock_prev,
        LAG(snapshot_date) OVER w AS snapshot_date_prev
    FROM fct_keepa_daily
    WINDOW w AS (PARTITION BY asin ORDER BY snapshot_date)
)
SELECT
    snapshot_date,
    asin,
    fba_stock,
    fba_stock_prev,
    snapshot_date_prev,
    CAST(julianday(snapshot_date) - julianday(snapshot_date_prev) AS INTEGER) AS days_since_prev,
    CASE
        WHEN fba_stock_prev IS NULL OR fba_stock IS NULL THEN NULL
        WHEN fba_stock > fba_stock_prev THEN NULL  -- restock detected
        ELSE fba_stock_prev - fba_stock
    END AS units_sold,
    CASE
        WHEN fba_stock_prev IS NULL OR fba_stock IS NULL THEN NULL
        WHEN fba_stock > fba_stock_prev THEN NULL
        WHEN julianday(snapshot_date) - julianday(snapshot_date_prev) < 1 THEN NULL
        ELSE (fba_stock_prev - fba_stock) * 1.0
             / (julianday(snapshot_date) - julianday(snapshot_date_prev))
    END AS units_sold_per_day
FROM lagged;

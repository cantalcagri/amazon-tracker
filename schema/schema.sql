-- ============================================================
-- Amazon Seller Tracker Schema
-- dim_product  — static product info (one row per child ASIN)
-- fct_asin_daily — daily snapshot per ASIN (fact table, append-only)
-- ============================================================

-- dim_product: one row per tracked child ASIN
CREATE TABLE IF NOT EXISTS dim_product (
    asin           TEXT PRIMARY KEY,
    parent_asin    TEXT,
    title          TEXT,
    brand          TEXT,
    variation_size  TEXT,
    variation_color TEXT,
    image_url       TEXT  -- Amazon CDN URL (from Keepa "Swatch Image" column)
);

-- fct_asin_daily: one row per ASIN per day
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
    offer_count     INTEGER          -- live offers seen on last fetch
);

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

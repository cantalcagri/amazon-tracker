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
    variation_color TEXT
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

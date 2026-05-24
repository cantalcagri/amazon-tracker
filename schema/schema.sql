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
    monthly_sold_date     TEXT,
    buy_box_price         REAL,
    buy_box_stock         INTEGER,
    oos_90d_pct           REAL,
    buy_box_seller        TEXT,
    pct_top_seller_30d    REAL,
    pct_top_seller_90d    REAL,
    is_fba_pct            REAL,
    raw_json              TEXT,
    PRIMARY KEY (snapshot_date, asin)
);
CREATE INDEX IF NOT EXISTS idx_fct_keepa_daily_asin ON fct_keepa_daily(asin);

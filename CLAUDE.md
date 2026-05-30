# Amazon Seller Tracker — Project Context

This file is read automatically by Claude Code (VS Code extension).
It gives Claude full context about this project so you don't have to re-explain every session.

---

## ⛔ ABSOLUTE RULES — NEVER VIOLATE

1. **NEVER run `pkill` or `kill` on "Google Chrome"** — this logs the user out of all accounts and destroys their session. It has happened and caused serious disruption. There is no exception.
2. **NEVER touch the user's real Chrome profile** at `~/Library/Application Support/Google/Chrome` — copying, deleting, or modifying it has broken their extensions before.
3. To launch Chrome for the CSV exporter: launch manually with `open -a "Google Chrome" --args --remote-debugging-port=9222` and let `keepa_viewer_export.py` attach via CDP. (The archived scraper used `create_driver()` + `--user-data-dir=pipeline/.chrome_profile`.)

---

## What this project does

Tracks Amazon product listings daily:
- Seller inventory levels (FBA and FBM sellers separately)
- Prices per seller (weighted average, min, max)
- Best Sellers Rank (BSR)
- Calculates **estimated daily units sold** by comparing inventory snapshots day over day

## Units sold logic (critical — don't change this)

```
Day 1:  S1=5, S2=3, S3=2
Day 2:  S1=4, S2=3, S4=2   (S3 gone, S4 is new)

Result:
  S1: 5→4 = 1 sold
  S2: 3→3 = 0 sold
  S3: gone = 2 sold (all remaining, went OOS)
  S4: NEW seller → excluded from day1→day2 calculation

Total minimum units sold = 3
```

New sellers on any given day are NEVER counted toward the previous day's sales.
If a seller disappears, ALL their remaining inventory counts as sold.

---

## Project structure

```
amazon-tracker/
├── CLAUDE.md                        ← you are here
├── requirements.txt
├── schema/
│   └── schema.sql                   ← SQLite schema + views (safe to re-run)
├── pipeline/                        ← ACTIVE pipeline (only 5 scripts run)
│   ├── keepa_viewer_export.py       ← Selenium → Keepa Viewer CSV (aggregate daily)
│   ├── keepa_csv_importer.py        ← imports that CSV → fct_keepa_daily
│   ├── keepa_api_offers.py          ← MAIN per-seller collector (API, --tick)
│   ├── replenishment.py             ← SHIP/HOLD/AVOID recommendation engine
│   ├── db.py                        ← shared SQLite helpers
│   ├── health_check.py              ← data-quality guardrails + seller housekeeping
│   ├── asins.txt                    ← legacy 252-ASIN subset (kept as fallback)
│   ├── .chrome_profile/             ← tracker's own Chrome profile (Keepa session)
│   └── .env                         ← secrets/config (not in git)
├── data/
│   └── asins.txt                    ← 1,073-ASIN master list (primary input)
├── dashboard/
│   ├── dashboard.py                 ← Streamlit analytics dashboard
│   └── explore.ipynb                ← Jupyter notebook for ad-hoc SQL queries
├── dash_app/                        ← Plotly Dash app (DuckDB-over-SQLite, zero-ETL)
│   ├── app.py                       ← run: DB_PATH=pipeline/amazon_tracker.db python3 dash_app/app.py
│   └── data.py                      ← DuckDB data-access layer (attaches SQLite read-only)
├── scripts/
│   └── daily_pipeline.sh            ← orchestration: runs the full daily pipeline
├── docs/                            ← reference + recovery runbooks
└── config/
    └── .env.template                ← copy this to pipeline/.env
```

> **Note:** The original Selenium AOD scraper and several earlier collector attempts
> have been deleted. The per-seller stock/price history now comes from the Keepa
> **API** (`keepa_api_offers.py`), not the browser AOD panel. The Chrome/AOD sections
> below are retained as historical reference only and do not describe code that still
> exists in this repo.

---

## ⚠️ CRITICAL: Chrome + Keepa Architecture (Read Before Touching create_driver)

### The problem
- Keepa extension must be **logged in** to inject stock numbers per seller in the AOD panel
- Keepa is installed in the user's real Chrome under **Profile 9** (cantalcagri@gmail.com)
- Selenium **cannot use the user's real Chrome profile** on macOS — Chrome blocks it

### What works (DO NOT REVERT)
- `create_driver()` uses `pipeline/.chrome_profile/` as `--user-data-dir`
- Keepa extension **files** are loaded read-only via `--load-extension` from Profile 9
- CDP mode (`--connect-port 9222`): attaches Selenium to the user's already-running Chrome
  - Launch Chrome with: `open -a "Google Chrome" --args --remote-debugging-port=9222`
  - Then run: `python browser_collector.py --asins-file asins.txt --connect-port 9222`
  - Keepa is already logged in this mode — no login needed

### What does NOT work (do not attempt again)
1. **CDP on default Chrome profile** — macOS blocks it
2. **`undetected-chromedriver`** — same macOS restriction
3. **Copying files from user's real Chrome profile** — destroyed the user's extensions previously, NEVER do this again
4. **`--user-data-dir` pointing to real Chrome** — fails when real Chrome is running

---

## Data model (SQLite star schema)

### dim_product — one row per ASIN
| Column | Description |
|---|---|
| `asin` | Child ASIN being tracked |
| `parent_asin` | Parent ASIN (variation family) — scraped from page JSON |
| `title` | Product title |
| `brand` | Brand name |
| `variation_size` | e.g. "Large", "X-Large" |
| `variation_color` | e.g. "Blue", "Black" |
| `variation_theme` | e.g. "Size_nameColor_name" |

**Removed columns (do not add back):** `subcategory`, `created_at`, `updated_at`, `category`, `rating`, `rating_count`
BSR category lives in `fact_bsr_snapshot.bsr_category` — not in dim_product.

### dim_seller — one row per seller + fulfillment
| Column | Description |
|---|---|
| `seller_name` | Seller display name |
| `seller_url` | Amazon seller page URL |
| `fulfillment` | "FBA" or "FBM" |
| `positive_pct` | % positive ratings |
| `first_seen_date` | Date first captured |

**Removed columns (do not add back):** `rating`, `rating_count`, `updated_at`

### fact_seller_snapshot — daily price + inventory (10-day rolling)
| Column | Description |
|---|---|
| `snapshot_date` | Date of capture |
| `product_id` | FK to dim_product |
| `seller_id` | FK to dim_seller |
| `price` | Listed price |
| `shipping_cost` | Shipping (0 if FREE) |
| `total_price` | Generated: price + shipping |
| `inventory` | Units in stock (from Keepa) |
| `is_buy_box_winner` | 1 if this is the buy-box seller |

**Removed columns (do not add back):** `snapshot_hour`, `raw_json`, `delivery_date`

### fact_bsr_snapshot — BSR per product per day (10-day rolling)
| Column | Description |
|---|---|
| `snapshot_date` | Date of capture |
| `product_id` | FK to dim_product |
| `bsr_rank` | Numeric rank |
| `bsr_category` | Category string |

**Removed columns (do not add back):** `snapshot_hour`

### fact_daily_units_sold — calculated sales (kept forever)
### agg_product_daily — daily summary per product (kept forever)

---

## ⚠️ CRITICAL: AOD Panel Parsing

### The "Unknown seller" problem — SOLVED
The AOD (All Offers Display) side panel has a **"See more" / "See less" toggle** on the buy-box block:
- **Before clicking "See more"**: Stock number is visible, seller name is HIDDEN
- **After clicking "See more" (→ "See less")**: Seller name visible, stock HIDDEN

**Fix implemented in `_expand_see_more()`:**
1. Read `innerText` BEFORE clicking → captures stock
2. Click "See more"
3. Read `innerText` AFTER clicking → captures seller name
4. Merge both texts → `_parse_offer_block()` gets both stock AND seller name

### The `element.text` returns empty problem — SOLVED
The AOD panel is a side drawer rendered off-screen. Selenium's `.text` returns `''` for off-screen elements.
**Fix:** Always use `driver.execute_script("return arguments[0].innerText;", element)`

### CAPTCHA detection — FIXED
Old code scanned full page source for the word "captcha" — caused false positives from browser extensions (SellerSprite, etc.).
**Fix:** Check URL, page title, and actual CAPTCHA form element only.

### Variation params required
Always use `?th=1&psc=1` in product URL — without it, Amazon shows parent ASIN with no real sellers.

---

## How to run

### Daily run (CDP mode — recommended, uses your logged-in Chrome)
```bash
# Step 1: Launch Chrome with remote debugging (do this once)
open -a "Google Chrome" --args --remote-debugging-port=9222

# Step 2: Run tracker
cd pipeline
python browser_collector.py --asins-file asins.txt --connect-port 9222

# Single ASIN
python browser_collector.py --asin B0F8QT93ZK --connect-port 9222

# Schedule every 6 hours
python browser_collector.py --asins-file asins.txt --connect-port 9222 --scheduler --interval 6
```

### Dashboard
```bash
cd dashboard
DB_PATH=../pipeline/amazon_tracker.db streamlit run dashboard.py
# Open http://localhost:8501

# Or use Jupyter notebook
jupyter notebook explore.ipynb
```

---

## Environment variables (pipeline/.env)

| Variable | Description |
|---|---|
| `CHROME_PROFILE_PATH` | Path to tracker's own Chrome profile (not the real one) |
| `DB_PATH` | SQLite file path (defaults to `amazon_tracker.db`) |
| `KEEPA_USERNAME` | `trendyzone.sw` |
| `KEEPA_PASSWORD` | In .env file |

---

## Tech stack

| Layer | Tool |
|---|---|
| Language | Python 3.10+ |
| Browser automation | Selenium 4 + webdriver-manager |
| Database | SQLite (file: `amazon_tracker.db`) |
| Dashboard | Streamlit + Plotly |
| Scheduling | `schedule` library |
| Config | `python-dotenv` |

---

## Common fixes for future agents

**"Unknown seller" in logs**
→ The "See more" toggle hides seller name. `_expand_see_more()` handles this — do not revert.

**"inv=None" for buy-box seller**
→ Keepa injects "Stock\nN" which may be in the pre-expand text. Check `_expand_see_more()` merges both texts.

**"table X has no column Y" error**
→ Schema was rewritten — removed many columns. Check schema.sql for current columns before adding any INSERT.
→ Removed for good: `snapshot_hour`, `raw_json`, `delivery_date`, `subcategory`, `created_at`, `updated_at`, `category`, `rating`, `rating_count`

**"table X already exists" error**
→ All CREATE statements in schema.sql must use `CREATE TABLE IF NOT EXISTS`

**"disk I/O error" on sqlite3**
→ WAL journal mode fails on some macOS filesystems. Uses `journal_mode=DELETE` now — do not change back to WAL.

**"CAPTCHA detected" causing 5-min wait on normal pages**
→ Fixed — CAPTCHA detection now checks URL/title/form element only, not full page source.

**Seller inventory shows None for all sellers**
→ Keepa is not logged in. Use `--connect-port 9222` to attach to your already-logged-in Chrome.

**Script got blocked by Amazon**
→ Increase `DELAY_BETWEEN_PAGES` and `DELAY_BETWEEN_ASINS` in `browser_collector.py`

---

## What NOT to change without understanding the impact

1. `calculate_units_sold()` — core sales logic
2. The `UNIQUE` constraints in `schema.sql` — prevent duplicate snapshots
3. `RETENTION_DAYS = 10` — affects how far back sales can be calculated
4. The `is_new_seller` flag — removing this inflates sales numbers
5. `create_driver()` / `create_driver_cdp()` — see Chrome + Keepa architecture above
6. `_expand_see_more()` — captures both stock and seller name from AOD toggle
7. `driver.execute_script("return arguments[0].innerText;", el)` — do NOT use `.text` for AOD elements

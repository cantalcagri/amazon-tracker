# Amazon Seller Tracker

Track BSR, per-seller inventory, prices, and daily units sold for Amazon ASINs
using **Keepa** as the data source. Produces a replenishment recommendation
(SHIP_NOW / HOLD / AVOID_BUY / WATCH) per ASIN.

See [CLAUDE.md](CLAUDE.md) for the full architecture and data model.

---

## Two data pipelines

| Path | Script | Writes | Cost |
|---|---|---|---|
| **A. Daily aggregate** | `keepa_viewer_export.py` → `keepa_csv_importer.py` | `fct_keepa_daily` (BSR, buy-box, offer counts, aggregate FBA stock) | 0 API tokens (Selenium CSV export) |
| **B. Per-seller** | `keepa_api_offers.py --tick` | `fct_keepa_seller_history`, `dim_keepa_seller` | ~4 Keepa tokens/ASIN |

Units sold is derived in SQL view `v_asin_daily_sales` from per-seller stock
deltas (per-seller decrease = sold; new sellers excluded; a seller going to
stock 0 / disappearing credits its last stock as sold). See "Units sold" below.

---

## Project structure

```
amazon-tracker/
├── data/asins.txt              ← THE ASIN list (one per line, # = comment)
├── schema/
│   ├── schema.sql              ← tables + views (safe to re-run)
│   └── migrations/             ← idempotent one-off migrations
├── pipeline/
│   ├── keepa_api_offers.py     ← Path B: per-seller API collector
│   ├── keepa_viewer_export.py  ← Path A: Selenium → Keepa Viewer CSV
│   ├── keepa_csv_importer.py   ← Path A: CSV → fct_keepa_daily
│   ├── replenishment.py        ← recommendation engine
│   ├── health_check.py         ← data-quality checks + retention purge
│   └── db.py                   ← shared sqlite helpers
├── dashboard/dashboard.py      ← Streamlit dashboard (primary UI)
├── dash_app/                   ← newer Plotly Dash + DuckDB UI (partial)
├── scripts/daily_pipeline.sh   ← orchestrated daily run (launchd/cron)
└── docs/                       ← Keepa reference + handoff notes
```

---

## Quick start

```bash
pip install -r requirements.txt

# Configure secrets
cp config/.env.template pipeline/.env
# edit pipeline/.env → set KEEPA_API_KEY

# Initialize the database
python -c "import sys; sys.path.insert(0,'pipeline'); import db; db.init_db()"
```

### Path A — daily aggregate snapshot (Keepa Viewer CSV)

Needs a Chrome window logged into keepa.com, with remote debugging on:

```bash
open -a "Google Chrome" --args --remote-debugging-port=9222
cd pipeline
python keepa_viewer_export.py --asins-file ../data/asins.txt
```

This attaches to Chrome via CDP, builds the Keepa Viewer URL for all ASINs,
clicks **Export → CSV**, saves to `pipeline/keepa_exports/`, and imports into
`fct_keepa_daily` (re-running the same day overwrites — idempotent).

Flags: `--no-import` (download only), `--keep-tab`, `--asins-file PATH`.

Re-import an existing CSV without Chrome:

```bash
python keepa_csv_importer.py keepa_exports/keepa_viewer_<timestamp>.csv
```

### Path B — per-seller API collector

```bash
cd pipeline
python keepa_api_offers.py --status        # token balance + queue stats (0 tokens)
python keepa_api_offers.py --tick          # one batch of stalest ASINs (for cron)
python keepa_api_offers.py --seller-names  # resolve names for new sellers (1 token each)
```

For 5,000 ASINs run `--tick` continuously (e.g. cron every 15 min, 24/7); a
short daily burst can't keep the queue fresh — see CLAUDE.md "token economics".

### Health & housekeeping

```bash
python health_check.py                 # data-quality report (exit 1 if any fail)
python health_check.py --purge --vacuum  # enforce 90-day retention, shrink DB
```

### Orchestrated daily run

```bash
bash scripts/daily_pipeline.sh
# Deploy on macOS via scripts/com.amazontracker.daily.plist (launchd)
```

### Dashboard

```bash
# Streamlit (primary)
DB_PATH=pipeline/amazon_tracker.db streamlit run dashboard/dashboard.py
# Dash + DuckDB (newer, partial)
DB_PATH=pipeline/amazon_tracker.db python3 dash_app/app.py
```

---

## Units sold

`v_asin_daily_sales` (canonical), computed from `fct_keepa_seller_history`:

```
day1:  S1=5, S2=3, S3=2
day2:  S1=4, S2=3, S4=2   (S3 gone, S4 is new)

  S1: 5→4 = 1 sold
  S2: 3→3 = 0 sold
  S3: gone → last stock (2) credited as sold
  S4: new seller → EXCLUDED from this day (first observation)
Total = 3 units (a lower bound; restocks between observations can mask sales)
```

ASINs without per-seller data yet fall back to `v_daily_sales` (aggregate FBA
stock deltas).

---

## Multi-marketplace / Costco

`dim_product` carries `marketplace` (default `amazon_us`) plus `item_uid` and
`gtin` — the marketplace-neutral identity for joining Amazon listings to Costco
or other marketplaces. The Amazon ASIN is not a stable cross-marketplace key.

---

## Keepa vs scraping

Path A still uses Selenium to export the Keepa Viewer CSV (free, but needs a
logged-in Chrome). Path B uses the Keepa HTTP API (paid tokens, headless,
deterministic). The API can also serve the Path-A fields directly — see the
"retire Selenium" note in the audit if you want a fully headless pipeline.

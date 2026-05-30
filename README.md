# Amazon Seller Tracker Pipeline

Track BSR, seller inventory, prices, and daily units sold for any Amazon ASIN.

---

## Project Structure

```
amazon-tracker/
├── schema/
│   └── schema.sql          ← All database tables (star schema)
├── pipeline/
│   └── collector.py        ← Data collection + units-sold logic
├── dashboard/
│   └── dashboard.py        ← Streamlit analytics dashboard
├── config/
│   └── .env.template       ← Environment config template
└── requirements.txt
```

---

## Quick Start (VS Code)

### 1. Install Python dependencies

Open the terminal in VS Code (`Ctrl+`` ` ``) and run:

```bash
cd amazon-tracker
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp config/.env.template pipeline/.env
# Edit pipeline/.env with your settings (optional for basic use)
```

### 3. Run first pipeline snapshot

```bash
cd pipeline
python collector.py --asin B0CV3CDPTK
```

This will:
- Create `amazon_tracker.db` (SQLite)
- Scrape the product page + seller offers
- Save BSR snapshot
- Calculate daily units sold (needs 2 days before sales appear)

### 4. Run again next day (or schedule it)

```bash
# Run every 6 hours automatically:
python collector.py --asin B0CV3CDPTK --scheduler --interval 6
```

### 5. View the dashboard

```bash
cd dashboard
DB_PATH=../pipeline/amazon_tracker.db streamlit run dashboard.py
```

Open http://localhost:8501 in your browser.

---

## Data Model (Star Schema)

```
dim_product ──────────┐
dim_seller  ──────────┤──→ fact_seller_snapshot  (raw, 10-day rolling)
dim_date    ──────────┤──→ fact_bsr_snapshot     (raw, 10-day rolling)
                      └──→ fact_daily_units_sold (calculated, keep forever)
                           agg_product_daily     (summary, keep forever)
```

### Retention Policy
| Table | Retention |
|---|---|
| `fact_seller_snapshot` | 10 days (raw snapshots purged automatically) |
| `fact_bsr_snapshot` | 10 days |
| `fact_daily_units_sold` | Forever (already aggregated) |
| `agg_product_daily` | Forever |

---

## Units Sold Calculation Logic

```
day1:  S1=5, S2=3, S3=2
day2:  S1=4, S2=3, S4=2  (S3 gone, S4 is new)

Calculation:
  S1: 5→4 = 1 sold ✓
  S2: 3→3 = 0 sold ✓
  S3: 5→(gone) = 2 sold (all remaining) ✓
  S4: new seller → EXCLUDED from day1→day2 calc ✓

Total sold: 1 + 0 + 2 = 3 units minimum
```

---

## Amazon Scraping vs Keepa API

| | Web Scraping | Keepa API |
|---|---|---|
| Cost | Free | ~$20/mo |
| Reliability | Can break, gets blocked | Stable |
| BSR history | Last run only | Years of history |
| Legality | Against Amazon ToS | ✓ |
| Setup | Just run it | Add `KEEPA_API_KEY` to `.env` |

**Recommendation:** Start with scraping locally. For production, add Keepa.

To use Keepa:
```bash
python collector.py --asin B0CV3CDPTK --keepa
```

---

## Tracking Multiple ASINs

```bash
# Run pipeline for multiple ASINs:
python collector.py --asin B0CV3CDPTK --scheduler
# (Edit collector.py start_scheduler call to add more ASINs)
```

Or edit the bottom of `collector.py`:
```python
start_scheduler(["B0CV3CDPTK", "B0CL5Z7VFR"], interval_hours=6)
```

---

## Dashboard Features

- **BSR Trend** — daily rank chart (inverted axis, lower = better)
- **Price Trend** — weighted avg / min / max per day
- **Seller Count** — FBA vs FBM stacked bar
- **Units Sold** — per seller breakdown table with color coding
  - 🟡 New sellers (excluded from sales calc)
  - 🟢 Sellers with units sold
- **Raw Snapshots** — full expandable table

---

## Database Queries (useful SQL)

```sql
-- Today's sellers with prices
SELECT * FROM v_latest_seller_snapshot;

-- Daily sales summary
SELECT * FROM v_daily_sales_summary;

-- BSR history
SELECT snapshot_date, bsr_rank FROM fact_bsr_snapshot
WHERE product_id=1 ORDER BY snapshot_date;

-- Manual cleanup
DELETE FROM fact_seller_snapshot WHERE snapshot_date < date('now','-10 days');
```

---

## Keepa Viewer Export (automated CSV download → SQLite)

Pulls the full Keepa **Product Viewer** CSV for a list of ASINs and imports it into the cumulative `fct_keepa_daily` table — no manual clicks.

**What it captures per ASIN per day** (cumulative, never purged):
- BSR current + 30-day average
- Buy-box price, stock, seller name
- 90-day OOS%
- Monthly sold estimate
- `% Top Seller` for 30 and 90 days
- Full raw row dumped to `raw_json` so nothing is ever lost

### One-time setup

1. Launch an **isolated** Chrome window (does NOT touch your regular Chrome):
   ```bash
   open -na "Google Chrome" --args \
     --user-data-dir="$(pwd)/pipeline/.chrome_profile" \
     --remote-debugging-port=9222
   ```
2. In that new window, sign into [keepa.com](https://keepa.com) once. The session is saved in `pipeline/.chrome_profile/` so you only do this once.

### Daily run

```bash
cd pipeline
python keepa_viewer_export.py --asins-file ../data/asins.txt
```

That single command:
1. Attaches to the isolated Chrome via CDP (port 9222)
2. Builds the Keepa viewer URL with all ASINs hash-encoded
3. Clicks **Export → CSV** automatically
4. Saves the CSV to `pipeline/keepa_exports/keepa_viewer_<timestamp>_<count>asins.csv`
5. Imports rows into `fct_keepa_daily` (re-running the same day overwrites — idempotent)

Useful flags:
- `--no-import` — skip the SQLite import (just save the CSV)
- `--keep-tab` — leave the Keepa viewer tab open after export
- `--asins-file PATH` — override the input file (default: `pipeline/asins.txt`)

### View the data

```bash
DB_PATH=pipeline/amazon_tracker.db streamlit run dashboard/dashboard.py
```

The dashboard has two views:
- **📊 Overview** — KPIs (total ASINs, avg BSR, OOS counts), daily coverage chart, latest-snapshot table
- **🔎 Per-ASIN drilldown** — BSR trend with 30-day average overlay, buy-box price + stock chart, raw snapshot history

### Re-importing an existing CSV (no Chrome needed)

```bash
cd pipeline
python keepa_csv_importer.py keepa_exports/keepa_viewer_20260524_011911_1073asins.csv
```

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Couldn't connect to 127.0.0.1:9222` | Isolated Chrome isn't running. Re-run the one-time setup command. |
| Table loads but Export button times out | Keepa changed selectors. Update `.tool__export` / `#exportSubmit` in `keepa_viewer_export.py`. |
| Only a few rows imported | Check `--asins-file` points at the real master list (not the 252-row test subset). |
| `no such column: product_id` in dashboard | Old `dashboard.py` cached. Restart Streamlit. |


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

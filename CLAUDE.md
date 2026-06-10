# Amazon Seller Tracker — Project Context

This file is read automatically by Claude Code (VS Code extension).
It gives Claude full context so you don't have to re-explain every session.

> **History note:** an earlier version of this project used a Selenium + Keepa
> Chrome-extension AOD scraper (`browser_collector.py`, `keepa_collector.py`).
> **Those scripts were deleted.** If you find docs/memories referencing
> `browser_collector`, `_expand_see_more`, `dim_seller`, `fact_seller_snapshot`,
> or `KEEPA_USERNAME/PASSWORD`, they are stale — ignore them. The current system
> is 100% Keepa-based (one Selenium CSV step + the Keepa HTTP API).

---

## What this project does

Tracks Amazon product listings over time using **Keepa** as the data source:
- BSR, buy-box price/stock, FBA/FBM offer counts, aggregate FBA stock (daily)
- **Per-seller** stock & price change history (from the Keepa API)
- Derives **daily units sold** per ASIN from per-seller stock deltas
- Produces a **replenishment recommendation** (SHIP_NOW / HOLD / AVOID_BUY / WATCH)

The long-term goal is to scale to **5,000+ ASINs**, link some products to a
**Costco pipeline**, and add **other marketplaces** — so the schema is being
made marketplace-neutral (see "Multi-marketplace" below).

## Units sold logic (critical — don't silently change the numbers)

Units sold is computed in SQL view `v_asin_daily_sales` from per-seller stock
change events:
- A seller's stock **decreases** by N → **N units sold**.
- A seller's stock **increases** → restock, ignored (counts toward restocked, not sold).
- A **brand-new** seller (no prior event) → excluded from that day (first observation).
- A seller **disappears** (Keepa emits stock `-1`, parsed to `0`) → their last
  known stock counts as sold. See `v_asin_daily_sales` for the exact handling.

Summed across all sellers per (asin, day). This is a **lower bound**: restocks
between two Keepa observations can mask sales.

---

## Two data pipelines (both write `pipeline/amazon_tracker.db`)

| Path | Script | Writes | Cost | Notes |
|---|---|---|---|---|
| **A. Daily aggregate** | `keepa_viewer_export.py` → `keepa_csv_importer.py` | `fct_keepa_daily` (1 row/ASIN/day) | 0 API tokens | Selenium drives a logged-in Chrome to export Keepa's Product Viewer CSV |
| **B. Per-seller** | `keepa_api_offers.py --tick` | `fct_keepa_seller_history`, `dim_keepa_seller` | ~4 tokens/ASIN | Keepa HTTP API; stalest-first queue in `asin_api_state` |

Path A is the source of BSR/price/offer-count/aggregate-stock charts and the
replenishment engine inputs. Path B is the source of true per-seller stock and
the canonical units-sold figure.

---

## Project structure

```
amazon-tracker/
├── CLAUDE.md                       ← you are here
├── README.md
├── requirements.txt
├── data/
│   └── asins.txt                   ← THE ASIN list (one per line, # = comment)
├── schema/
│   ├── schema.sql                  ← SQLite schema + views (safe to re-run)
│   └── migrations/                 ← idempotent one-off migrations
├── pipeline/
│   ├── keepa_api_offers.py         ← Path B: per-seller API collector (--tick/--status/--seller-names)
│   ├── keepa_viewer_export.py      ← Path A: Selenium → Keepa Viewer CSV export
│   ├── keepa_csv_importer.py       ← Path A: CSV → fct_keepa_daily
│   ├── replenishment.py            ← SHIP/HOLD/AVOID/WATCH recommendation engine
│   ├── health_check.py             ← data-quality checks + retention purge + housekeeping
│   ├── db.py                       ← shared sqlite helpers (get_conn, init_db, upsert_product)
│   └── .env                        ← KEEPA_API_KEY, DB_PATH (gitignored)
├── dashboard/
│   ├── dashboard.py                ← Streamlit dashboard (primary UI today)
│   └── explore.ipynb               ← ad-hoc SQL notebook
├── dash_app/                       ← newer Plotly Dash + DuckDB UI (read-only, partial)
│   ├── app.py
│   └── data.py
├── scripts/
│   ├── daily_pipeline.sh           ← orchestrated daily run (launchd/cron)
│   └── com.amazontracker.daily.plist
└── docs/
    ├── keepa_api_reference.md      ← Keepa field + token reference
    └── HANDOFF.md                  ← session handoff notes
```

---

## Data model (SQLite — `pipeline/amazon_tracker.db`)

### dim_product — one row per ASIN
`asin` (PK), `marketplace`, `parent_asin`, `title`, `brand`, `variation_size`,
`variation_color`, `image_url`, `gtin`, `item_uid`.
`marketplace` defaults to `'amazon_us'`. `item_uid`/`gtin` are the
marketplace-neutral identity used to join across marketplaces and to the Costco
pipeline (see below).

### fct_keepa_daily — daily aggregate snapshot (Path A, never purged)
One row per (`snapshot_date`, `asin`). Typed columns for charted fields
(`sales_rank_current`, `buy_box_price`, `buy_box_stock`, `fba_stock`,
`fba_offers`, `fbm_offers`, `total_offers`, `oos_90d_pct`, `monthly_sold_num`,
…) plus `raw_json` holding the full CSV row so no Keepa column is ever lost.

### fct_keepa_seller_history — per-seller change events (Path B)
One row per (`asin`, `seller_id`, `change_time`). Columns: `stock`,
`price_cents`, `shipping_cents`, `is_fba`, `is_prime`. Source of
`v_asin_daily_sales`. Retention is enforced two ways: old events are skipped at
insert (`HISTORY_RETENTION_DAYS`), AND `health_check.py --purge` deletes rows
older than the cutoff (insert-time skipping alone does not shrink stored rows).

### dim_keepa_seller — seller_id → name/rating
`seller_id` (PK), `seller_name`, `rating_pct`, `review_count`, `is_amazon`,
`updated_at`. Names cost 1 token each via `/seller`; resolved lazily for newly
seen sellers, so we pay once per seller.

### asin_api_state — per-ASIN fetch bookkeeping
Drives the stalest-first queue. `last_fetched_at`, `last_attempted_at`,
`fetch_success`, `error_msg`, `offer_count`, `offers_successful`,
`offers_truncated` (1 when Keepa saw more offers than the `offers=20` cap
returned — per-seller data for that ASIN is partial).

### pipeline_runs — one row per orchestrated daily run
Audit trail; drives the dashboard "last run" indicator.

### Views
- **`v_asin_daily_sales`** — canonical per-ASIN daily units sold (per-seller deltas + seller-disappeared credit). **Use this.**
- `v_daily_sales` — fallback units sold from aggregate `fba_stock` deltas (only for ASINs without per-seller data yet).

### Deprecated
- `fct_asin_daily` — output of the deleted AOD scraper. Nothing writes it. Do not build on it.

---

## Multi-marketplace / Costco identity

The Amazon ASIN is **not** a stable cross-marketplace key. To join an Amazon
listing to its Costco equivalent (or amazon.ca/.de later), use `item_uid` /
`gtin` on `dim_product`:
- `marketplace` distinguishes `amazon_us`, `amazon_ca`, `costco_us`, …
- `item_uid` is the canonical product (same physical product across marketplaces).
- `gtin` (UPC/EAN) bridges to external sources like Costco.

When adding a marketplace, set `marketplace` on inserts and never assume ASIN
uniqueness across marketplaces.

---

## How to run

```bash
# Path A — daily aggregate snapshot (needs a logged-in Chrome on :9222)
open -a "Google Chrome" --args --remote-debugging-port=9222
cd pipeline
python keepa_viewer_export.py --asins-file ../data/asins.txt

# Path B — per-seller API collector (dual-key: two loops, sharded queue)
python keepa_api_offers.py --loop --slot 1 --n-slots 2   # key 1, half the catalog
python keepa_api_offers.py --loop --slot 2 --n-slots 2   # key 2, other half
python keepa_api_offers.py --tick           # one batch of stalest ASINs (for cron)
python keepa_api_offers.py --status          # token balance + queue stats (0 tokens)
python keepa_api_offers.py --seller-names    # resolve names for new sellers (1 token each)
# Normally supervised: scripts/watchdog.sh (one instance per slot) + the daily
# CSV via scripts/csv_scheduler.sh — all started by the Login Item
# ~/start_amazon_tracker.command (launchd is TCC-blocked for Desktop paths).

# Health + housekeeping
python health_check.py                       # data-quality report (exit 1 if any fail)
python health_check.py --purge --vacuum      # enforce retention, shrink DB

# Orchestrated daily run (launchd/cron) — see scripts/daily_pipeline.sh
bash scripts/daily_pipeline.sh

# Dashboard (Streamlit — primary)
DB_PATH=pipeline/amazon_tracker.db streamlit run dashboard/dashboard.py
# Dashboard (Dash + DuckDB — newer, partial)
DB_PATH=pipeline/amazon_tracker.db python3 dash_app/app.py
```

---

## Environment variables (pipeline/.env)

| Variable | Description |
|---|---|
| `KEEPA_API_KEY` | Keepa HTTP API key, slot 1 (Path B). |
| `KEEPA_API_KEY_2` | Second Keepa key, slot 2 (doubles throughput). |
| `KEEPA_N_SLOTS` | Total key slots (2). MUST be set or a loop fetches the whole catalog and wastes the other key's work. CLI `--n-slots` overrides. |
| `DB_PATH` | SQLite file path (defaults to `pipeline/amazon_tracker.db`). |

---

## Keepa token economics (Path B)

- Plan: **5 tokens/min refill = 7,200/day**, burst cap **300** (over-refill is lost).
- ~4 tokens/ASIN when batched 100/call (`offers=20` +6, `stock=1` +3, bulk discount).
- 5,000 ASINs ≈ 20K tokens ≈ ~3 days per full sweep — so each ASIN's *latest*
  fetch can be ~3 days stale, but Keepa's `stockCSV` backfills the intra-gap
  history, so resolution is not lost, only latency.
- **Throughput note:** a tick spends ~400 and needs ≥150 to start, so it can only
  run ~once every ~80 min of refill. To keep 5,000 ASINs moving you need a
  **continuous** cron (e.g. every 15 min, 24/7), not a short daily burst.

---

## Tech stack

| Layer | Tool |
|---|---|
| Language | Python 3.10+ |
| Data source | Keepa (HTTP API + Product Viewer CSV) |
| Browser automation (Path A only) | Selenium 4 + webdriver-manager |
| Database | SQLite (`amazon_tracker.db`), WAL mode |
| Dashboards | Streamlit + Plotly (primary); Dash + DuckDB (newer) |
| Scheduling | launchd / cron |
| Config | python-dotenv |

---

## Gotchas / don't-break list

1. `v_asin_daily_sales` — core units-sold logic. Verify against a test DB before changing.
2. `UNIQUE`/`PRIMARY KEY` constraints in `schema.sql` — prevent duplicate snapshots.
3. `HISTORY_RETENTION_DAYS = 90` in `keepa_api_offers.py` — affects how far back sales can be computed. Purge job uses the same cutoff.
4. The DB must live on a **local APFS path**, never iCloud/Dropbox/network — that's what caused the old "disk I/O error", not WAL itself.
5. `offers=20` returns only the top 20 offers; ASINs with more sellers are partial (`offers_truncated=1`). Don't treat per-seller totals as complete for those.
6. Re-running `schema.sql` is non-destructive. Schema *migrations* live in `schema/migrations/` and are idempotent.
7. Secrets: rotate the Keepa API key and any GitHub PAT embedded in the git remote URL.
8. API `/product` calls MUST pass `stats=90` (0 extra tokens). Without it,
   `history=0` fetches return no BSR and the daily rows go in NULL (caused the
   June 2-9 2026 BSR gap).
9. `snapshot_date` is the LOCAL calendar date everywhere (collector + CSV
   importer). Don't switch either side to UTC — evening fetches would split
   one real day across two rows.
10. `last_fetched_at` etc. are ISO strings with `T`; SQLite `datetime('now')`
   emits a space. Same-day string comparisons between the two are off by hours
   — use `julianday()` for time math, not string `>=`.

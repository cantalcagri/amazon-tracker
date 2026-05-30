# Handoff — paste this into the next chat

> Copy everything below into a new conversation so the next Claude has full context.

---

## Project
**Amazon Tracker** at `/Users/cagri/Desktop/amazon-tracker` — tracks Amazon ASINs daily via Keepa, with three data sources feeding a Streamlit dashboard + replenishment recommendation engine.

## Where things live

| Path | What |
|---|---|
| `pipeline/keepa_viewer_export.py` | Selenium → Keepa Viewer CSV → `fct_keepa_daily` (1 row/ASIN/day) |
| `pipeline/keepa_csv_importer.py` | CSV importer for the above |
| **`pipeline/keepa_api_offers.py`** | **API per-seller collector** — `--tick` mode, batches 100 ASINs, parses `stockCSV`/`offerCSV` into `fct_keepa_seller_history` |
| `pipeline/replenishment.py` | SHIP_NOW / HOLD / AVOID_BUY / WATCH classifier |
| `pipeline/browser_collector.py` | AOD scraper (legacy, doesn't currently inject stock — Keepa extension not in isolated Chrome) |
| `pipeline/.env` | `KEEPA_API_KEY` lives here (gitignored) |
| `dashboard/dashboard.py` | Streamlit dashboard, 2 views: 🔎 Single product trend / 🎯 Replenishment |
| `dashboard/explore.ipynb` | Ad-hoc SQL exploration notebook |
| `schema/schema.sql` | All tables + views |
| `docs/keepa_api_reference.md` | Full Keepa API field reference |

## Database (`pipeline/amazon_tracker.db`)

Schema-of-record:
- `dim_product(asin, brand, title, image_url, parent_asin, variation_size, variation_color)` — **1,089 ASINs**
- `fct_keepa_daily(asin, snapshot_date, bsr, buy_box_price, fba_stock, fba_offers, fbm_offers, ...)` — **3 days × 1089 = ~3,200 rows** from CSV pipeline
- `fct_keepa_seller_history(asin, seller_id, change_time, stock, price_cents, is_fba, is_prime)` — per-seller change events. **Trimmed to last 90 days. 27 ASINs, 55 active sellers, 840 stock events, 1861 price events.** Source: API.
- `dim_keepa_seller(seller_id, seller_name, rating_pct, review_count, is_amazon)` — **376 sellers total, 55 with names** (Zappos, 6pm, ShoeMall, Amazon, etc.)
- `asin_api_state(asin, last_fetched_at, fetch_success)` — drives stalest-first queue
- `fct_asin_daily` — legacy AOD scraper output (units_sold computed via per-seller delta logic, but stock always NULL because Keepa extension not in isolated Chrome)
- `v_daily_sales` view — derived sales from aggregate fba_stock deltas (fct_keepa_daily based)
- **`v_asin_daily_sales` view — TRUE per-seller-derived sales (`fct_keepa_seller_history` based) ← USE THIS**

## Keepa account state
- API key: in `pipeline/.env` (originally pasted in chat history — **rotate it** at https://keepa.com/#!api when convenient)
- **5 tokens/min refill, 300 burst cap** = 7,200/day budget
- Bulk discount confirmed: ~4–6 tokens per ASIN when batched in 100
- One full sync of 5K ASINs ≈ 20–30K tokens (~3 days of refill); weekly cadence sustainable

## What the dashboard shows (per Single Product Trend page)

Sidebar: **Brand** filter → cascades into **ASIN** dropdown (🏪 N indicator on ASINs with per-seller data; sorted first). Days slider 7–90 default 90.

Page sections:
1. **Hero** — image, title, 5 KPI tiles (BSR, Buy-Box $, Total offers, FBA stock, FBA sellers) with day-over-day deltas
2. **Ranking** — BSR chart
3. **Competition & seller mix** — offer count, top-seller dominance, FBA share
4. **Inventory & availability** — stock, OOS%
5. **Pricing & sales velocity** — prices, monthly sold, daily velocity
6. **True daily units sold** ← NEW: uses `v_asin_daily_sales`, KPIs + bars/restocks chart
7. **Per-seller inventory** — multi-line stock chart (one per seller) + sellers table; can drill into a single seller for their stock/price chart

Second view: **🎯 Replenishment** — recommendation engine with sidebar threshold sliders + click-to-drill on table rows.

## Running things

```bash
# Daily aggregate snapshot from Keepa Viewer CSV (Chrome window needed)
cd pipeline
python keepa_viewer_export.py --asins-file ../data/asins.txt

# Per-seller API collector — one batch of stalest ASINs
python keepa_api_offers.py --tick

# Backfill seller names for newly-discovered sellers (1 token each, smart "only-missing")
python keepa_api_offers.py --seller-names

# Status (no tokens spent)
python keepa_api_offers.py --status

# Dashboard
cd ..
DB_PATH=pipeline/amazon_tracker.db python3 -m streamlit run dashboard/dashboard.py --server.port=8502
```

## What's working and recent

- ✅ Full per-seller API pipeline built + tested: 27 ASINs done, ~840 stock events captured
- ✅ Per-seller daily sales computed via `v_asin_daily_sales` (confirmed: B07RS94LGR sold 937 units in 30d, B00RNFPU3W Kirkland boxer briefs sold 211)
- ✅ Seller names populated for 55 active sellers (Zappos, 6pm, etc.)
- ✅ History capped to 90 days (DB went from 24K rows → 2.5K)
- ✅ Dashboard collapsed to 2 views per user request (was 4)
- ✅ Brand filter cascades to ASIN, 🏪 indicators show seller-data availability

## What's pending / next steps

1. **Set up cron for `--tick` every 15 min** (snippet in `pipeline/keepa_api_offers.py` docstring) — not yet enabled
2. **Backfill remaining 1062 ASINs** with per-seller data (will take ~7 days at current cadence; cron handles it automatically)
3. **Wire `v_asin_daily_sales` into the recommendation engine** (`replenishment.py`) — currently uses aggregate `v_daily_sales`; should prefer the per-seller view when available
4. **Hot-list priority refinement** — currently uses "FBA stock < 20" as hot signal; should use SHIP_NOW/HOLD recommendations once the engine flips to per-seller data
5. **Optionally**: auto-run `--seller-names` from cron every 4 hours (mostly 0-cost; only fires for new sellers)

## Open design questions to revisit

- Whether to upsert per-seller-derived `units_sold` back into `fct_asin_daily` so existing queries that reference it (none yet, but possible) keep working
- How to handle "seller disappeared" case in the sales view (currently only detects stock-down; a seller silently dropping off isn't credited as "all remaining sold" yet)
- Whether to switch the `replenishment.py` velocity source automatically when per-seller is available, or expose a sidebar toggle

## Most recent git state

- `main` branch is up to date with `258839f → bbc816d` already pushed
- **Uncommitted changes this session** (significant; not yet pushed): per-seller API collector additions to `pipeline/keepa_api_offers.py` (added `--seller-names`, 90-day cap), new view in `schema/schema.sql`, dashboard restructure in `dashboard/dashboard.py`, notebook additions, `docs/HANDOFF.md` itself

## Token balance at handoff
- Used 55 of 278 tokens this session on seller names → ~223 tokens left at last check
- Refills 5/min, caps at 300

## ⚠️ Repo gotchas (don't burn the next agent)
- **NEVER** `pkill Google Chrome` or touch the user's real Chrome profile at `~/Library/Application Support/Google/Chrome` (per `CLAUDE.md` rules). The isolated profile lives at `pipeline/.chrome_profile/`.
- The git remote URL has the user's GitHub PAT embedded — visible in `git remote -v`. User has been told to rotate.
- Anaconda's streamlit is broken — use `/Library/Developer/CommandLineTools/.../python3 -m streamlit` (verified).

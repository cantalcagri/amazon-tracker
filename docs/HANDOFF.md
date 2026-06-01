# Handoff — paste into the next chat

Copy this whole file into a new conversation so the next session has full context.

---

## Project
**Amazon Tracker** at `/Users/cagri/Desktop/amazon-tracker` — tracks 5,087 Amazon
ASINs via Keepa. Two data sources feed one SQLite DB + a public dashboard.

## ⚠️ Hard rules (do not violate)
- **NEVER use the user's Claude-account Gmail (`cantalcagri@gmail.com`) for Chrome,
  Keepa, Amazon, or anything in this pipeline.** It must stay disconnected.
- The Keepa account for this pipeline is **`trendyzone.sw`** in an **isolated Chrome
  profile** at `pipeline/.chrome_keepa/` (separate from the user's main Chrome).
- Keepa API key lives in `pipeline/.env` (gitignored). It was pasted in chat — **rotate it**.

## Architecture (cost-optimal: free CSV + paid API)
| Source | Script | Fills | Cost |
|---|---|---|---|
| **CSV** (Keepa Viewer, Selenium) | `keepa_viewer_export.py` → `keepa_csv_importer.py` | BSR, prices, offers, OOS%, monthly sold for ALL ASINs | 0 tokens |
| **API** (`--loop`) | `keepa_api_offers.py` | per-seller stock/price history → true units sold | ~4–7 tok/ASIN |

The API also writes BSR/price to `fct_keepa_daily` for free (from `salesRankCurrent`
+ offerCSV). **New ASINs** get `history=1` (full 90-day BSR backfill, ~9 tok);
**known ASINs** get `history=0` (today only). See `docs/DATA_SOURCING_PLAN.md`.

## What's running right now (background, nohup)
- **Collector loop** (`keepa_api_offers.py --loop`) — token-aware, fires a 100-ASIN
  batch whenever balance ≥150, sleeps precisely for refill. ~1,000 ASINs/day.
- **Watchdog** (`scripts/watchdog.sh`) — restarts the loop within 15 min if it dies.
- **Dashboard** (Streamlit, `.venv`, port 8502) + **Cloudflare tunnel** (public URL).
- Login Item `~/start_amazon_tracker.command` starts the watchdog at login.
  `scripts/start_dashboard.sh` starts dashboard + tunnel + Keepa Chrome.

Check state: `cd pipeline && /usr/bin/python3 keepa_api_offers.py --status`
Dashboard URL: `bash scripts/dashboard_url.sh`

## Progress (as of 2026-06-01)
- 5,087 ASINs in catalog (22 dead ones removed from `tam_liste.xlsx` import)
- ~1,100+ ASINs fetched for per-seller data; full sweep ~4–5 days
- CSV imported once today → BSR/price/OOS for 4,848 ASINs (1 day of history so far)
- Token usage tracked in `api_token_log`; ~6.7 tok/ASIN real cost (apparel = many sellers)

## Key facts
- Tokens: 5/min refill = 7,200/day, burst cap 300. Loop spends at ~100% of refill,
  **wastes nothing** (never idles at 300). Going negative is fine/expected.
- **NO tokens wasted** unless the loop stops (watchdog covers crashes).
- Dashboard: dark theme, `.streamlit/config.toml`, password REMOVED, line charts,
  per-seller stock has Source + Seller-name dropdowns, aggregated default.

## Pending / not yet done
1. **Dual API key (2x speed)** — code is ready. User has a 2nd Keepa account+key.
   To enable: add `KEEPA_API_KEY_2=<key>` to `pipeline/.env`, then:
   `cp scripts/com.amazontracker.loop2.plist ~/Library/LaunchAgents/ && launchctl load ...`
   Also set `KEEPA_N_SLOTS=2` for slot 1 (in watchdog env / its plist). Queue shards
   by ASIN hash so the two keys never overlap. **NOT YET ENABLED.**
2. **Daily CSV automation** — wired into `daily_pipeline.sh` but needs the Keepa Chrome
   open. `start_dashboard.sh` auto-launches it. Not yet scheduled via launchd/cron.
3. **Historical BSR** — new-ASIN backfill is coded but the existing 5,087 already-fetched
   ASINs won't re-trigger it (they're "known"). To force a full historical backfill of
   everything, would need a one-time `--history` pass over the whole catalog.

## Most recent git state
- `main` @ `e8723bb` — all work committed. `9d941bc` was the last pushed to origin;
  `27a2b07` + `e8723bb` are local commits **not yet pushed**.

## Gotchas
- Use `/usr/bin/python3` for the collector, `.venv/bin/python` for dashboard + Selenium.
- macOS blocks launchd from Desktop paths (TCC) — that's why we use nohup + Login Item.
- `open -a "Google Chrome" --args --remote-debugging-port=9222` does NOT work if Chrome
  is already running. Must fully quit first, OR use the dedicated profile launch (which
  start_dashboard.sh does): `--user-data-dir=pipeline/.chrome_keepa`.

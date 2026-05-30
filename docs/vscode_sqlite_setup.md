# Browse the tracker DB in VS Code (no code)

Two extensions get you a visual, point-and-click view of every table.

---

## Option A — "SQLite Viewer" (read-only, simplest)

**Install:**
1. Open VS Code → **Extensions** panel (`Cmd+Shift+X`)
2. Search: **"SQLite Viewer"** by **Florian Klampfer**
3. Click **Install**

**Use:**
1. In the file explorer, navigate to `pipeline/amazon_tracker.db`
2. Right-click → **Open With → SQLite Viewer**
3. Side panel shows every table; click one to see all rows
4. Click any column header to sort
5. Search bar filters rows live

Perfect for: quick browsing, "what's in this table", verifying recent imports.

---

## Option B — "SQLite" by alexcvzz (read + write, more powerful)

**Install:**
1. Extensions → search **"SQLite"** by **alexcvzz**
2. Install

**Use:**
1. `Cmd+Shift+P` → **"SQLite: Open Database"** → pick `pipeline/amazon_tracker.db`
2. New **SQLITE EXPLORER** panel appears in the file sidebar
3. Expand → see all tables, click to preview
4. Right-click any table → **"Show Table"** opens a result grid
5. `Cmd+Shift+P` → **"SQLite: New Query"** → write any SQL → `Cmd+Shift+Q` to run

Perfect for: writing custom queries, ad-hoc analysis, exporting subsets.

---

## Quick orientation — what's in each table

| Table | One row per | Use it for |
|---|---|---|
| `dim_product` | ASIN | product master: title, brand, image, parent, color, size |
| `fct_keepa_daily` | (ASIN, day) | daily snapshot from Keepa CSV: BSR, buy-box, stock, OOS%, etc. |
| `fct_keepa_seller_history` | (ASIN, seller, change_time) | per-seller stock & price events from API |
| `dim_keepa_seller` | seller ID | seller name + rating lookup |
| `asin_api_state` | ASIN | last_fetched_at for the API queue |
| `fct_asin_daily` | (ASIN, day) | AOD scraper output: sellers JSON, units_sold |
| `v_daily_sales` (view) | (ASIN, day) | derived daily units sold from FBA stock delta |

---

## Or use the Jupyter notebook

`dashboard/explore.ipynb` is the ad-hoc SQL exploration notebook. Open it in VS Code with the Jupyter extension installed.

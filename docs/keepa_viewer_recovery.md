# Recovery runbook — Keepa Viewer CSV export (`keepa_viewer_export.py`)

This is the **most fragile** part of the pipeline. It drives the Keepa website
through a real Chrome via Selenium/CDP, so it breaks whenever Keepa changes
their UI (CSS class names, button IDs, the export popup, the ag-grid table).
The Keepa **API** path (`keepa_api_offers.py`) is unaffected by UI changes —
only this CSV exporter is at risk.

When it breaks, you'll see one of these in the log:
- `TimeoutError: Product table did not load within timeout`
- `TimeoutError: CSV download did not complete in time`
- A Selenium `NoSuchElementException` / `ElementClickInterceptedException`

---

## Pre-flight (do this first — usually the real cause)

1. **Chrome is running with the debug port:**
   ```bash
   open -a "Google Chrome" --args --remote-debugging-port=9222
   ```
   ⛔ Do NOT `pkill` Chrome to restart it — that logs the user out everywhere
   (see `CLAUDE.md` absolute rules). Quit it normally if needed.

2. **You're logged into keepa.com** in that Chrome window. Open
   https://keepa.com and confirm you see your account, not a login wall.

3. **Your Keepa subscription is active.** The viewer/export is a paid feature;
   an expired plan silently disables export.

4. **The ASIN list isn't too large.** The viewer URL encodes every ASIN in the
   hash fragment. Thousands of ASINs can exceed practical URL limits — if so,
   split `asins.txt` and run in chunks.

If all four are fine and it still fails, the UI changed. Continue below.

---

## The selectors this script depends on

All live in `pipeline/keepa_viewer_export.py`. When Keepa redesigns, one of
these stopped matching:

| Step | Selector / signal | Code location |
|---|---|---|
| Detect table loaded | `.ag-row, .product-row, tr[role=row]` (count > 0) | `wait_for_table()` |
| Open export popup | `.tool__export .trigger` (clicked via JS) | `trigger_export()` |
| Submit the export | `#exportSubmit` (button id) | `trigger_export()` |
| CSV format | default radio (2nd `name=format`) — not touched | `trigger_export()` |
| Download landed | new `*.csv` (not `*.crdownload`) in `keepa_exports/` | `wait_for_download()` |

---

## How to re-derive a broken selector

1. In the **logged-in Chrome**, open the viewer manually with a few ASINs and
   let the table render.
2. Open DevTools (⌥⌘I) → Elements.
3. Find the element that moved:
   - **Export button:** look for the toolbar control that opens the export
     popup. Note its class/id. Update the `CSS_SELECTOR` in `trigger_export()`.
   - **Submit button:** open the popup, inspect the confirm/export button. If
     `#exportSubmit` is gone, find the new id or a stable selector.
   - **Table rows:** if `.ag-row` is gone, inspect a product row and grab its
     new container class; add it to the `querySelectorAll` list in
     `wait_for_table()` (keep the old ones too — harmless if absent).
4. Prefer **clicking via JS** (`driver.execute_script("arguments[0].click()", el)`)
   over native `.click()` — Keepa overlays intercept native pointer events.
   This is why the code already JS-clicks.
5. Re-run with a tiny list to confirm:
   ```bash
   cd pipeline
   printf 'B00RNFPU3W\n' > /tmp/one.txt
   python3 keepa_viewer_export.py --asins-file /tmp/one.txt --keep-tab --no-import
   ```
   `--keep-tab` leaves the viewer tab open so you can inspect; `--no-import`
   skips the DB write so you can iterate on just the download.

---

## Last-resort fallbacks (if the UI fight isn't worth it)

1. **Manual export, automated import.** Export the CSV by hand from the Keepa
   site, drop it in `pipeline/keepa_exports/`, then import it directly:
   ```bash
   cd pipeline
   python3 keepa_csv_importer.py keepa_exports/<that_file>.csv
   ```
   The importer is UI-independent and rarely breaks (it fuzzy-matches column
   headers, see `FIELD_PATTERNS`).

2. **Lean harder on the API path.** Most fields in `fct_keepa_daily` (BSR,
   buy-box price, offers, FBA stock) are also obtainable from the Keepa
   `/product` API that `keepa_api_offers.py` already calls. If the viewer stays
   broken, consider extending the API collector to populate `fct_keepa_daily`
   too, and retire the Selenium exporter. (Not done yet — only worth it if the
   UI breaks repeatedly.)

---

## Don't do these

- ❌ `pkill`/`kill` on Google Chrome (logs the user out everywhere).
- ❌ Point Selenium at the user's real Chrome profile
  (`~/Library/Application Support/Google/Chrome`) — macOS blocks it and it has
  corrupted extensions before.
- ❌ `undetected-chromedriver` — same macOS profile restriction; already tried.

# Data Sourcing Plan — free CSV vs paid API

**Goal:** capture the maximum useful data for 5,000+ ASINs while spending the
fewest Keepa tokens. The rule: **anything the free Product Viewer CSV can give,
get it from the CSV. Spend API tokens ONLY on what the CSV cannot provide.**

---

## The one thing only the API can give

| Data | CSV? | API? |
|---|---|---|
| **Per-seller stock time-series** (which seller holds how many units, over time) | ❌ no | ✅ `offers=20&stock=1` |
| **Per-seller price/shipping time-series** | ❌ no | ✅ same call |

Everything else below is in the **free** CSV. So the API runs in **per-seller-only
mode** (default, `history=0`) at **~4 tokens/ASIN**, and feeds
`fct_keepa_seller_history` → the true units-sold view. That's the whole reason we
pay tokens.

> Earlier the collector ran with `history=1` (~9 tok/ASIN) to also fill BSR/price.
> That was wasteful — the CSV has those for free. Fixed: API default is now
> `history=0`, and it skips the `fct_keepa_daily` write so it can't clobber the
> richer CSV rows. Use `--history` only if you want a fully headless pipeline
> with no CSV at all.

---

## Token cost is measured, not guessed

Keepa bills by **data returned** (offer count + history depth), so cost/ASIN
varies a lot (we saw 3–10 tok/ASIN in tests). Every API call is now logged to
`api_token_log`. Check the real blended rate any time:

```bash
cd pipeline && python keepa_api_offers.py --status
# → "Avg cost: X tok/ASIN (~Y tok / 100 ASINs)" over the last 7 days
```

Budget math at the measured rate (default no-history, ~4 tok/ASIN):
- Refill 5/min = 7,200 tok/day → **~1,800 ASIN-fetches/day**
- 5,000 ASINs → **full per-seller sweep ≈ 2.8 days**, then it cycles.

---

## CSV column plan (what to export in the Keepa Product Viewer)

Configure the Viewer to export the columns below. The importer
(`keepa_csv_importer.py`) already resolves all Tier-1 names via fuzzy matching
and stores the full row in `raw_json` so nothing is ever lost.

### Tier 1 — ingest as typed columns (already wired)

| Keepa CSV column | DB field | Why |
|---|---|---|
| ASIN | `dim_product.asin` | key |
| Parent ASIN | `parent_asin` | variation family |
| Title, Brand, Color, Size | title/brand/variation_* | identity |
| Swatch Image | `image_url` | thumbnail |
| **Product Codes: GTIN / UPC / EAN** | `gtin / upc / ean` | **← Costco / cross-marketplace bridge** |
| PartNumber | `part_number` | identity |
| Sales Rank: Current, 30 days avg., Display Group | `sales_rank_*`, `display_group` | demand |
| Buy Box: Current / Stock / Buy Box Seller / Is FBA | `buy_box_*`, `is_fba_pct` | buy-box state |
| Buy Box: % Top Seller 30/90 | `pct_top_seller_*` | competition lock |
| Buy Box: % Amazon 30/90 | `pct_amazon_30d/90d` | Amazon-as-competitor |
| Buy Box: 90 days OOS | `oos_90d_pct` | availability |
| Total Offer Count | `total_offers` | competition |
| New FBA / FBM Offer Count: Current | `fba_offers / fbm_offers` | channel mix (TOTAL, not buy-box-eligible) |
| New Offer Count: Current | `new_offer_count` | competition |
| New, 3rd Party FBA: Current / Stock | `fba_price / fba_stock` | aggregate FBA |
| New, 3rd Party FBM: Current | `fbm_price` | aggregate FBM |
| Monthly Sold (Last Known) | `monthly_sold` / `monthly_sold_num` | Keepa sales estimate |
| Monthly Sold (Peak), Bought in past month | `monthly_sold_peak`, `bought_past_month` | sales signal |
| Reviews: Rating / Rating Count | `rating / rating_count` | review velocity |
| Referral Fee %, FBA Pick&Pack Fee | `referral_fee_pct`, `fba_pick_pack_fee` | profitability |
| Item: Weight (g) | `weight_g` | shipping cost |
| Return Rate | `return_rate` | risk |

### Tier 2 — export too (kept in `raw_json`; promote to typed columns when needed)
List Price · Item & Package dimensions · Categories (Root/Sub/Tree) · Listed
since · Tracking since · Variation ASINs/Count · Lowest FBA/FBM Seller ·
Competitive Price Threshold · Coupons · Subscribe & Save · Strikethrough Price.

### Tier 3 — don't bother (irrelevant to this model)
eBay · Collectible · Refurbished · Used (all conditions) · Trade-In · Lightning/
Warehouse Deals · A+ Content · Videos · Description/Features · book fields
(Author/Edition/Pages). Selecting fewer columns also keeps the CSV smaller.

---

## How the two pipelines combine

```
                 ┌─────────────────────────────┐
  FREE  CSV ───▶ │ fct_keepa_daily             │  BSR, prices, offers, OOS,
  (Viewer)       │  + dim_product (GTIN/UPC…)  │  monthly sold, fees, ratings
                 └─────────────────────────────┘
                                │  joined by asin
                 ┌─────────────────────────────┐
  PAID  API ───▶ │ fct_keepa_seller_history    │  per-seller stock/price events
  (~4 tok/ASIN)  │  → v_asin_daily_sales       │  → TRUE units sold
                 └─────────────────────────────┘
```

- **CSV cadence:** daily (or whenever you export). Owns all aggregate fields.
- **API cadence:** continuous `--loop`, self-paced on tokens. Owns per-seller.
- They never fight: the API in default mode does not write `fct_keepa_daily`.

---

## Go-live checklist (5,000 ASINs)

1. Put the 5,000-ASIN list in `data/asins.txt`, then:
   `cd pipeline && python load_asins.py` (add `--prune` to drop removed ASINs).
2. Export the CSV with the Tier-1 (+Tier-2) columns selected → import:
   `python keepa_csv_importer.py <export>.csv` (or automate via
   `keepa_viewer_export.py`).
3. Start the per-seller loop (cost-optimal, no history):
   `cp scripts/com.amazontracker.loop.plist ~/Library/LaunchAgents/ && \`
   `launchctl load ~/Library/LaunchAgents/com.amazontracker.loop.plist`
4. Watch real cost: `python keepa_api_offers.py --status`.
5. Schedule the CSV export + import to run daily (launchd), and
   `health_check.py --purge` weekly to enforce 90-day retention.

---

## Costco / second marketplace hook

`dim_product` now carries `gtin`, `upc`, `ean`, `item_uid`, and `marketplace`.
Once the CSV populates GTIN/UPC, link an Amazon ASIN to a Costco item by shared
GTIN/UPC, or assign a common `item_uid`. No schema change needed when Costco or
amazon.ca is added — just a new `marketplace` value and rows that share the code.

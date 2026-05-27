# Keepa API — what we can pull

Reference for **what fields Keepa provides** and **what we currently use**.
Endpoint: `GET https://api.keepa.com/product`.

---

## Token economics

| Item | Cost |
|---|---|
| Base product call | **1 token / ASIN** |
| Add `offers=20` (last 20 offers per product) | **+6 tokens / ASIN** |
| Add `stock=1` (include stock per offer) | **+3 tokens / ASIN** |
| Add `history=1` (full price history CSV) | **+5 tokens / ASIN** |
| Add `rating=1` (product rating/review history) | **+1 token / ASIN** |
| Add `update=0` (force fresh, no cache) | usually +1 token |

**Bulk discount:** real per-ASIN cost is much lower in batches of 100. Empirically:
- 3 ASINs/call: ~8.5 tokens/ASIN
- 100 ASINs/call: ~4 tokens/ASIN

→ Always batch up to Keepa's max of 100 ASINs per call.

### Your plan's refill rate

- 5 tokens/min = **7,200 tokens/day** of refill
- Burst cap = 300 (tokens earned above 300 are forfeit)
- → Keep usage continuous to avoid wasting refill

---

## What we currently call

```python
GET /product?key=XXX&domain=1&asin=ASIN1,ASIN2,...&offers=20&stock=1&history=0
```

| Param | Value | Why |
|---|---|---|
| `domain` | `1` | Amazon.com |
| `offers` | `20` | Last 20 active/historical offers (covers most ASINs fully) |
| `stock` | `1` | **Critical** — gives per-seller stock & change history |
| `history` | `0` | We get aggregate price history elsewhere; skip to save tokens |

---

## Response: top-level product fields (selection)

| Field | Type | What it is |
|---|---|---|
| `asin` | string | The product |
| `title` | string | Product title |
| `brand` | string | Brand |
| `manufacturer` | string | Manufacturer (often == brand) |
| `parentAsin` | string | Variation parent |
| `variationCSV` | string | CSV list of child variation ASINs |
| `salesRankReference` | int | Top-level category for the rank |
| `salesRanks` | dict | All category ranks (e.g. {"7141123011": [...time series], "7147441011": [...]}) |
| `salesRankReferenceHistory` | array | Time-series of which top-rank category was used |
| `imagesCSV` | string | Comma-separated image filenames (build URL with `m.media-amazon.com/images/I/<file>`) |
| `categoryTree` | list of dicts | Full Amazon category breadcrumb |
| `eanList` / `upcList` | list | UPCs / EAN codes |
| `numberOfItems` | int | Pack size if multipack |
| `monthlySold` | int | Monthly Sold (last known) — Amazon's published "X+ bought in past month" |
| `buyBoxSellerIdHistory` | array | Alternating `[keepa_time, seller_id]` — who has had the buy box |
| `liveOffersOrder` | array | `offerId`s currently live (sorted by buy-box probability) |
| `offers` | array of dicts | The per-seller offer detail — see below |
| `offersSuccessful` | int | How many offers Keepa successfully captured |
| `buyBoxEligibleOfferCounts` | array | Counts of buy-box-eligible offers split by condition/fulfillment |
| `lastUpdate` | int | Keepa minute of last product refresh |

---

## Response: per-offer fields (`offers[]`)

This is the **gold** — per-seller, per-ASIN data with time-series stock & price.

| Field | Type | What it is |
|---|---|---|
| `offerId` | int | Keepa's internal offer ID (stable across calls) |
| `sellerId` | string | **Amazon merchant ID** — stable, the right primary key |
| `sellerName` | string | Seller display name (sometimes blank, fetch via `seller` endpoint to fill) |
| `condition` | int | `1`=new, `2`=used, `3`=collectible, etc. |
| `isAmazon` | bool | True if seller IS Amazon directly (always wins buy box, infinite stock) |
| `isFBA` | bool | Fulfilled by Amazon |
| `isPrime` | bool | Prime-eligible (often == isFBA but not always) |
| `isPrimeExcl` | bool | Prime-exclusive offer |
| `isMAP` | bool | Minimum advertised price restriction |
| `isPreorder` | bool | Preorder offer |
| `isWarehouseDeal` | bool | Amazon Warehouse refurbished |
| `isShippable` | bool | Can ship to standard US addresses |
| `minOrderQty` | int | Minimum order quantity |
| `shipsFromChina` | bool | Origin flag |
| `coupon` | float | Active coupon discount (%) |
| `couponHistory` | array | Time-series of coupon changes |
| `firstSeen` | int | Keepa minute the offer was first observed |
| `lastSeen` | int | Most recent observation |
| `lastStockUpdate` | int | Most recent stock observation |
| `stockCSV` | array | **Time-series of stock**: `[time1, stock1, time2, stock2, ...]` ⭐ |
| `offerCSV` | array | **Time-series of price+shipping**: `[time, price_cents, shipping_cents, time, ...]` ⭐ |
| `condition` | int | See above |
| `conditionDescription` | string | Free-text condition notes |

### Parsing `stockCSV` and `offerCSV`

**Keepa time format**: minutes since 2011-01-01 UTC.
```python
unix_seconds = (keepa_minute + 21564000) * 60
```

**stockCSV `[t1, s1, t2, s2, …]`** → each pair = "at time t, stock changed to s units".
Example: `[7934812, 25, 7937232, 24, 7945406, 23]` means:
- At keepa-min 7934812 (2026-05-19 14:52 UTC) stock was 25
- At 7937232 (2026-05-21 07:12 UTC) it dropped to 24 → **1 unit sold**
- At 7945406 (2026-05-27 00:46 UTC) it dropped to 23 → **another 1 unit sold**

**offerCSV `[t, p, ship, t, p, ship, …]`** → price+shipping triples.

---

## Other endpoints we could use

| Endpoint | Purpose | Token cost | Why we'd use it |
|---|---|---|---|
| `/token` | Check current balance, refill rate, cap | **0 tokens** | Pre-flight check |
| `/seller` | Lookup seller name, rating, total reviews by `sellerId` | 1/seller | Fill missing `sellerName` |
| `/category` | Full category tree | 1/category | Map salesRanks to category names |
| `/bestsellers` | Top N ASINs in a category | varies | Hunt new opportunities |
| `/search` | Keyword search | varies | Discovery |
| `/product/finder` | Filter products by attributes | varies | Advanced discovery |
| `/lightning` | Lightning deals feed | 1/call | Promo monitoring |
| `/deals` | Camelizer-style "good deal" alerts | 1/deal | Buy-trigger automation |

---

## What we use today vs what we could add

| Data | Source today | Could pull from API too? |
|---|---|---|
| Title, brand, image | dim_product (from CSV) | ✅ `/product` returns same |
| BSR daily | fct_keepa_daily (from CSV) | ✅ + finer time resolution if `history=1` |
| Buy-box price/stock | fct_keepa_daily (from CSV) | ✅ + change-by-change history |
| Aggregate FBA stock | fct_keepa_daily (from CSV) | ✅ + per-seller breakdown |
| Per-seller stock & price | **fct_keepa_seller_history (NEW from API)** | ✅ what we just built |
| Monthly Sold (Keepa estimate) | fct_keepa_daily (~5% coverage) | ✅ same; coverage limited by Amazon |
| Top-seller dominance % | fct_keepa_daily | ✅ same |
| Buy-box winner identity over time | not stored yet | 🆕 `buyBoxSellerIdHistory` is in API |
| Coupon history per offer | not stored yet | 🆕 `couponHistory` is in API |
| Variation siblings | partially in dim_product | 🆕 `variationCSV` lists all sibling ASINs |
| Full Amazon category tree | not stored | 🆕 `categoryTree` + `/category` endpoint |

---

## Sample response (one offer, abridged)

```json
{
  "asin": "B0FVTT3XX1",
  "title": "Mondetta Women's Fleece Full Zip Jacket ...",
  "brand": "Mondetta",
  "monthlySold": null,
  "liveOffersOrder": [0, 5, 14, 27],
  "offers": [
    {
      "offerId": 14,
      "sellerId": "ALEHKFC9FPA50",
      "sellerName": "unique-treasure-finds",
      "condition": 1,
      "isFBA": true,
      "isPrime": true,
      "isAmazon": false,
      "firstSeen": 7892952,
      "lastSeen": 8101800,
      "lastStockUpdate": 8101800,
      "stockCSV": [7892952, 35, 7945406, 33, 8101800, 30],
      "offerCSV": [7892952, 1499, 0, 7937232, 1293, 0]
    },
    ...
  ]
}
```

→ From this one offer's `stockCSV` we can see seller ALEHKFC9FPA50:
- Started at 35 units
- Dropped to 33 → **2 sold**
- Dropped to 30 → **3 more sold**

Total: 5 units sold over the observation window — measured precisely from real Keepa observations.

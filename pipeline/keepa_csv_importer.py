"""
Import a Keepa Product Viewer CSV export into the tracker DB.

Behavior:
  - Upserts dim_product (title, brand) for each ASIN in the CSV
  - Inserts one row per ASIN into fct_keepa_daily for today's date
    (REPLACE on conflict so re-running the same day is idempotent)
  - Cumulative: fct_keepa_daily is never purged

Keepa column names are verbose and occasionally shift between exports
("Sales Rank: Current", "Sales Rank: 30 days avg.", "Buy Box \U0001f69a: Current", ...).
We resolve each typed field via fuzzy regex match on the header row, and
dump the full row as JSON into raw_json so nothing is ever lost.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

from db import get_conn, init_db, upsert_product


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("keepa_import")


# Header-matching patterns. First regex that matches a column header (case-
# insensitive) wins. Keep these loose so they survive Keepa column-name
# tweaks ("Sales Rank: Current" vs "Sales Rank - Current" etc.).
FIELD_PATTERNS: dict[str, list[str]] = {
    "asin":               [r"^asin$"],
    "title":              [r"^title$", r"product title"],
    "brand":              [r"^brand$", r"manufacturer"],
    "parent_asin":        [r"parent\s*asin"],
    "color":              [r"^color$"],
    "size":               [r"^size$"],
    "image_url":          [r"swatch\s*image", r"^image\s*url$", r"image$"],
    "sales_rank_current": [r"^sales\s*rank.*current"],
    "sales_rank_30d_avg": [r"sales\s*rank.*30\s*day", r"30\s*day.*sales\s*rank"],
    "display_group":      [r"display\s*group", r"product\s*group"],
    "monthly_sold":       [r"monthly\s*sold(?!\s*date)"],
    "monthly_sold_date":  [r"monthly\s*sold\s*date"],
    "buy_box_price":      [r"^buy\s*box:\s*current", r"buy\s*box.*price"],
    "buy_box_stock":      [r"^buy\s*box:\s*stock"],
    "oos_90d_pct":        [r"90\s*day.*oos", r"oos.*90"],
    "buy_box_seller":     [r"buy\s*box\s*seller"],
    "pct_top_seller_30d": [r"%?\s*top\s*seller.*30"],
    "pct_top_seller_90d": [r"%?\s*top\s*seller.*90"],
    "is_fba_pct":         [r"^buy\s*box:\s*is\s*fba", r"%\s*fba"],
    # Offer counts: match the TOTAL "New FBA/FBM Offer Count: Current" — NOT the
    # "Buy Box Eligible Offer Counts: New FBA" subset (that's a different, smaller
    # number and appears earlier in the header row, so it used to win by mistake).
    "fba_offers":         [r"new\s*fba\s*offer\s*count.*current"],
    "fbm_offers":         [r"new\s*fbm\s*offer\s*count.*current"],
    "new_offer_count":    [r"^new\s*offer\s*count.*current"],
    "total_offers":       [r"^total\s*offer\s*count"],
    "fba_stock":          [r"3rd\s*party\s*fba.*stock"],
    "fba_price":          [r"3rd\s*party\s*fba.*current"],
    "fbm_price":          [r"3rd\s*party\s*fbm.*current"],
    # Product identity codes — the bridge to Costco / other marketplaces.
    "gtin":               [r"product\s*codes:\s*gtin", r"^gtin$"],
    "upc":                [r"product\s*codes:\s*upc", r"^upc$"],
    "ean":                [r"product\s*codes:\s*ean", r"^ean$"],
    "part_number":        [r"part\s*number", r"partnumber"],
    # Profitability + physical
    "referral_fee_pct":   [r"referral\s*fee\s*%", r"referral\s*fee(?!.*based)"],
    "fba_pick_pack_fee":  [r"fba\s*pick.*pack\s*fee", r"pick\s*&?\s*pack"],
    "weight_g":           [r"^item:\s*weight", r"^weight\s*\(g\)"],
    "return_rate":        [r"return\s*rate"],
    # Reviews + sales signals
    "rating":             [r"^reviews:\s*rating(?!\s*count)", r"^rating$"],
    "rating_count":       [r"^reviews:\s*rating\s*count$", r"^rating\s*count$"],
    "bought_past_month":  [r"bought\s*in\s*past\s*month"],
    "monthly_sold_peak":  [r"monthly\s*sold\s*\(peak\)"],
    "pct_amazon_30d":     [r"buy\s*box:\s*%\s*amazon\s*30"],
    "pct_amazon_90d":     [r"buy\s*box:\s*%\s*amazon\s*90"],
}


def resolve_columns(headers: list[str]) -> dict[str, str]:
    """Map our internal field name -> actual CSV column header."""
    resolved: dict[str, str] = {}
    for field, patterns in FIELD_PATTERNS.items():
        for h in headers:
            h_norm = h.strip()
            if any(re.search(p, h_norm, re.IGNORECASE) for p in patterns):
                resolved[field] = h
                break
    return resolved


def to_int(val) -> int | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("#", "").replace("%", "")
    if not s or s in {"-", "?", "n/a", "N/A"}:
        return None
    # Strip suffix like "Top 0.57%" or "50+"
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def parse_magnitude(val) -> int | None:
    """Parse Keepa magnitude strings like '50+', '1K+', '2.5K', '3M' into an int.

    Keepa's "Monthly Sold" column uses K/M suffixes and a trailing '+'
    ('1K+ bought in past month'). A naive int-regex turns '1K+' into 1 — a
    ~1000x undercount that then poisons the velocity fallback. We expand the
    suffix here. Returns None for blanks/dashes.
    """
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("+", "").replace("#", "")
    if not s or s.lower() in {"-", "?", "n/a"}:
        return None
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([kKmM]?)\s*$", s)
    if not m:
        # Unexpected format — fall back to the first integer found.
        m2 = re.search(r"-?\d+", s)
        return int(m2.group(0)) if m2 else None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(round(float(m.group(1)) * mult))


def to_float(val) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("$", "").replace("%", "")
    if not s or s in {"-", "?", "n/a", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        # Last digits-with-decimal in the string
        m = re.search(r"-?\d+\.?\d*", s)
        return float(m.group(0)) if m else None


def to_text(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s not in {"-", "?"} else None


def detect_delimiter(sample: str) -> str:
    # Keepa CSVs are usually comma-separated but sometimes semicolon
    # (locale-dependent). Sniff a single line.
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


def import_csv(csv_path: Path, snapshot: date | None = None) -> int:
    snapshot = snapshot or date.today()
    text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    delim = detect_delimiter(text.splitlines()[0] if text else "")
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    headers = reader.fieldnames or []
    if not headers:
        log.error("CSV has no header row: %s", csv_path)
        return 0

    cols = resolve_columns(headers)
    log.info("Resolved %d/%d fields from %d CSV columns", len(cols), len(FIELD_PATTERNS), len(headers))
    if "asin" not in cols:
        log.error("CSV has no ASIN column — cannot import. Headers: %s", headers[:10])
        return 0

    init_db()
    inserted = 0
    skipped = 0
    with get_conn() as conn:
        for row in reader:
            asin = to_text(row.get(cols["asin"]))
            if not asin or not re.match(r"^[A-Z0-9]{10}$", asin):
                skipped += 1
                continue

            def g(field):  # helper: resolved cell text for a field, or None
                return to_text(row.get(cols.get(field, "")))

            upsert_product(
                conn, asin,
                title=g("title"), brand=g("brand"),
                parent_asin=g("parent_asin"),
                variation_color=g("color"), variation_size=g("size"),
                image_url=g("image_url"),
                gtin=g("gtin"), upc=g("upc"), ean=g("ean"),
                part_number=g("part_number"),
                weight_g=to_float(row.get(cols.get("weight_g", ""))),
                referral_fee_pct=to_float(row.get(cols.get("referral_fee_pct", ""))),
                fba_pick_pack_fee=to_float(row.get(cols.get("fba_pick_pack_fee", ""))),
            )

            monthly_sold_raw = to_text(row.get(cols.get("monthly_sold", "")))
            values = {
                "snapshot_date":      snapshot.isoformat(),
                "asin":               asin,
                "sales_rank_current": to_int(row.get(cols.get("sales_rank_current", ""))),
                "sales_rank_30d_avg": to_int(row.get(cols.get("sales_rank_30d_avg", ""))),
                "display_group":      to_text(row.get(cols.get("display_group", ""))),
                "monthly_sold":       monthly_sold_raw,
                "monthly_sold_num":   parse_magnitude(monthly_sold_raw),
                "monthly_sold_date":  to_text(row.get(cols.get("monthly_sold_date", ""))),
                "buy_box_price":      to_float(row.get(cols.get("buy_box_price", ""))),
                "buy_box_stock":      to_int(row.get(cols.get("buy_box_stock", ""))),
                "oos_90d_pct":        to_float(row.get(cols.get("oos_90d_pct", ""))),
                "buy_box_seller":     to_text(row.get(cols.get("buy_box_seller", ""))),
                "pct_top_seller_30d": to_float(row.get(cols.get("pct_top_seller_30d", ""))),
                "pct_top_seller_90d": to_float(row.get(cols.get("pct_top_seller_90d", ""))),
                "is_fba_pct":         to_float(row.get(cols.get("is_fba_pct", ""))),
                "fba_offers":         to_int(row.get(cols.get("fba_offers", ""))),
                "fbm_offers":         to_int(row.get(cols.get("fbm_offers", ""))),
                "total_offers":       to_int(row.get(cols.get("total_offers", ""))),
                "fba_stock":          to_int(row.get(cols.get("fba_stock", ""))),
                "fba_price":          to_float(row.get(cols.get("fba_price", ""))),
                "fbm_price":          to_float(row.get(cols.get("fbm_price", ""))),
                "new_offer_count":    to_int(row.get(cols.get("new_offer_count", ""))),
                "rating":             to_float(row.get(cols.get("rating", ""))),
                "rating_count":       to_int(row.get(cols.get("rating_count", ""))),
                "bought_past_month":  parse_magnitude(to_text(row.get(cols.get("bought_past_month", "")))),
                "monthly_sold_peak":  parse_magnitude(to_text(row.get(cols.get("monthly_sold_peak", "")))),
                "pct_amazon_30d":     to_float(row.get(cols.get("pct_amazon_30d", ""))),
                "pct_amazon_90d":     to_float(row.get(cols.get("pct_amazon_90d", ""))),
                "return_rate":        to_float(row.get(cols.get("return_rate", ""))),
                "raw_json":           json.dumps(row, ensure_ascii=False),
            }

            conn.execute(
                """
                INSERT OR REPLACE INTO fct_keepa_daily (
                    snapshot_date, asin, sales_rank_current, sales_rank_30d_avg,
                    display_group, monthly_sold, monthly_sold_num, monthly_sold_date,
                    buy_box_price, buy_box_stock, oos_90d_pct, buy_box_seller,
                    pct_top_seller_30d, pct_top_seller_90d, is_fba_pct,
                    fba_offers, fbm_offers, total_offers,
                    fba_stock, fba_price, fbm_price,
                    new_offer_count, rating, rating_count, bought_past_month,
                    monthly_sold_peak, pct_amazon_30d, pct_amazon_90d, return_rate,
                    raw_json
                ) VALUES (
                    :snapshot_date, :asin, :sales_rank_current, :sales_rank_30d_avg,
                    :display_group, :monthly_sold, :monthly_sold_num, :monthly_sold_date,
                    :buy_box_price, :buy_box_stock, :oos_90d_pct, :buy_box_seller,
                    :pct_top_seller_30d, :pct_top_seller_90d, :is_fba_pct,
                    :fba_offers, :fbm_offers, :total_offers,
                    :fba_stock, :fba_price, :fbm_price,
                    :new_offer_count, :rating, :rating_count, :bought_past_month,
                    :monthly_sold_peak, :pct_amazon_30d, :pct_amazon_90d, :return_rate,
                    :raw_json
                )
                """,
                values,
            )
            inserted += 1
        conn.commit()

    log.info("Imported %d rows (skipped %d) into fct_keepa_daily for %s", inserted, skipped, snapshot)
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path, help="Path to Keepa viewer CSV export")
    ap.add_argument("--date", type=date.fromisoformat, default=None,
                    help="Override snapshot date (YYYY-MM-DD). Defaults to today.")
    args = ap.parse_args()
    if not args.csv.exists():
        log.error("File not found: %s", args.csv)
        return 1
    n = import_csv(args.csv, snapshot=args.date)
    return 0 if n > 0 else 2


if __name__ == "__main__":
    sys.exit(main())

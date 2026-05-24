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
    "sales_rank_current": [r"sales\s*rank.*current", r"^current.*sales\s*rank"],
    "sales_rank_30d_avg": [r"sales\s*rank.*30\s*day", r"30\s*day.*sales\s*rank"],
    "display_group":      [r"display\s*group", r"^category$", r"product\s*group"],
    "monthly_sold":       [r"monthly\s*sold(?!\s*date)"],
    "monthly_sold_date":  [r"monthly\s*sold\s*date"],
    "buy_box_price":      [r"buy\s*box.*current", r"buy\s*box.*price"],
    "buy_box_stock":      [r"buy\s*box.*stock", r"^stock$"],
    "oos_90d_pct":        [r"90\s*day.*oos", r"oos.*90"],
    "buy_box_seller":     [r"buy\s*box\s*seller"],
    "pct_top_seller_30d": [r"%?\s*top\s*seller.*30"],
    "pct_top_seller_90d": [r"%?\s*top\s*seller.*90"],
    "is_fba_pct":         [r"is\s*fba", r"%\s*fba"],
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

            title = to_text(row.get(cols["title"])) if "title" in cols else None
            brand = to_text(row.get(cols["brand"])) if "brand" in cols else None
            upsert_product(conn, asin, title=title, brand=brand)

            values = {
                "snapshot_date":      snapshot.isoformat(),
                "asin":               asin,
                "sales_rank_current": to_int(row.get(cols.get("sales_rank_current", ""))),
                "sales_rank_30d_avg": to_int(row.get(cols.get("sales_rank_30d_avg", ""))),
                "display_group":      to_text(row.get(cols.get("display_group", ""))),
                "monthly_sold":       to_text(row.get(cols.get("monthly_sold", ""))),
                "monthly_sold_date":  to_text(row.get(cols.get("monthly_sold_date", ""))),
                "buy_box_price":      to_float(row.get(cols.get("buy_box_price", ""))),
                "buy_box_stock":      to_int(row.get(cols.get("buy_box_stock", ""))),
                "oos_90d_pct":        to_float(row.get(cols.get("oos_90d_pct", ""))),
                "buy_box_seller":     to_text(row.get(cols.get("buy_box_seller", ""))),
                "pct_top_seller_30d": to_float(row.get(cols.get("pct_top_seller_30d", ""))),
                "pct_top_seller_90d": to_float(row.get(cols.get("pct_top_seller_90d", ""))),
                "is_fba_pct":         to_float(row.get(cols.get("is_fba_pct", ""))),
                "raw_json":           json.dumps(row, ensure_ascii=False),
            }

            conn.execute(
                """
                INSERT OR REPLACE INTO fct_keepa_daily (
                    snapshot_date, asin, sales_rank_current, sales_rank_30d_avg,
                    display_group, monthly_sold, monthly_sold_date,
                    buy_box_price, buy_box_stock, oos_90d_pct, buy_box_seller,
                    pct_top_seller_30d, pct_top_seller_90d, is_fba_pct, raw_json
                ) VALUES (
                    :snapshot_date, :asin, :sales_rank_current, :sales_rank_30d_avg,
                    :display_group, :monthly_sold, :monthly_sold_date,
                    :buy_box_price, :buy_box_stock, :oos_90d_pct, :buy_box_seller,
                    :pct_top_seller_30d, :pct_top_seller_90d, :is_fba_pct, :raw_json
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

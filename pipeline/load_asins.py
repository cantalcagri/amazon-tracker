"""
Load / refresh the tracked-ASIN catalog from a text file into dim_product.

Reads one ASIN per line (blank lines and `# comments` ignored), validates the
10-char ASIN format, de-dupes, and upserts each into dim_product. Titles/brands
stay untouched — the API collector fills those in on first fetch. Existing rows
are left intact (idempotent), so loading a bigger list only ADDS new ASINs.

Usage:
    python load_asins.py                      # loads ../data/asins.txt
    python load_asins.py path/to/asins.txt
    python load_asins.py --prune              # also REMOVE dim_product ASINs not
                                              # in the file (and their state row)

--prune is destructive for catalog membership only; it does not touch
fct_keepa_daily / fct_keepa_seller_history history. Use it when the new list is
authoritative and you want stale ASINs to stop being fetched.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from db import get_conn, init_db, upsert_product

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "asins.txt"


def read_asins(path: Path) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ASIN_RE.match(line) and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def load(path: Path, prune: bool = False) -> None:
    asins = read_asins(path)
    if not asins:
        print(f"No valid ASINs found in {path}")
        sys.exit(1)

    init_db()
    conn = get_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM dim_product").fetchone()[0]
        for a in asins:
            upsert_product(conn, a)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM dim_product").fetchone()[0]
        print(f"Loaded {len(asins)} ASINs from {path.name} "
              f"(catalog {before} → {after}, {after - before} new)")

        if prune:
            placeholders = ",".join("?" for _ in asins)
            removed = conn.execute(
                f"DELETE FROM dim_product WHERE asin NOT IN ({placeholders})", asins
            ).rowcount
            conn.execute(
                f"DELETE FROM asin_api_state WHERE asin NOT IN ({placeholders})", asins
            )
            conn.commit()
            print(f"Pruned {removed} ASIN(s) no longer in the list "
                  f"(history tables left intact).")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Load tracked ASINs into dim_product")
    ap.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE,
                    help=f"ASIN list file (default: {DEFAULT_FILE})")
    ap.add_argument("--prune", action="store_true",
                    help="Remove catalog ASINs not present in the file")
    args = ap.parse_args()
    if not args.file.exists():
        print(f"File not found: {args.file}")
        return 1
    load(args.file, prune=args.prune)
    return 0


if __name__ == "__main__":
    sys.exit(main())

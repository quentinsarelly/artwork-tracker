"""
Pull MX (ShipHero) SKU-level quantity totals straight from the API and
write them as a CSV in the same shape as the manual "product table"
export — so mx_lot_discrepancy_check.py consumes it unchanged. Written
into data/shiphero_sku_totals/ by default, meaning it becomes the most
recent file those scripts pick up automatically.

Rows come from the warehouse_products query: one row per active SKU,
total on-hand/available/allocated regardless of lot or bin. Unlike
item_locations (see pull_shiphero_extract.py), this INCLUDES kits —
on_hand double-counts virtual bundles (see
inventory-snapshot/ARTWORK_TRACKING.md) — which is exactly why comparing
the two sources catches units with no lot record at all: kit SKUs will
always show up as a "discrepancy" here, the same as they would in a
manual product-table export.

Output: data/shiphero_sku_totals/shiphero_sku_totals_api_<timestamp>.csv

Usage:
    python scripts/pull_shiphero_sku_totals.py
    python scripts/pull_shiphero_sku_totals.py --out reports/mx_sku_totals.csv
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shiphero_client import iter_warehouse_products


def pull() -> pd.DataFrame:
    rows = []
    for node in iter_warehouse_products():
        sku = (node.get("sku") or "").strip()
        if not sku:
            continue
        rows.append(
            {
                "SKU": sku,
                "On Hand": int(node.get("on_hand") or 0),
                "Available": int(node.get("available") or 0),
                "Reserved": int(node.get("allocated") or 0),
            }
        )
    return pd.DataFrame(rows, columns=["SKU", "On Hand", "Available", "Reserved"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path")
    args = parser.parse_args()

    print("Pulling SKU-level totals from ShipHero (warehouse_products)...")
    df = pull()
    if df.empty:
        raise SystemExit("ShipHero returned no warehouse_products rows — nothing written.")

    out_path = (
        args.out
        or ROOT
        / "data"
        / "shiphero_sku_totals"
        / f"shiphero_sku_totals_api_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(f"\nWrote {len(df)} SKUs ({int(df['On Hand'].sum()):,} units on hand) to {out_path}")


if __name__ == "__main__":
    main()

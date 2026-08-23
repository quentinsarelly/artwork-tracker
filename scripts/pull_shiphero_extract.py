"""
Pull MX (ShipHero) lot-level inventory straight from the API and write
it as a CSV in the same shape as the manual ShipHero export — so
packaging_report.py / build_share_report.py / push_to_sheets.py consume
it unchanged. Written into data/shiphero_exports/ by default, meaning it
becomes the most recent export those scripts pick up automatically.

Rows come from the item_locations query: one row per SKU per bin per
lot — real physical stock in bins. Kits don't appear (only their
components do), unlike warehouse_products.on_hand which double-counts
virtual bundles (see inventory-snapshot/ARTWORK_TRACKING.md).

Lot handling mirrors the manual exports so downstream bucketing is
unchanged: bins with no lot at all get an empty Lot Number, and the
SINLOTE placeholder lot passes through verbatim — packaging_report.py
treats both as "Needs Lot Number", never as a real artwork version.
(A naive pull that counted SINLOTE as real lot data would silently
attribute ~26% of MX units to an artwork version they don't have.)

Available Qty mirrors On Hand: item_locations carries a single quantity
per bin with no reserved split; the report already assumes
nothing-reserved when no available column exists.

Output: data/shiphero_exports/shiphero_api_<timestamp>.csv

Usage:
    python scripts/pull_shiphero_extract.py
    python scripts/pull_shiphero_extract.py --sku SCL-0117 SCL-0155
    python scripts/pull_shiphero_extract.py --out reports/mx_check.csv
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shiphero_client import iter_item_locations


def pull(sku: list[str] | None = None) -> pd.DataFrame:
    rows = []
    for node in iter_item_locations(sku=sku):
        lot = node.get("expiration_lot") or {}
        location = node.get("location") or {}
        rows.append(
            {
                "SKU": (node.get("sku") or "").strip(),
                "Lot Number": (lot.get("name") or "").strip(),
                "Bin": (location.get("name") or "").strip(),
                "Qty On Hand": int(node.get("quantity") or 0),
                "Available Qty": int(node.get("quantity") or 0),
            }
        )
    return pd.DataFrame(
        rows, columns=["SKU", "Lot Number", "Bin", "Qty On Hand", "Available Qty"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--sku", nargs="*", default=None, help="Filter to specific SKU(s); default all"
    )
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path")
    args = parser.parse_args()

    print("Pulling lot-level inventory from ShipHero (item_locations)...")
    df = pull(sku=args.sku)
    if df.empty:
        raise SystemExit("ShipHero returned no item_locations rows — nothing written.")

    out_path = (
        args.out
        or ROOT
        / "data"
        / "shiphero_exports"
        / f"shiphero_api_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    by_lot = df.groupby("SKU")["Qty On Hand"].sum()
    sinlote = int(df.loc[df["Lot Number"] == "SINLOTE", "Qty On Hand"].sum())
    no_lot = int(df.loc[df["Lot Number"] == "", "Qty On Hand"].sum())
    total = int(df["Qty On Hand"].sum())

    print(
        f"\nWrote {len(df)} rows ({by_lot.index.nunique()} SKUs, {total:,} units) to {out_path}"
    )
    print(f"  On a real lot:      {total - sinlote - no_lot:>10,} units")
    print(f"  SINLOTE placeholder:{sinlote:>10,} units  (needs-lot-number)")
    print(f"  No lot at all:      {no_lot:>10,} units  (needs-lot-number)")


if __name__ == "__main__":
    main()

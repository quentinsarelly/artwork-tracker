"""
Pull a full inventory extract from Camelot via the SOAP API
(GetAvailableInventory) and save it as CSV.

NOTE: this is item-level only — QtyOnHand/QtyAvailable/QtyReserved per
ItemNumber, with no batch/lot breakdown. For lot-level detail use
scripts/pull_camelot_lot_extract.py instead (same call, piece-inventory
interface profile), which is what scripts/packaging_report.py needs.
This script stays useful for total on-hand quantities per item — it
keeps Camelot's own QtyAvailableToOrder, which the piece payload doesn't
carry — e.g. as input to scripts/replenishment_check.py.

Output: reports/inventory_extract_<timestamp>.csv

Usage:
    python scripts/pull_inventory_extract.py
    python scripts/pull_inventory_extract.py --item SAR-0262   # single item
    python scripts/pull_inventory_extract.py --out reports/my_extract.csv
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camelot_client import CamelotError, client

_CLIENT_FILTER = os.getenv("CAMELOT_CLIENT", "")

_NS = "urn:microsoft-dynamics-nav/xmlports/x50009"
_FIELDS = [
    "ItemNumber",
    "ItemDesc1",
    "ItemDesc2",
    "SubPart1Number",
    "SubPart2Number",
    "AltItemNo",
    "UOM",
    "QtyOnHand",
    "QtyAvailable",
    "QtyAvailableToOrder",
    "QtyReserved",
    "QtyWithStatus",
]


def pull_inventory(item: str = "") -> pd.DataFrame:
    try:
        doc = client.get_available_inventory(client_filter=_CLIENT_FILTER, item=item)
    except CamelotError as e:
        raise SystemExit(f"Camelot SOAP fault: {e}")

    if doc is None:
        return pd.DataFrame(columns=_FIELDS)

    rows = []
    for el in doc.findall(f"{{{_NS}}}Inventory"):
        row = {}
        for field in _FIELDS:
            val = el.findtext(f"{{{_NS}}}{field}")
            row[field] = (val or "").strip() if field not in (
                "QtyOnHand", "QtyAvailable", "QtyAvailableToOrder", "QtyReserved", "QtyWithStatus"
            ) else int(float(val)) if val else 0
        rows.append(row)

    return pd.DataFrame(rows, columns=_FIELDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--item", default="", help="Filter to a single item/SKU")
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path")
    args = parser.parse_args()

    print("Pulling inventory from Camelot (GetAvailableInventory)...")
    df = pull_inventory(item=args.item)

    out_path = args.out or ROOT / "reports" / f"inventory_extract_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(f"\nWrote {len(df)} items to {out_path}")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()

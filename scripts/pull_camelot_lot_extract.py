"""
Pull US (Camelot) lot-level inventory straight from the API and write it
as a CSV in the same shape as the manual "WebLink - Lot Inventory"
export — so packaging_report.py / build_share_report.py /
push_to_sheets.py consume it unchanged. Written into
data/camelot_exports/ by default, meaning it becomes the most recent
export those scripts pick up automatically.

Uses GetAvailableInventory with the piece-inventory interface profile
(SAR_PINV_E, override with CAMELOT_PIECE_PROFILE) — same call as the
item-level pull in pull_inventory_extract.py, different response payload
(XMLPort 37005332). One row per item + lot + receipt + bin + inventory
status, so a given item/lot can span several rows; the manual WebLink
export collapses those, which is why this file has more rows than the
UI report for the same stock.

Camelot asks that this be pulled no more than once or twice a day.

Availability: the piece payload has no QtyAvailableToOrder field, so the
manual export's "Available Qty" is reconstructed per row as
CountQty - CountQtyCommit, with rows carrying a non-blank InvStatus
(e.g. QC hold) counted as zero-available and their quantity reported in
"Status Qty" instead. Verified against the manual export: this reproduces
Available Qty exactly on every reconciling item/lot.

Lot handling mirrors the manual export so downstream bucketing is
unchanged: Camelot's "NA" placeholder passes through verbatim and
packaging_report.py treats it — like a blank — as "Needs Lot Number",
never as a real artwork version.

Output: data/camelot_exports/camelot_api_<timestamp>.csv

Usage:
    python scripts/pull_camelot_lot_extract.py
    python scripts/pull_camelot_lot_extract.py --item SCL-0117
    python scripts/pull_camelot_lot_extract.py --out reports/us_check.csv
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camelot_client import CamelotError, client

_CLIENT_FILTER = os.getenv("CAMELOT_CLIENT", "")

# XMLPort 37005332 PW Piece Inventory Export
_NS = "urn:microsoft-dynamics-nav/xmlports/x37005332"

# Column names match the manual WebLink export so packaging_report.py's
# alias lookup resolves them identically. Bin/Status/Receipt are extra
# detail the UI report doesn't carry; downstream ignores unknown columns.
_COLUMNS = [
    "Item",
    "Warehouse",
    "LOT#",
    "Receipt Date",
    "Description",
    "Quantity",
    "Qty Reserved",
    "Status Qty",
    "Unit",
    "Alt 1 Qty",
    "Alt 1 Unit",
    "Available Qty",
    "Grs Weight",
    "Bin",
    "Status",
    "Receipt",
    "Code Date",
]


def _text(el: ET.Element, tag: str) -> str:
    return (el.findtext(f"{{{_NS}}}{tag}") or "").strip()


def _num(el: ET.Element, tag: str) -> float:
    raw = _text(el, tag)
    return float(raw) if raw else 0.0


def pull(item: str = "") -> pd.DataFrame:
    try:
        doc = client.get_piece_inventory(client_filter=_CLIENT_FILTER, item=item)
    except CamelotError as e:
        raise SystemExit(f"Camelot SOAP fault: {e}")

    if doc is None:
        return pd.DataFrame(columns=_COLUMNS)

    rows = []
    for el in doc.findall(f"{{{_NS}}}Inventory"):
        qty = _num(el, "CountQty")
        reserved = _num(el, "CountQtyCommit")
        status = _text(el, "InvStatus")
        # A non-blank status (QC hold, damage, ...) makes the whole row
        # unavailable, which is what the WebLink report's Status Qty column
        # nets out of Available Qty.
        rows.append(
            {
                "Item": _text(el, "Item"),
                "Warehouse": _text(el, "Whse"),
                "LOT#": _text(el, "Lot"),
                "Receipt Date": _text(el, "RececiptDate"),  # Camelot's spelling
                "Description": _text(el, "Description"),
                "Quantity": int(qty),
                "Qty Reserved": int(reserved),
                "Status Qty": int(qty) if status else 0,
                "Unit": _text(el, "CountUnit"),
                "Alt 1 Qty": _num(el, "Alt1Qty"),
                "Alt 1 Unit": _text(el, "Alt1Unit"),
                "Available Qty": 0 if status else int(qty - reserved),
                "Grs Weight": _num(el, "GrsWgt"),
                "Bin": _text(el, "Bin"),
                "Status": status,
                "Receipt": _text(el, "Receipt"),
                "Code Date": _text(el, "PieceCodeDate"),
            }
        )

    return pd.DataFrame(rows, columns=_COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--item", default="", help="Filter to a single item/SKU")
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path")
    args = parser.parse_args()

    print(f"Pulling lot-level inventory from Camelot (profile {client.piece_profile})...")
    df = pull(item=args.item)
    if df.empty:
        raise SystemExit("Camelot returned no piece-inventory rows — nothing written.")

    out_path = (
        args.out
        or ROOT / "data" / "camelot_exports" / f"camelot_api_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    total = int(df["Quantity"].sum())
    no_lot = int(df.loc[df["LOT#"].isin(["NA", ""]), "Quantity"].sum())
    on_hold = int(df["Status Qty"].sum())

    print(f"\nWrote {len(df)} rows ({df['Item'].nunique()} items, {total:,} units) to {out_path}")
    print(f"  On a real lot:      {total - no_lot:>10,} units")
    print(f"  NA / no lot:        {no_lot:>10,} units  (needs-lot-number)")
    print(f"  Held (non-blank status): {on_hold:>5,} units  ({', '.join(sorted(s for s in df['Status'].unique() if s)) or 'none'})")
    print(f"  Available:          {int(df['Available Qty'].sum()):>10,} units")


if __name__ == "__main__":
    main()

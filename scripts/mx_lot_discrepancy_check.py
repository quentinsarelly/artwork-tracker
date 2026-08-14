"""
MX (ShipHero) lot-tracking discrepancy check.

ShipHero's SKU-level quantity export (total on-hand per SKU) doesn't
always match the sum of quantities in the lot-level inventory export
(the one used by packaging_report.py --warehouse MX) -- some units have
no lot record in ShipHero at all. That's a different, more severe
failure than the "Needs Lot Number" tab in build_share_report.py, which
only catches units that *are* in the lot extract but tagged with the
"SINLOTE" placeholder (see NO_LOT_MARKERS in packaging_report.py) --
units missing from the lot extract entirely don't show up there because
they're absent from that file, not present-with-a-placeholder.

This script flags SKUs where the two extracts disagree, so warehouse can
investigate why those units aren't lot-tracked in ShipHero at all.

Inputs (column names matched case-insensitively against the same alias
lists as packaging_report.py):

  data/shiphero_sku_totals/*.csv (or .xlsx) -- ShipHero SKU-level qty
  export (one row per SKU, total on-hand quantity). Most recent file
  used unless --sku-totals is given.

  data/shiphero_exports/*.csv (or .xlsx) -- ShipHero lot-level inventory
  export. Most recent file used unless --lot-export is given.

Output: reports/mx_lot_discrepancy_<timestamp>.xlsx -- one row per SKU
where the two totals disagree. A positive discrepancy means units are
missing from the lot-level extract entirely (worse than SINLOTE, which
is at least tracked with a placeholder). A negative discrepancy usually
just means the two extracts were pulled at different times, not a real
data-quality issue -- flagged separately so it doesn't get confused with
the real problem.

Usage:
    python scripts/mx_lot_discrepancy_check.py
    python scripts/mx_lot_discrepancy_check.py --sku-totals ... --lot-export ... --out ...
"""

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from build_share_report import _write_sheet, load_descriptions
from packaging_report import ITEM_ALIASES, QTY_ALIASES, WAREHOUSES, _find_col, _latest_file, _parse_qty, _read_table, load_export

ROOT = Path(__file__).resolve().parent.parent
SKU_TOTALS_DIR = ROOT / "data" / "shiphero_sku_totals"


def load_sku_totals(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    item_col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    qty_col = _find_col(list(df.columns), QTY_ALIASES, "quantity")

    out = df[[item_col, qty_col]].copy()
    out.columns = ["item", "qty"]
    out["item"] = out["item"].str.strip()
    out["qty"] = _parse_qty(out["qty"])
    return out.groupby("item", as_index=False)["qty"].sum()


def load_lot_totals(path: Path) -> pd.DataFrame:
    inventory = load_export(path)
    return inventory.groupby("item", as_index=False)["qty"].sum()


def build_discrepancy(sku_totals: pd.DataFrame, lot_totals: pd.DataFrame, descriptions: pd.DataFrame) -> pd.DataFrame:
    merged = sku_totals.merge(
        lot_totals, on="item", how="outer", suffixes=("_sku_extract", "_lot_extract")
    ).fillna(0)
    merged["qty_sku_extract"] = merged["qty_sku_extract"].astype(int)
    merged["qty_lot_extract"] = merged["qty_lot_extract"].astype(int)
    merged["discrepancy"] = merged["qty_sku_extract"] - merged["qty_lot_extract"]
    merged = merged[merged["discrepancy"] != 0]
    merged = merged.merge(descriptions, on="item", how="left")
    merged["description"] = merged["description"].fillna("")

    return merged[["item", "description", "qty_sku_extract", "qty_lot_extract", "discrepancy"]].rename(columns={
        "item": "Item",
        "description": "Description",
        "qty_sku_extract": "Qty (SKU Extract)",
        "qty_lot_extract": "Qty (Lot Extract)",
        "discrepancy": "Discrepancy (Missing From Lot Data)",
    }).sort_values("Discrepancy (Missing From Lot Data)", ascending=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sku-totals", type=Path, default=None, help="ShipHero SKU-level qty export")
    parser.add_argument("--lot-export", type=Path, default=None, help="ShipHero lot-level inventory export")
    parser.add_argument("--out", type=Path, default=None, help="Output .xlsx path")
    args = parser.parse_args()

    sku_totals_path = args.sku_totals or _latest_file(SKU_TOTALS_DIR)
    lot_export_path = args.lot_export or _latest_file(WAREHOUSES["MX"]["export_dir"])

    print(f"SKU totals export: {sku_totals_path}")
    print(f"Lot-level export:  {lot_export_path}")

    sku_totals = load_sku_totals(sku_totals_path)
    lot_totals = load_lot_totals(lot_export_path)
    descriptions = load_descriptions(lot_export_path)

    discrepancy = build_discrepancy(sku_totals, lot_totals, descriptions)
    missing_from_lot = discrepancy[discrepancy["Discrepancy (Missing From Lot Data)"] > 0]
    extra_in_lot = discrepancy[discrepancy["Discrepancy (Missing From Lot Data)"] < 0]

    out_path = args.out or ROOT / "reports" / f"mx_lot_discrepancy_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        _write_sheet(
            writer, missing_from_lot, "Missing From Lot Data",
            note=f"MX (ShipHero) — as of {today}. SKU-level qty exceeds the sum of the lot-level "
                 "extract, meaning these units have no lot record at all in ShipHero (not even a "
                 "SINLOTE placeholder). Warehouse: please investigate and lot-tag this stock.",
        )
        _write_sheet(
            writer, extra_in_lot, "Lot Total Exceeds SKU Total",
            note="Lot-level extract has more units than the SKU-level extract. Usually just means "
                 "the two extracts were pulled at different times — re-run both together before "
                 "treating this as a real discrepancy.",
        )

    print(f"\nWrote discrepancy report to {out_path}")
    print(f"  Missing From Lot Data:        {len(missing_from_lot)} SKUs, {int(missing_from_lot['Discrepancy (Missing From Lot Data)'].sum()) if len(missing_from_lot) else 0} units")
    print(f"  Lot Total Exceeds SKU Total:   {len(extra_in_lot)} SKUs")


if __name__ == "__main__":
    main()

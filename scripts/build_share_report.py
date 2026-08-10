"""
Build a shareable Excel report for the sales and warehouse teams, from
the same inputs as packaging_report.py (Camelot/ShipHero lot export +
batch -> packaging mapping).

Three tabs:
  - "Packaging Summary" (sales): old vs. new packaging qty per product,
    for deciding what to allocate to each PO.
  - "Needs Lot Number" (warehouse): stock with no lot/batch code recorded
    (see NO_LOT_MARKERS in packaging_report.py — Camelot's "NA", ShipHero's
    "SINLOTE", or blank) for products in the transition. Warehouse should
    investigate and add the correct lot number, then re-run this report.
  - "Unmapped Batches": batch codes present in the export but not yet
    classified old/new in the mapping file — a mapping-file maintenance
    item, not a warehouse task.

--warehouse ALL produces one file per warehouse (US and MX), not a
combined file.

Output: reports/packaging_share_report_<warehouse>_<timestamp>.xlsx

Usage:
    python scripts/build_share_report.py --warehouse US
    python scripts/build_share_report.py --warehouse ALL
    python scripts/build_share_report.py --warehouse MX --export ... --mapping ... --out ...
"""

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from packaging_report import (
    ITEM_ALIASES,
    NO_LOT_MARKERS,
    WAREHOUSES,
    _find_col,
    _latest_file,
    _read_table,
    build_report,
    load_export,
    load_mapping,
)

ROOT = Path(__file__).resolve().parent.parent

DESC_ALIASES = ["description", "item description", "desc", "itemdesc1", "product_name", "product name"]


def load_descriptions(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    item_col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    try:
        desc_col = _find_col(list(df.columns), DESC_ALIASES, "description")
    except SystemExit:
        return pd.DataFrame(columns=["item", "description"])
    out = df[[item_col, desc_col]].copy()
    out.columns = ["item", "description"]
    out["item"] = out["item"].str.strip()
    return out.drop_duplicates(subset="item", keep="first")


def _autosize(ws, df: pd.DataFrame) -> None:
    for i, col in enumerate(df.columns, start=1):
        width = max([len(str(col))] + [len(str(v)) for v in df[col]]) + 2 if len(df) else len(str(col)) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 60)


def _write_sheet(writer, df: pd.DataFrame, sheet_name: str, note: str, bold_rows: tuple[int, ...] = ()) -> None:
    df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=2)
    ws = writer.sheets[sheet_name]
    ws["A1"] = note
    ws["A1"].font = Font(italic=True, size=9)
    for cell in ws[3]:
        cell.font = Font(bold=True)
    for row_offset in bold_rows:
        for cell in ws[3 + row_offset]:
            cell.font = Font(bold=True)
    ws.freeze_panes = f"A{4 + len(bold_rows)}"
    _autosize(ws, df)


def build_summary(inventory: pd.DataFrame, mapping: pd.DataFrame, descriptions: pd.DataFrame) -> pd.DataFrame:
    summary, unmapped = build_report(inventory, mapping)
    summary = summary.merge(descriptions, on="item", how="left")
    summary["description"] = summary["description"].fillna("")
    # float("nan") rather than pd.NA: pd.NA doesn't support .round() below.
    summary["pct_old_packaging"] = (
        summary["qty_old_packaging"] / summary["total_qty"].replace(0, float("nan")) * 100
    ).round(1)
    summary = summary[[
        "item", "description",
        "qty_old_packaging", "available_qty_old_packaging",
        "qty_new_packaging", "available_qty_new_packaging",
        "total_qty", "total_available_qty",
        "pct_old_packaging", "old_packaging_batches",
    ]].rename(columns={
        "item": "Item",
        "description": "Description",
        "qty_old_packaging": "Qty Old Packaging",
        "available_qty_old_packaging": "Available Qty Old Packaging",
        "qty_new_packaging": "Qty New Packaging",
        "available_qty_new_packaging": "Available Qty New Packaging",
        "total_qty": "Total Qty On Hand",
        "total_available_qty": "Total Available Qty",
        "pct_old_packaging": "% Old Packaging",
        "old_packaging_batches": "Old Packaging Batch(es)",
    }).sort_values("Qty Old Packaging", ascending=False)

    total_old = int(summary["Qty Old Packaging"].sum())
    total_avail_old = int(summary["Available Qty Old Packaging"].sum())
    total_new = int(summary["Qty New Packaging"].sum())
    total_avail_new = int(summary["Available Qty New Packaging"].sum())
    total_qty = total_old + total_new
    total_avail_qty = total_avail_old + total_avail_new
    total_row = pd.DataFrame([{
        "Item": "TOTAL",
        "Description": f"{len(summary)} products",
        "Qty Old Packaging": total_old,
        "Available Qty Old Packaging": total_avail_old,
        "Qty New Packaging": total_new,
        "Available Qty New Packaging": total_avail_new,
        "Total Qty On Hand": total_qty,
        "Total Available Qty": total_avail_qty,
        "% Old Packaging": round(total_old / total_qty * 100, 1) if total_qty else None,
        "Old Packaging Batch(es)": "",
    }])
    summary = pd.concat([total_row, summary], ignore_index=True)

    return summary, unmapped


def build_needs_lot(unmapped: pd.DataFrame, descriptions: pd.DataFrame) -> pd.DataFrame:
    needs_lot = unmapped[unmapped["batch"].isin(NO_LOT_MARKERS)].copy()
    needs_lot = needs_lot.merge(descriptions, on="item", how="left")
    needs_lot["description"] = needs_lot["description"].fillna("")
    return needs_lot[["item", "description", "batch", "qty"]].rename(columns={
        "item": "Item", "description": "Description", "batch": "LOT# (as recorded)", "qty": "Qty",
    }).sort_values("Qty", ascending=False)


def build_unmapped_batches(unmapped: pd.DataFrame, descriptions: pd.DataFrame) -> pd.DataFrame:
    other = unmapped[~unmapped["batch"].isin(NO_LOT_MARKERS)].copy()
    other = other.merge(descriptions, on="item", how="left")
    other["description"] = other["description"].fillna("")
    return other[["item", "description", "batch", "qty"]].rename(columns={
        "item": "Item", "description": "Description", "batch": "LOT#", "qty": "Qty",
    }).sort_values(["Item", "LOT#"])


def run_for_warehouse(warehouse: str, export: Path | None, mapping: Path | None, out: Path | None) -> None:
    label = WAREHOUSES[warehouse]["label"]
    print(f"\n=== {label} ===")

    export_path = export or _latest_file(WAREHOUSES[warehouse]["export_dir"])
    mapping_path = mapping or _latest_file(ROOT / "data" / "mapping")

    print(f"Inventory export: {export_path}")
    print(f"Mapping file:     {mapping_path}")

    inventory = load_export(export_path)
    mapping_df = load_mapping(mapping_path)
    descriptions = load_descriptions(export_path)

    summary, unmapped = build_summary(inventory, mapping_df, descriptions)
    needs_lot = build_needs_lot(unmapped, descriptions)
    unmapped_batches = build_unmapped_batches(unmapped, descriptions)

    out_path = out or ROOT / "reports" / f"packaging_share_report_{warehouse}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        _write_sheet(
            writer, summary, "Packaging Summary",
            note=f"{label} — old vs. new packaging on hand as of {today}. For sales / PO allocation planning.",
            bold_rows=(1,),
        )
        _write_sheet(
            writer, needs_lot, "Needs Lot Number",
            note=f"{label} warehouse: this stock has no lot number recorded. Please investigate "
                 "and add the correct lot number, then ask for this report to be re-run.",
        )
        _write_sheet(
            writer, unmapped_batches, "Unmapped Batches",
            note="Batch codes present in the export but not yet classified old/new in the mapping file "
                 "(internal use — not a warehouse or sales action item).",
        )

    print(f"\nWrote share report to {out_path}")
    print(f"  Packaging Summary: {len(summary) - 1} products (+ totals row)")
    print(f"  Needs Lot Number:  {len(needs_lot)} rows, {int(needs_lot['Qty'].sum()) if len(needs_lot) else 0} units")
    print(f"  Unmapped Batches:  {len(unmapped_batches)} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--warehouse", choices=["US", "MX", "ALL"], default="US", help="Which warehouse to report on")
    parser.add_argument("--export", type=Path, default=None, help="Inventory export file (only valid with a single --warehouse)")
    parser.add_argument("--mapping", type=Path, default=None, help="Batch -> packaging mapping file")
    parser.add_argument("--out", type=Path, default=None, help="Output .xlsx path (only valid with a single --warehouse)")
    args = parser.parse_args()

    warehouses = ["US", "MX"] if args.warehouse == "ALL" else [args.warehouse]
    if len(warehouses) > 1 and (args.export or args.out):
        parser.error("--export/--out require a single --warehouse (US or MX), not ALL")

    for warehouse in warehouses:
        run_for_warehouse(warehouse, args.export, args.mapping, args.out)


if __name__ == "__main__":
    main()

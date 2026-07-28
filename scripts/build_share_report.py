"""
Build a shareable Excel report for the sales and warehouse teams, from
the same inputs as packaging_report.py (Camelot lot export + batch ->
packaging mapping).

Three tabs:
  - "Packaging Summary" (sales): old vs. new packaging qty per product,
    for deciding what to allocate to each PO.
  - "Needs Lot Number" (warehouse): stock with no lot/batch code recorded
    in Camelot (LOT# = "NA" or blank) for products in the transition.
    Warehouse should investigate and add the correct lot number in
    Camelot, then re-run this report.
  - "Unmapped Batches": batch codes present in the export but not yet
    classified old/new in the mapping file — a mapping-file maintenance
    item, not a warehouse task.

Output: reports/packaging_share_report_<timestamp>.xlsx

Usage:
    python scripts/build_share_report.py
    python scripts/build_share_report.py --export ... --mapping ... --out ...
"""

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from packaging_report import ITEM_ALIASES, _find_col, _latest_file, _read_table, build_report, load_export, load_mapping

ROOT = Path(__file__).resolve().parent.parent

DESC_ALIASES = ["description", "item description", "desc", "itemdesc1"]


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
    summary["pct_old_packaging"] = (
        summary["qty_old_packaging"] / summary["total_qty"].replace(0, pd.NA) * 100
    ).round(1)
    summary = summary[[
        "item", "description", "qty_old_packaging", "qty_new_packaging",
        "total_qty", "pct_old_packaging", "old_packaging_batches",
    ]].rename(columns={
        "item": "Item",
        "description": "Description",
        "qty_old_packaging": "Qty Old Packaging",
        "qty_new_packaging": "Qty New Packaging",
        "total_qty": "Total Qty On Hand",
        "pct_old_packaging": "% Old Packaging",
        "old_packaging_batches": "Old Packaging Batch(es)",
    }).sort_values("Qty Old Packaging", ascending=False)

    total_old = int(summary["Qty Old Packaging"].sum())
    total_new = int(summary["Qty New Packaging"].sum())
    total_qty = total_old + total_new
    total_row = pd.DataFrame([{
        "Item": "TOTAL",
        "Description": f"{len(summary)} products",
        "Qty Old Packaging": total_old,
        "Qty New Packaging": total_new,
        "Total Qty On Hand": total_qty,
        "% Old Packaging": round(total_old / total_qty * 100, 1) if total_qty else None,
        "Old Packaging Batch(es)": "",
    }])
    summary = pd.concat([total_row, summary], ignore_index=True)

    return summary, unmapped


def build_needs_lot(unmapped: pd.DataFrame, descriptions: pd.DataFrame) -> pd.DataFrame:
    needs_lot = unmapped[unmapped["batch"].isin(["NA", ""])].copy()
    needs_lot = needs_lot.merge(descriptions, on="item", how="left")
    needs_lot["description"] = needs_lot["description"].fillna("")
    return needs_lot[["item", "description", "batch", "qty"]].rename(columns={
        "item": "Item", "description": "Description", "batch": "LOT# (as recorded)", "qty": "Qty",
    }).sort_values("Qty", ascending=False)


def build_unmapped_batches(unmapped: pd.DataFrame, descriptions: pd.DataFrame) -> pd.DataFrame:
    other = unmapped[~unmapped["batch"].isin(["NA", ""])].copy()
    other = other.merge(descriptions, on="item", how="left")
    other["description"] = other["description"].fillna("")
    return other[["item", "description", "batch", "qty"]].rename(columns={
        "item": "Item", "description": "Description", "batch": "LOT#", "qty": "Qty",
    }).sort_values(["Item", "LOT#"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", type=Path, default=None, help="Camelot lot/batch inventory export file")
    parser.add_argument("--mapping", type=Path, default=None, help="Batch -> packaging mapping file")
    parser.add_argument("--out", type=Path, default=None, help="Output .xlsx path")
    args = parser.parse_args()

    export_path = args.export or _latest_file(ROOT / "data" / "camelot_exports")
    mapping_path = args.mapping or _latest_file(ROOT / "data" / "mapping")

    print(f"Inventory export: {export_path}")
    print(f"Mapping file:     {mapping_path}")

    inventory = load_export(export_path)
    mapping = load_mapping(mapping_path)
    descriptions = load_descriptions(export_path)

    summary, unmapped = build_summary(inventory, mapping, descriptions)
    needs_lot = build_needs_lot(unmapped, descriptions)
    unmapped_batches = build_unmapped_batches(unmapped, descriptions)

    out_path = args.out or ROOT / "reports" / f"packaging_share_report_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        _write_sheet(
            writer, summary, "Packaging Summary",
            note=f"Old vs. new packaging on hand as of {today}. For sales / PO allocation planning.",
            bold_rows=(1,),
        )
        _write_sheet(
            writer, needs_lot, "Needs Lot Number",
            note="Warehouse: this stock has no lot number recorded in Camelot. Please investigate "
                 "and add the correct lot number in Camelot, then ask for this report to be re-run.",
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


if __name__ == "__main__":
    main()

"""
Old vs. new packaging report.

Camelot's SOAP API has no lot/batch field (confirmed live — see
spike_check_lot_fields.py), so batch-level inventory must come from a
manual export out of the Camelot UI. This script joins that export
against a batch -> packaging mapping file to report, per product, how
much old-packaging vs new-packaging stock remains.

Inputs (column names are matched case-insensitively against the aliases
below, so the export/mapping don't need to match these exact headers):

  data/camelot_exports/*.csv (or .xlsx) — most recent file is used unless
  --export is given. Expected columns: item/SKU, batch/lot code, quantity
  on hand.

  data/mapping/*.csv (or .xlsx) — one row per batch code. Expected
  columns: item/SKU, batch/lot code, packaging (old/new).

Output: reports/packaging_report_<timestamp>.csv

Usage:
    python scripts/packaging_report.py
    python scripts/packaging_report.py --export data/camelot_exports/foo.csv --mapping data/mapping/bar.csv
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

ITEM_ALIASES = ["item", "itemnumber", "item number", "sku", "product", "product sku"]
BATCH_ALIASES = ["batch", "batch code", "batch number", "batchno", "lot", "lot code", "lot number", "lotno", "lot no"]
QTY_ALIASES = ["qty", "quantity", "qtyonhand", "qty on hand", "on hand", "on hand qty", "onhandqty"]
PACKAGING_ALIASES = ["packaging", "packaging version", "old_new", "old/new", "version", "presentation"]


def _find_col(columns: list[str], aliases: list[str], required_for: str) -> str:
    normalized = {c.strip().lower(): c for c in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    raise SystemExit(
        f"Could not find a column for '{required_for}' in {columns}. "
        f"Expected one of: {aliases}"
    )


def _latest_file(directory: Path) -> Path:
    candidates = sorted(
        [p for p in directory.glob("*") if p.suffix.lower() in (".csv", ".xlsx") and p.name != ".gitkeep"],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"No .csv/.xlsx files found in {directory}")
    return candidates[0]


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str)


def load_export(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    item_col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    batch_col = _find_col(list(df.columns), BATCH_ALIASES, "batch/lot code")
    qty_col = _find_col(list(df.columns), QTY_ALIASES, "quantity on hand")

    out = df[[item_col, batch_col, qty_col]].copy()
    out.columns = ["item", "batch", "qty"]
    out["item"] = out["item"].str.strip()
    out["batch"] = out["batch"].str.strip()
    out["qty"] = pd.to_numeric(out["qty"], errors="coerce").fillna(0).astype(int)
    return out


def load_mapping(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    item_col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    batch_col = _find_col(list(df.columns), BATCH_ALIASES, "batch/lot code")
    pkg_col = _find_col(list(df.columns), PACKAGING_ALIASES, "packaging (old/new)")

    out = df[[item_col, batch_col, pkg_col]].copy()
    out.columns = ["item", "batch", "packaging"]
    out["item"] = out["item"].str.strip()
    out["batch"] = out["batch"].str.strip()
    out["packaging"] = out["packaging"].str.strip().str.lower()

    bad = ~out["packaging"].isin(["old", "new"])
    if bad.any():
        raise SystemExit(
            "Mapping file has packaging values other than 'old'/'new':\n"
            f"{out.loc[bad, ['item', 'batch', 'packaging']]}"
        )
    return out


def build_report(inventory: pd.DataFrame, mapping: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    merged = inventory.merge(mapping, on=["item", "batch"], how="left")

    unmapped = merged[merged["packaging"].isna()]
    mapped = merged.dropna(subset=["packaging"])

    pivot = (
        mapped.groupby(["item", "packaging"])["qty"]
        .sum()
        .unstack(fill_value=0)
        .reindex(columns=["old", "new"], fill_value=0)
        .rename(columns={"old": "qty_old_packaging", "new": "qty_new_packaging"})
        .reset_index()
    )
    pivot["total_qty"] = pivot["qty_old_packaging"] + pivot["qty_new_packaging"]

    old_batches = (
        mapped[mapped["packaging"] == "old"]
        .groupby("item")
        .apply(lambda g: ", ".join(f"{b} ({q})" for b, q in zip(g["batch"], g["qty"])), include_groups=False)
        .rename("old_packaging_batches")
        .reset_index()
    )
    pivot = pivot.merge(old_batches, on="item", how="left")
    pivot["old_packaging_batches"] = pivot["old_packaging_batches"].fillna("")

    return pivot.sort_values("item"), unmapped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", type=Path, default=None, help="Camelot lot/batch inventory export file")
    parser.add_argument("--mapping", type=Path, default=None, help="Batch -> packaging mapping file")
    parser.add_argument("--out", type=Path, default=None, help="Output report path")
    args = parser.parse_args()

    export_path = args.export or _latest_file(ROOT / "data" / "camelot_exports")
    mapping_path = args.mapping or _latest_file(ROOT / "data" / "mapping")

    print(f"Inventory export: {export_path}")
    print(f"Mapping file:     {mapping_path}")

    inventory = load_export(export_path)
    mapping = load_mapping(mapping_path)

    report, unmapped = build_report(inventory, mapping)

    out_path = args.out or ROOT / "reports" / f"packaging_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(out_path, index=False)

    print(f"\nWrote {len(report)} products to {out_path}")
    print(report.to_string(index=False))

    if not unmapped.empty:
        print(
            f"\n*** WARNING: {len(unmapped)} inventory rows had no mapping entry and were "
            "EXCLUDED from the report (not counted as old or new). Add these to the mapping file: ***"
        )
        print(unmapped[["item", "batch", "qty"]].drop_duplicates().to_string(index=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Old vs. new packaging report.

Camelot's SOAP API has no lot/batch field (confirmed live — see
spike_check_lot_fields.py), so batch-level inventory must come from a
manual export out of the Camelot UI (US warehouse) or ShipHero (MX
warehouse). This script joins that export against a batch -> packaging
mapping file to report, per product, how much old-packaging vs
new-packaging stock remains.

Inputs (column names are matched case-insensitively against the aliases
below, so the export/mapping don't need to match these exact headers):

  data/camelot_exports/*.csv (or .xlsx) [--warehouse US] or
  data/shiphero_exports/*.csv (or .xlsx) [--warehouse MX] — most recent
  file is used unless --export is given. Expected columns: item/SKU,
  batch/lot code, quantity on hand, and available quantity (on-hand minus
  reserved — if no such column is found, available quantity falls back to
  on-hand quantity, i.e. assumes nothing is reserved).

  data/mapping/*.csv (or .xlsx) — one row per batch code, shared across
  both warehouses. Expected columns: item/SKU, batch/lot code, packaging
  (old/new).

Output: reports/packaging_report_<warehouse>_<timestamp>.csv

Usage:
    python scripts/packaging_report.py --warehouse US
    python scripts/packaging_report.py --warehouse ALL
    python scripts/packaging_report.py --warehouse MX --export data/shiphero_exports/foo.csv --mapping data/mapping/bar.csv
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

WAREHOUSES = {
    "US": {"export_dir": ROOT / "data" / "camelot_exports", "label": "US (Camelot)"},
    "MX": {"export_dir": ROOT / "data" / "shiphero_exports", "label": "MX (ShipHero)"},
}

ITEM_ALIASES = ["item", "itemnumber", "item number", "sku", "product", "product sku"]
BATCH_ALIASES = [
    "batch", "batch code", "batch number", "batchno", "lot", "lot code",
    "lot number", "lotno", "lot no", "lot#", "lot #",
]
QTY_ALIASES = ["qty", "quantity", "qtyonhand", "qty on hand", "on hand", "on hand qty", "onhandqty"]
AVAILABLE_QTY_ALIASES = [
    "available qty", "availableqty", "qty available", "available quantity",
    "available", "available qty to order", "qty available to order",
]
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
    # keep_default_na=False: Camelot uses the literal string "NA" as its
    # batch code for non-lot-tracked items. Pandas' default NA-string list
    # includes "NA" and would otherwise silently turn it into a real null.
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path, dtype=str, keep_default_na=False)
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    except UnicodeDecodeError:
        # Some WebLink exports come out cp1252 instead of utf-8 (seen with
        # accented product descriptions, e.g. "BUCHÓN").
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")


def _parse_qty(series: pd.Series) -> pd.Series:
    # Camelot's export uses thousands separators (e.g. "4,994") which
    # to_numeric would otherwise silently coerce to NaN -> 0.
    return pd.to_numeric(series.str.replace(",", "", regex=False), errors="coerce").fillna(0).astype(int)


def load_export(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    item_col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    batch_col = _find_col(list(df.columns), BATCH_ALIASES, "batch/lot code")
    qty_col = _find_col(list(df.columns), QTY_ALIASES, "quantity on hand")

    try:
        avail_col = _find_col(list(df.columns), AVAILABLE_QTY_ALIASES, "available quantity")
    except SystemExit:
        print(
            "WARNING: no 'Available Qty' column found in the export — "
            "falling back to on-hand quantity (i.e. assuming nothing reserved)."
        )
        avail_col = qty_col

    out = df[[item_col, batch_col, qty_col]].copy()
    out.columns = ["item", "batch", "qty"]
    out["available_qty"] = df[avail_col]  # may equal qty_col in the fallback case
    out["item"] = out["item"].str.strip()
    out["batch"] = out["batch"].str.strip()
    out["qty"] = _parse_qty(out["qty"])
    out["available_qty"] = _parse_qty(out["available_qty"])
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
    # Only products named in the mapping file are part of the packaging
    # transition. Without this filter, every batch of every unrelated
    # product in the warehouse would show up as "unmapped" noise.
    transitioning_items = set(mapping["item"])
    inventory = inventory[inventory["item"].isin(transitioning_items)]

    merged = inventory.merge(mapping, on=["item", "batch"], how="left")

    unmapped = merged[merged["packaging"].isna()]
    mapped = merged.dropna(subset=["packaging"])

    pivot = (
        mapped.groupby(["item", "packaging"])[["qty", "available_qty"]]
        .sum()
        .unstack(fill_value=0)
    )
    pivot.columns = [f"{value_col}_{pkg}" for value_col, pkg in pivot.columns]
    pivot = pivot.reindex(
        columns=["qty_old", "qty_new", "available_qty_old", "available_qty_new"], fill_value=0
    ).rename(columns={
        "qty_old": "qty_old_packaging",
        "qty_new": "qty_new_packaging",
        "available_qty_old": "available_qty_old_packaging",
        "available_qty_new": "available_qty_new_packaging",
    }).reset_index()

    pivot["total_qty"] = pivot["qty_old_packaging"] + pivot["qty_new_packaging"]
    pivot["total_available_qty"] = pivot["available_qty_old_packaging"] + pivot["available_qty_new_packaging"]

    old_only = mapped[mapped["packaging"] == "old"]
    if old_only.empty:
        old_batches = pd.DataFrame(columns=["item", "old_packaging_batches"])
    else:
        old_batches = (
            old_only.groupby("item")
            .apply(lambda g: ", ".join(f"{b} ({q})" for b, q in zip(g["batch"], g["qty"])), include_groups=False)
            .rename("old_packaging_batches")
            .reset_index()
        )
    pivot = pivot.merge(old_batches, on="item", how="left")
    pivot["old_packaging_batches"] = pivot["old_packaging_batches"].fillna("")

    return pivot.sort_values("item"), unmapped


def run_for_warehouse(warehouse: str, export: Path | None, mapping: Path | None, out: Path | None) -> bool:
    """Returns True if the report came out clean (no unmapped batches)."""
    print(f"\n=== {WAREHOUSES[warehouse]['label']} ===")

    export_path = export or _latest_file(WAREHOUSES[warehouse]["export_dir"])
    mapping_path = mapping or _latest_file(ROOT / "data" / "mapping")

    print(f"Inventory export: {export_path}")
    print(f"Mapping file:     {mapping_path}")

    inventory = load_export(export_path)
    mapping_df = load_mapping(mapping_path)

    report, unmapped = build_report(inventory, mapping_df)

    out_path = out or ROOT / "reports" / f"packaging_report_{warehouse}_{datetime.now():%Y%m%d_%H%M%S}.csv"
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
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--warehouse", choices=["US", "MX", "ALL"], default="US", help="Which warehouse to report on")
    parser.add_argument("--export", type=Path, default=None, help="Inventory export file (only valid with a single --warehouse)")
    parser.add_argument("--mapping", type=Path, default=None, help="Batch -> packaging mapping file")
    parser.add_argument("--out", type=Path, default=None, help="Output report path (only valid with a single --warehouse)")
    args = parser.parse_args()

    warehouses = ["US", "MX"] if args.warehouse == "ALL" else [args.warehouse]
    if len(warehouses) > 1 and (args.export or args.out):
        parser.error("--export/--out require a single --warehouse (US or MX), not ALL")

    clean = True
    for warehouse in warehouses:
        clean &= run_for_warehouse(warehouse, args.export, args.mapping, args.out)

    if not clean:
        sys.exit(1)


if __name__ == "__main__":
    main()

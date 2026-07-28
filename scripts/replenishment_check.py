"""
Replenishment / PO coverage check.

Compares current US on-hand inventory (from Camelot) against upcoming PO
demand to flag products that need more stock shipped from origin.

CAVEAT: Camelot's SOAP API doesn't expose a clear "open/upcoming PO
queue" — get_order_status_date_range / get_transaction_status_time_range
are oriented around orders that have already shipped or are staged for
shipment, not a forward-looking demand queue. This script pulls that data
as a best-effort supplementary signal only; the supplemental PO
spreadsheet (data/po/) is treated as the primary source of upcoming
demand until the right Camelot call for open POs is confirmed with the
team. Verify the Camelot-sourced numbers before trusting them.

Inputs:
  data/po/*.csv (or .xlsx) — most recent file used unless --po is given.
  Expected columns: item/SKU, quantity demanded. Optional: PO number,
  retailer.

Output: reports/replenishment_report_<timestamp>.csv

Usage:
    python scripts/replenishment_check.py
    python scripts/replenishment_check.py --po data/po/upcoming.csv --camelot-days-ahead 30
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

ITEM_ALIASES = ["item", "itemnumber", "item number", "sku", "product", "product sku", "buyer item num", "vendor item num"]
QTY_ALIASES = ["qty", "quantity", "qty ordered", "quantity ordered", "qty demanded", "qty needed"]

_CLIENT_FILTER = os.getenv("CAMELOT_CLIENT", "")


def _find_col(columns: list[str], aliases: list[str], required_for: str) -> str:
    normalized = {c.strip().lower(): c for c in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    raise SystemExit(f"Could not find a column for '{required_for}' in {columns}. Expected one of: {aliases}")


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


def load_on_hand_inventory() -> pd.DataFrame:
    """Current total on-hand qty per item, pulled live from Camelot."""
    try:
        doc = client.get_available_inventory(client_filter=_CLIENT_FILTER)
    except CamelotError as e:
        raise SystemExit(f"Could not reach Camelot for on-hand inventory: {e}")

    if doc is None:
        return pd.DataFrame(columns=["item", "on_hand"])

    ns = "urn:microsoft-dynamics-nav/xmlports/x50009"
    rows = []
    for el in doc.findall(f"{{{ns}}}Inventory"):
        item = (el.findtext(f"{{{ns}}}ItemNumber") or "").strip()
        qty = el.findtext(f"{{{ns}}}QtyOnHand") or "0"
        if item:
            rows.append({"item": item, "on_hand": int(float(qty))})
    return pd.DataFrame(rows)


def load_po_demand(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    item_col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    qty_col = _find_col(list(df.columns), QTY_ALIASES, "quantity demanded")

    out = df[[item_col, qty_col]].copy()
    out.columns = ["item", "demand"]
    out["item"] = out["item"].str.strip()
    out["demand"] = pd.to_numeric(out["demand"], errors="coerce").fillna(0).astype(int)
    return out.groupby("item", as_index=False)["demand"].sum()


def build_report(on_hand: pd.DataFrame, demand: pd.DataFrame) -> pd.DataFrame:
    merged = demand.merge(on_hand, on="item", how="left")
    merged["on_hand"] = merged["on_hand"].fillna(0).astype(int)
    merged["shortfall"] = (merged["demand"] - merged["on_hand"]).clip(lower=0)
    return merged.sort_values("shortfall", ascending=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--po", type=Path, default=None, help="Supplemental PO demand file")
    parser.add_argument("--out", type=Path, default=None, help="Output report path")
    args = parser.parse_args()

    po_path = args.po or _latest_file(ROOT / "data" / "po")
    print(f"PO demand file: {po_path}")
    print("Pulling live on-hand inventory from Camelot...")

    on_hand = load_on_hand_inventory()
    demand = load_po_demand(po_path)
    report = build_report(on_hand, demand)

    out_path = args.out or ROOT / "reports" / f"replenishment_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(out_path, index=False)

    print(f"\nWrote {len(report)} products to {out_path}")
    print(report.to_string(index=False))

    at_risk = report[report["shortfall"] > 0]
    if not at_risk.empty:
        print(f"\n*** {len(at_risk)} product(s) may need more stock shipped to the US to cover upcoming demand ***")
    else:
        print("\nNo shortfalls detected against the supplied PO demand file.")

    print(
        "\nNOTE: this only counts demand from the supplemental PO file. Camelot-sourced "
        "open-PO data is not yet wired in (see module docstring) — confirm with the team "
        "which Camelot call represents true upcoming demand before relying on this alone."
    )


if __name__ == "__main__":
    main()

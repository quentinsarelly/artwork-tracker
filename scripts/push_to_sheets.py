"""
Push the old vs. new packaging report to Google Sheets — one tab per
warehouse ("US" and "MX"), one row per product with columns for
available quantity in old / new packaging, plus an "Unknown" column
counting stock whose batch code is missing from the Mapping tab (that
row is flagged in a separate column — nothing is silently dropped).
Each run clears and rewrites the tabs in place, so the sheet is always
a live view of the latest exports.

Data source is identical to packaging_report.py: the most recent lot-level
export in data/camelot_exports/ (US) / data/shiphero_exports/ (MX) joined
against the batch -> packaging mapping. The mapping lives in the target
spreadsheet's "Mapping" tab (columns: item/SKU, batch/lot code, packaging
old/new); pass --mapping to use a local file instead.

A third tab ("SKU List") holds the minimum-coverage list: every SKU on
it is guaranteed to appear on BOTH warehouse tabs, zero-filled if it
has no stock or is missing from an export. A fourth tab ("SKU
Coverage") outer-joins both raw exports on SKU and flags each as Both /
US only / MX only / Neither, with a Required column — mismatches first.

Auth: gspread's default OAuth flow. First run opens a browser to log in
to the Google account that owns (or can edit) the target spreadsheet;
the token is cached at ~/.config/gspread/authorized_user.json.

Setup:
    Put OAuth client credentials at ~/.config/gspread/credentials.json
    (Google Cloud console -> APIs & Services -> Credentials -> OAuth
    client ID -> Desktop app; enable the Google Sheets API + Drive API).

Usage:
    python scripts/push_to_sheets.py                     # both warehouses
    python scripts/push_to_sheets.py --dry-run           # print, don't push
    python scripts/push_to_sheets.py --warehouse US --export ... --mapping ...
"""

import argparse
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from packaging_report import (
    ITEM_ALIASES,
    WAREHOUSES,
    _find_col,
    _latest_file,
    load_export,
    load_mapping,
    load_mapping_df,
)

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

MAPPING_TAB = "Mapping"
COVERAGE_TAB = "SKU Coverage"
SKU_LIST_TAB = "SKU List"


def build_available_table(
    inventory: pd.DataFrame,
    mapping: pd.DataFrame,
    required_skus: list[str] | None = None,
) -> pd.DataFrame:
    """Per-product available qty split old/new/unknown packaging.

    Batches missing from the Mapping tab are counted in the Unknown
    column and flagged in a separate column — never silently dropped.
    Scope: products named somewhere in the mapping (so unrelated
    warehouse stock doesn't show up as all-Unknown noise) PLUS every
    SKU on the required list, zero-filled if it has no stock or is
    missing from the export entirely.
    """
    required_skus = required_skus or []
    scope_items = set(mapping["item"]) | set(required_skus)
    inv = inventory[inventory["item"].isin(scope_items)]

    merged = inv.merge(mapping, on=["item", "batch"], how="left")
    merged["packaging"] = merged["packaging"].fillna("unknown")

    pivot = (
        merged.groupby(["item", "packaging"])["available_qty"]
        .sum()
        .unstack(fill_value=0)
    )
    for col in ("old", "new", "unknown"):
        if col not in pivot.columns:
            pivot[col] = 0

    unknown_detail = merged[merged["packaging"] == "unknown"]
    if unknown_detail.empty:
        flags = pd.DataFrame(columns=["item", "unmapped_batches"])
    else:
        flags = (
            unknown_detail.groupby("item")
            .apply(
                lambda g: ", ".join(
                    f"{b} ({q})" for b, q in zip(g["batch"], g["available_qty"])
                ),
                include_groups=False,
            )
            .rename("unmapped_batches")
            .reset_index()
        )

    table = pivot.reset_index()[["item", "old", "new", "unknown"]].merge(
        flags, on="item", how="left"
    )
    table["unmapped_batches"] = table["unmapped_batches"].fillna("")
    table.columns = [
        "Item",
        "Available Qty Old Packaging",
        "Available Qty New Packaging",
        "Available Qty Unknown Packaging",
        "Unmapped Batch(es) — add to Mapping tab",
    ]

    # Zero-fill required SKUs that have no rows at all (no stock, or not
    # in this warehouse's export) so minimum coverage always holds.
    missing_required = [s for s in required_skus if s not in set(table["Item"])]
    if missing_required:
        zeros = pd.DataFrame({"Item": missing_required})
        for col in table.columns[1:-1]:
            zeros[col] = 0
        zeros[table.columns[-1]] = ""
        table = pd.concat([table, zeros], ignore_index=True)

    return table.sort_values(
        ["Available Qty Old Packaging", "Item"], ascending=[False, True]
    ).reset_index(drop=True)


def build_coverage_table(
    inventory_us: pd.DataFrame,
    inventory_mx: pd.DataFrame,
    required_skus: list[str] | None = None,
) -> pd.DataFrame:
    """Outer-join the two raw exports on SKU: which SKUs exist in which
    warehouse. Covers ALL SKUs in the exports (not just mapping-scope
    ones) plus every required SKU — a SKU in one warehouse but not the
    other, or on the list with no stock anywhere, is exactly what we
    don't want to miss."""
    required_skus = required_skus or []
    us = inventory_us.groupby("item", as_index=False)["available_qty"].sum()
    mx = inventory_mx.groupby("item", as_index=False)["available_qty"].sum()

    merged = us.merge(mx, on="item", how="outer", suffixes=(" US", " MX"))

    # Required SKUs absent from both exports still need a row.
    missing_required = sorted(set(required_skus) - set(merged["item"]))
    if missing_required:
        merged = pd.concat(
            [merged, pd.DataFrame({"item": missing_required})], ignore_index=True
        )

    us_skus, mx_skus = set(us["item"]), set(mx["item"])
    merged["presence"] = merged["item"].map(
        lambda i: "Both"
        if i in us_skus and i in mx_skus
        else "US only"
        if i in us_skus
        else "MX only"
        if i in mx_skus
        else "Neither"
    )
    merged[["available_qty US", "available_qty MX"]] = (
        merged[["available_qty US", "available_qty MX"]].fillna(0).astype(int)
    )
    merged["required"] = (
        merged["item"].isin(required_skus).map({True: "Yes", False: ""})
    )
    merged.columns = [
        "Item",
        "Available Qty (US)",
        "Available Qty (MX)",
        "Presence",
        "Required",
    ]

    # Mismatches first (the actionable rows), then alphabetical.
    return (
        merged.assign(_mismatch=merged["Presence"].ne("Both"))
        .sort_values(["_mismatch", "Item"], ascending=[False, True])
        .drop(columns="_mismatch")
        .reset_index(drop=True)
    )


def _client_config_from_env() -> dict:
    return {
        "installed": {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def open_spreadsheet(sheet_id: str | None, sheet_name: str | None):
    import json

    import gspread

    if os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"):
        # Client id/secret from .env; cache the granted token at the
        # standard gspread location so the browser flow only runs once.
        token_cache = Path.home() / ".config" / "gspread" / "authorized_user.json"
        authorized_user_info = None
        if token_cache.exists():
            authorized_user_info = json.loads(token_cache.read_text())
        gc, user_info = gspread.oauth_from_dict(
            credentials=_client_config_from_env(),
            authorized_user_info=authorized_user_info,
        )
        if not authorized_user_info:
            token_cache.parent.mkdir(parents=True, exist_ok=True)
            # oauth_from_dict returns the safe-to-store info as a JSON string
            token_cache.write_text(user_info)
    else:
        # Fall back to gspread's default: ~/.config/gspread/credentials.json
        gc = gspread.oauth()

    if sheet_id:
        return gc.open_by_key(sheet_id)
    if not sheet_name:
        raise SystemExit(
            "No spreadsheet specified — set GOOGLE_SHEET_ID (or GOOGLE_SHEET_NAME) "
            "in .env, or pass --sheet-id/--sheet-name."
        )
    try:
        return gc.open(sheet_name)
    except gspread.SpreadsheetNotFound:
        raise SystemExit(
            f"No spreadsheet named '{sheet_name}' is visible to this account. "
            "Create it (or share it with edit access) and retry, or use GOOGLE_SHEET_ID."
        )
    try:
        return gc.open(sheet_name)
    except gspread.SpreadsheetNotFound:
        raise SystemExit(
            f"No spreadsheet named '{sheet_name}' is visible to this account. "
            "Create it (or share it with edit access) and retry, or use GOOGLE_SHEET_ID."
        )


def load_sku_list_worksheet(spreadsheet) -> list[str]:
    """Read the minimum-coverage SKU list from the spreadsheet's SKU List tab."""
    import gspread

    try:
        ws = spreadsheet.worksheet(SKU_LIST_TAB)
    except gspread.WorksheetNotFound:
        raise SystemExit(
            f"No '{SKU_LIST_TAB}' tab found in spreadsheet '{spreadsheet.title}' — add it "
            "(one column of SKUs, header e.g. 'Item') so both warehouse tabs can be "
            "guaranteed to cover them, or remove the tab requirement."
        )
    values = ws.get_all_values()
    if len(values) < 2:
        raise SystemExit(
            f"The '{SKU_LIST_TAB}' tab in '{spreadsheet.title}' has no data rows."
        )
    header, *rows = values
    df = pd.DataFrame(rows, columns=header)
    col = _find_col(list(df.columns), ITEM_ALIASES, "item/SKU")
    skus = df[col].str.strip()
    return sorted(set(skus) - {""})


def load_mapping_worksheet(spreadsheet) -> pd.DataFrame:
    """Read the batch -> packaging mapping from the spreadsheet's Mapping tab."""
    import gspread

    try:
        ws = spreadsheet.worksheet(MAPPING_TAB)
    except gspread.WorksheetNotFound:
        raise SystemExit(
            f"No '{MAPPING_TAB}' tab found in spreadsheet '{spreadsheet.title}' — add it "
            "(columns: item/SKU, batch/lot code, packaging old/new) or pass --mapping <file>."
        )
    values = ws.get_all_values()
    if len(values) < 2:
        raise SystemExit(
            f"The '{MAPPING_TAB}' tab in '{spreadsheet.title}' has no data rows."
        )
    header, *rows = values
    # Built manually from raw cell strings (no pandas NA coercion), so
    # literal "NA" batch codes survive intact.
    return load_mapping_df(pd.DataFrame(rows, columns=header))


def ensure_worksheet(spreadsheet, title: str):
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        print(f"Creating tab '{title}'...")
        return spreadsheet.add_worksheet(title=title, rows=100, cols=10)


def push_table(ws, df: pd.DataFrame) -> None:
    header = list(df.columns)
    rows = []
    for row in df.itertuples(index=False):
        cells = []
        for v in row:
            if pd.isna(v):
                cells.append("")
            elif hasattr(v, "item"):  # numpy scalar -> native int/float for JSON
                cells.append(v.item())
            else:
                cells.append(v)
        rows.append(cells)
    ws.clear()
    # USER_ENTERED so numeric-looking values become real numbers (usable
    # in formulas), not text.
    ws.update(
        values=[header] + rows, range_name="A1", value_input_option="USER_ENTERED"
    )


def run_for_warehouse(
    warehouse: str,
    export: Path | None,
    mapping: Path | None,
    dry_run: bool,
    spreadsheet=None,
    inventories: dict | None = None,
    required_skus: list[str] | None = None,
) -> None:
    """Push one warehouse's tab."""
    label = WAREHOUSES[warehouse]["label"]
    print(f"\n=== {label} ===")

    export_path = export or _latest_file(WAREHOUSES[warehouse]["export_dir"])
    print(f"Inventory export: {export_path}")

    inventory = load_export(export_path)
    if inventories is not None:
        inventories[warehouse] = inventory
    if mapping is not None:
        mapping_df = load_mapping(mapping)
        print(f"Mapping file:     {mapping}")
    else:
        mapping_df = load_mapping_worksheet(spreadsheet)
        print(f"Mapping source:   '{spreadsheet.title}' -> '{MAPPING_TAB}' tab")

    table = build_available_table(inventory, mapping_df, required_skus)

    if dry_run:
        print(f"\nWould write {len(table)} products to tab '{warehouse}':")
        print(table.to_string(index=False))
    else:
        ws = ensure_worksheet(spreadsheet, warehouse)
        push_table(ws, table)
        print(f"Wrote {len(table)} products to tab '{warehouse}'")

    unknown_items = table[table["Available Qty Unknown Packaging"] > 0]
    if not unknown_items.empty:
        print(
            f"\nNOTE: {len(unknown_items)} product(s) include stock with batch(es) missing "
            f"from the '{MAPPING_TAB}' tab — counted in 'Available Qty Unknown Packaging' "
            "and flagged per row."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--warehouse",
        choices=["US", "MX", "ALL"],
        default="ALL",
        help="Which warehouse tab to update",
    )
    parser.add_argument(
        "--sheet-id",
        default=os.getenv("GOOGLE_SHEET_ID"),
        help="Target spreadsheet ID (overrides GOOGLE_SHEET_ID)",
    )
    parser.add_argument(
        "--sheet-name",
        default=os.getenv("GOOGLE_SHEET_NAME"),
        help="Target spreadsheet by title (if no ID given)",
    )
    parser.add_argument(
        "--export",
        type=Path,
        default=None,
        help="Inventory export file (only valid with a single --warehouse)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=None,
        help="Batch -> packaging mapping file (overrides the spreadsheet's 'Mapping' tab)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the tables without touching Google Sheets",
    )
    args = parser.parse_args()

    warehouses = ["US", "MX"] if args.warehouse == "ALL" else [args.warehouse]
    if len(warehouses) > 1 and args.export:
        parser.error("--export requires a single --warehouse (US or MX), not ALL")

    # The spreadsheet is needed for pushing, and also for dry runs that
    # read the mapping from its "Mapping" tab.
    spreadsheet = None
    if not args.dry_run or args.mapping is None:
        spreadsheet = open_spreadsheet(args.sheet_id, args.sheet_name)

    inventories: dict = {}
    required_skus: list[str] = []
    if spreadsheet is not None:
        required_skus = load_sku_list_worksheet(spreadsheet)
        print(
            f"Required SKUs:    {len(required_skus)} from '{spreadsheet.title}' -> '{SKU_LIST_TAB}' tab"
        )
    else:
        print(
            "NOTE: no spreadsheet opened (--dry-run with --mapping) — SKU-list enforcement skipped."
        )

    for warehouse in warehouses:
        run_for_warehouse(
            warehouse,
            args.export,
            args.mapping,
            args.dry_run,
            spreadsheet,
            inventories,
            required_skus,
        )

    # SKU coverage needs both raw exports; skip with a note if either is
    # unavailable (e.g. single-warehouse run where the other folder is empty).
    if len(inventories) < 2:
        print(
            f"\nSkipping '{COVERAGE_TAB}' — needs both warehouses' exports "
            f"(loaded: {', '.join(sorted(inventories)) or 'none'})."
        )
        return

    coverage = build_coverage_table(inventories["US"], inventories["MX"], required_skus)
    counts = coverage["Presence"].value_counts()
    both = int(counts.get("Both", 0))
    us_only = int(counts.get("US only", 0))
    mx_only = int(counts.get("MX only", 0))
    summary = (
        f"SKU presence across warehouses: {both} in both, {us_only} US-only, "
        f"{mx_only} MX-only ({len(coverage)} SKUs total). Mismatches listed first."
    )
    required_missing = coverage[
        (coverage["Required"] == "Yes") & (coverage["Presence"] != "Both")
    ]
    if not required_missing.empty:
        summary += (
            f"\n*** {len(required_missing)} REQUIRED SKU(s) not in both warehouses: "
            + ", ".join(
                f"{r.Item} ({r.Presence})" for r in required_missing.itertuples()
            )
            + " ***"
        )

    # SKU coverage needs both raw exports; skip with a note if either is
    # unavailable (e.g. single-warehouse run where the other folder is empty).
    if len(inventories) < 2:
        print(
            f"\nSkipping '{COVERAGE_TAB}' — needs both warehouses' exports "
            f"(loaded: {', '.join(sorted(inventories)) or 'none'})."
        )
        return

    coverage = build_coverage_table(inventories["US"], inventories["MX"])
    counts = coverage["Presence"].value_counts()
    both = int(counts.get("Both", 0))
    us_only = int(counts.get("US only", 0))
    mx_only = int(counts.get("MX only", 0))
    summary = (
        f"SKU presence across warehouses: {both} in both, {us_only} US-only, "
        f"{mx_only} MX-only ({len(coverage)} SKUs total). Mismatches listed first."
    )

    if args.dry_run:
        print(f"\n=== {COVERAGE_TAB} ===\n{summary}")
        print(coverage.to_string(index=False))
    else:
        ws = ensure_worksheet(spreadsheet, COVERAGE_TAB)
        push_table(ws, coverage)
        print(f"\nWrote {COVERAGE_TAB}: {summary}")


if __name__ == "__main__":
    main()

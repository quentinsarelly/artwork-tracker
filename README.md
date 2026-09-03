# us-logistics

Tools for managing US logistics: Camelot (3PL/WMS) inventory reporting
and PO/replenishment tracking.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in CAMELOT_* credentials
```

## Old vs. new packaging report

Covers both warehouses: US (Camelot) and MX (ShipHero). Both can now be
pulled lot-level straight from their APIs — see
`scripts/pull_camelot_lot_extract.py` (US) and
`scripts/pull_shiphero_extract.py` (MX) — or fed with a manual export.

1. Get a lot-level inventory report into the matching folder:
   `data/camelot_exports/` for US, `data/shiphero_exports/` for MX
   (run the API pull below, or drop in a manual export).
2. Put the batch-code -> product -> old/new packaging mapping file in
   `data/mapping/`. One mapping file covers both warehouses' batch codes.
3. Run:

   ```bash
   .venv/bin/python scripts/packaging_report.py --warehouse US    # or MX, or ALL
   .venv/bin/python scripts/build_share_report.py --warehouse US  # formatted Excel version
   ```

   `--warehouse` defaults to `US`. `ALL` runs both and writes two
   separate report files (not a combined one). Each picks the most
   recently modified file in its warehouse's export folder by default;
   pass `--export` / `--mapping` to target specific files (only valid
   with a single `--warehouse`, not `ALL`). Output goes to `reports/`,
   named with the warehouse code. Any batch code present in an export but
   missing from the mapping is flagged (not silently dropped) and the
   script exits non-zero so it can't be missed.

## US inventory pull via Camelot API

```bash
.venv/bin/python scripts/pull_camelot_lot_extract.py
```

Pulls lot-level stock live and writes
`data/camelot_exports/camelot_api_<timestamp>.csv`, which downstream
packaging scripts pick up automatically as the latest US export — it
replaces the manual "WebLink - Lot Inventory" download from the Camelot
UI and is written with the same column names.

Same `GetAvailableInventory` call as the item-level pull, but with the
piece-inventory interface profile (`SAR_PINV_E`, override with
`CAMELOT_PIECE_PROFILE`), which swaps the response payload for XMLPort
37005332. **Camelot asks that this be pulled no more than once or twice
a day**, since it splits rows down to the pallet.

Two differences from the WebLink report to be aware of:

- More rows for the same stock: one row per item + lot + receipt + bin +
  inventory status, where the UI report collapses those. Totals per
  item/lot are identical.
- No `QtyAvailableToOrder` field in this payload, so `Available Qty` is
  reconstructed as `CountQty - CountQtyCommit`, with rows carrying a
  non-blank `InvStatus` (e.g. QC hold) counted as zero-available and
  their quantity reported under `Status Qty`. Verified against the
  manual export: this reproduces the UI's `Available Qty` exactly.

Camelot's `NA` placeholder lot passes through verbatim and buckets as
"Needs Lot Number" downstream, never as a real artwork version.

## MX inventory pull via ShipHero API

```bash
.venv/bin/python scripts/pull_shiphero_extract.py
```

Pulls lot-level stock live (GraphQL `item_locations`: one row per SKU
per bin per lot — real binned stock, kits excluded) and writes
`data/shiphero_exports/shiphero_api_<timestamp>.csv`, which downstream
packaging scripts pick up automatically as the latest MX export.
Requires `SHIPHERO_REFRESH_TOKEN` in `.env` (same one
inventory-snapshot's mx_3pl connector uses). Lot-less bins are written
with an empty lot and the SINLOTE placeholder passes through verbatim —
both bucket as "Needs Lot Number" downstream, never as a real artwork
version.

## MX lot-tracking discrepancy check

ShipHero's SKU-level quantity export (total on-hand per SKU) doesn't
always match the sum of the lot-level inventory export used above —
some units have no lot record in ShipHero at all. That's a different,
more severe issue than "Needs Lot Number" in the packaging report, which
only catches units that *are* in the lot extract but tagged with the
`SINLOTE` placeholder — units missing from the lot extract entirely
don't show up there because they're absent from that file, not
present-with-a-placeholder.

1. Get a SKU-level quantity export into `data/shiphero_sku_totals/`
   (all SKUs, total on-hand qty, no lot breakdown):

   ```bash
   .venv/bin/python scripts/pull_shiphero_sku_totals.py
   ```

   Pulls live via ShipHero's `warehouse_products` query (same one
   inventory-snapshot's mx_3pl connector uses) and writes
   `shiphero_sku_totals_api_<timestamp>.csv`, which this check picks up
   automatically as the latest file — or drop in a manual export
   instead. Unlike `pull_shiphero_extract.py`'s `item_locations`, this
   includes kits (`on_hand` double-counts virtual bundles), which is
   exactly why the comparison below catches them as a "discrepancy" —
   same as a manual export would.

   For results to mean anything, pull both extracts back-to-back —
   `pull_shiphero_sku_totals.py` then `pull_shiphero_extract.py` (or
   vice versa) — since "Lot Total Exceeds SKU Total" below is mostly
   just a symptom of the two being pulled minutes/hours apart.
2. Run:

   ```bash
   .venv/bin/python scripts/mx_lot_discrepancy_check.py
   ```

   Picks the most recent file in `data/shiphero_sku_totals/` and
   `data/shiphero_exports/` by default; pass `--sku-totals` /
   `--lot-export` to target specific files. Output is an Excel file in
   `reports/` with two tabs: SKUs missing from the lot data entirely
   (the real warehouse action item), and SKUs where the lot extract
   exceeds the SKU extract (usually just means the two exports weren't
   pulled at the same time, not a real issue).

## Replenishment / PO coverage check

Pulls current on-hand inventory live from Camelot and compares it against
upcoming PO demand from `data/po/` (a supplemental spreadsheet — Camelot's
API doesn't cleanly expose a forward-looking open-PO queue, only
shipped/staged order status, so that source isn't wired in yet).

```bash
.venv/bin/python scripts/replenishment_check.py
```

Flags any product where demand exceeds on-hand inventory, meaning more
stock needs to ship from origin to the US.

## Google Sheets push

Pushes the packaging report (available qty per product, split old/new
packaging) to Google Sheets — one tab per warehouse (`US` and `MX`),
cleared and rewritten on every run. A third tab (**SKU List**) is the
minimum-coverage list you maintain: every SKU on it is forced onto both
warehouse tabs (zeros if no stock). A fourth tab (**SKU Coverage**)
outer-joins both raw exports on SKU and flags each as Both / US only /
MX only / Neither, with a Required column (mismatches first), so a SKU
stocked in one warehouse but not the other — or a required SKU with no
stock anywhere — can't go unnoticed. The batch -> packaging mapping is
read from the same spreadsheet's **`Mapping`** tab (columns: item/SKU,
batch/lot code, packaging old/new) so it can be maintained in one place;
`--mapping <file>` overrides with a local file instead.

One-time setup:

1. In the Google Cloud console, create an OAuth client ID
   (Desktop app) with the Sheets API and Drive API enabled.
2. Put its client ID and secret in `.env` as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` (or download the client-secret JSON to
   `~/.config/gspread/credentials.json` instead).
3. Create (or pick) a spreadsheet containing a `Mapping` tab, and put its
   ID in `.env` as `GOOGLE_SHEET_ID` (or just its title as
   `GOOGLE_SHEET_NAME`).

Then run:

```bash
.venv/bin/python scripts/push_to_sheets.py              # both warehouses
.venv/bin/python scripts/push_to_sheets.py --dry-run    # preview, no push
```

The first run opens a browser to authorize once; the token is cached at
`~/.config/gspread/authorized_user.json`. Uses the same inputs as
`packaging_report.py`, but batches missing from the mapping are counted
in an "Unknown" column and flagged per row (not excluded).

## Camelot credentials

Shared with `apisandbox` and `inventory-snapshot-system` — same Excalibur
SOAP endpoint, same interface profile (`SAR_ITEM_E`) and client
(`SARELLY`).

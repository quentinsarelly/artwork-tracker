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

Covers both warehouses: US (Camelot) and MX (ShipHero). Camelot's SOAP
API (`GetAvailableInventory`) does not expose a lot/batch field —
confirmed both in the API docs and live (see `spike_check_lot_fields.py`)
— so batch-level detail for both warehouses comes from **manual exports**
rather than a live API pull.

1. Export a lot-level inventory report and drop it in the matching
   folder: `data/camelot_exports/` for US, `data/shiphero_exports/` for
   MX.
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

## Camelot credentials

Shared with `apisandbox` and `inventory-snapshot-system` — same Excalibur
SOAP endpoint, same interface profile (`SAR_ITEM_E`) and client
(`SARELLY`).

#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== Packaging Report Pipeline ==="
echo "Started: $(date)"
echo

# Activate venv
source .venv/bin/activate

echo "[1/6] Pulling US lot-level inventory from Camelot..."
python scripts/pull_camelot_lot_extract.py
echo

echo "[2/6] Pulling MX lot-level inventory from ShipHero..."
python scripts/pull_shiphero_extract.py
echo

echo "[3/6] Pulling MX SKU totals from ShipHero..."
python scripts/pull_shiphero_sku_totals.py
echo

echo "[4/6] Running MX lot discrepancy check..."
python scripts/mx_lot_discrepancy_check.py
echo

echo "[5/6] Building formatted share reports..."
python scripts/build_share_report.py --warehouse US
python scripts/build_share_report.py --warehouse MX
echo

echo "[6/6] Pushing to Google Sheets..."
python scripts/push_to_sheets.py
echo

echo "=== Done ==="
echo "Finished: $(date)"

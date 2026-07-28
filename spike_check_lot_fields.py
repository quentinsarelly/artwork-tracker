"""
One-off spike: check whether Camelot's live SOAP responses carry any
lot/batch/serial field that isn't documented in the API PDF or reflected
in the existing inventory-snapshot-system connector (which only maps
ItemNumber/QtyOnHand/QtyReserved/QtyAvailable).

Usage:
    python spike_check_lot_fields.py                    # dump inventory tags
    python spike_check_lot_fields.py --item <ITEM_NO>    # filter to one item
    python spike_check_lot_fields.py --receipt <RECEIPT_NO>  # also check a receipt

Delete this script once the question is settled (see plan Phase A step 2).
"""

import argparse
import os
import sys
from xml.etree import ElementTree as ET

from camelot_client import CamelotError, client

# The working us_3pl.py connector passes pClientFilter=CAMELOT_CLIENT explicitly;
# an empty filter is rejected by the SAR_ITEM_E interface profile ("does not have
# permission to call this function for client .").
_CLIENT_FILTER = os.getenv("CAMELOT_CLIENT", "")

LOT_KEYWORDS = ("lot", "batch", "serial", "expir")


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1]


def _flag_lot_like(tag_names: set[str]) -> list[str]:
    return [t for t in tag_names if any(k in t.lower() for k in LOT_KEYWORDS)]


def check_inventory(item: str) -> None:
    print("=== GetAvailableInventory ===")
    try:
        doc = client.get_available_inventory(client_filter=_CLIENT_FILTER, item=item)
    except CamelotError as e:
        print(f"SOAP Fault: {e}")
        return

    if doc is None:
        print("(no data returned)")
        return

    ET.indent(doc)
    print(ET.tostring(doc, encoding="unicode"))

    tag_names: set[str] = set()
    for el in doc.iter():
        tag_names.add(_local_tag(el.tag))

    print(f"\nAll distinct tag names seen: {sorted(tag_names)}")
    hits = _flag_lot_like(tag_names)
    if hits:
        print(f"\n*** POSSIBLE LOT/BATCH FIELDS FOUND: {hits} ***")
    else:
        print("\nNo lot/batch/serial/expiration-like tag names found.")


def check_receipt(receipt_number: str) -> None:
    print(f"\n=== GetReceiptStatusDetailed({receipt_number!r}) ===")
    try:
        doc = client.get_receipt_status_detailed(receipt_number)
    except CamelotError as e:
        print(f"SOAP Fault: {e}")
        return

    if doc is None:
        print("(no data returned)")
        return

    ET.indent(doc)
    print(ET.tostring(doc, encoding="unicode"))

    tag_names = {_local_tag(el.tag) for el in doc.iter()}
    print(f"\nAll distinct tag names seen: {sorted(tag_names)}")
    hits = _flag_lot_like(tag_names)
    if hits:
        print(f"\n*** POSSIBLE LOT/BATCH FIELDS FOUND: {hits} ***")
    else:
        print("\nNo lot/batch/serial/expiration-like tag names found.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", default="", help="Filter inventory to a single item/SKU")
    parser.add_argument("--receipt", default="", help="Also check GetReceiptStatusDetailed for this receipt number")
    args = parser.parse_args()

    print("TestConnection:")
    try:
        print(client.test_connection())
    except CamelotError as e:
        print(f"SOAP Fault: {e}", file=sys.stderr)
        sys.exit(1)
    print()

    check_inventory(args.item)

    if args.receipt:
        check_receipt(args.receipt)
    else:
        print("\n(no --receipt given — pass a known receipt number to also check GetReceiptStatusDetailed)")


if __name__ == "__main__":
    main()

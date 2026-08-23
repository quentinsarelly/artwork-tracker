"""
ShipHero (MX 3PL) GraphQL API client.

Same auth pattern as inventory-snapshot's connectors/mx_3pl.py: a
refresh token (SHIPHERO_REFRESH_TOKEN in .env) is exchanged at
/auth/refresh for a short-lived access token, cached until expiry.

The load-bearing read for artwork tracking is `item_locations`: one row
per SKU per bin per lot — real physical stock in bins. Kits don't appear
(only their components do), unlike warehouse_products.on_hand which
double-counts virtual bundles (see inventory-snapshot/ARTWORK_TRACKING.md).

Lot-less bins come back with expiration_lot = null, and ShipHero also has
a literal lot named "SINLOTE" meaning "no lot" — both must stay
distinguishable from real lots downstream.

Usage:
    from shiphero_client import iter_item_locations
    for row in iter_item_locations():
        print(row["sku"], row["expiration_lot"], row["quantity"])
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://public-api.shiphero.com/auth"
GRAPHQL_URL = "https://public-api.shiphero.com/graphql"

_cached_token: dict = {}


class ShipHeroError(Exception):
    pass


def _get_token() -> str:
    global _cached_token
    if _cached_token.get("expires_at", 0) > time.time() + 60:
        return _cached_token["access_token"]
    refresh_token = os.getenv("SHIPHERO_REFRESH_TOKEN")
    if not refresh_token:
        raise SystemExit(
            "SHIPHERO_REFRESH_TOKEN not set — add it to .env "
            "(same refresh token inventory-snapshot uses for mx_3pl)."
        )
    resp = requests.post(
        f"{AUTH_URL}/refresh",
        json={"refresh_token": refresh_token},
        timeout=30,
    )
    if not resp.ok:
        raise ShipHeroError(f"ShipHero auth failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    _cached_token = {
        "access_token": data["access_token"],
        "expires_at": time.time() + data.get("expires_in", 28 * 24 * 3600),
    }
    return _cached_token["access_token"]


def query(gql: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": gql, "variables": variables or {}},
        headers={
            "Authorization": f"Bearer {_get_token()}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    if not resp.ok:
        raise ShipHeroError(f"ShipHero query failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    if "errors" in data:
        raise ShipHeroError(f"ShipHero GraphQL error: {data['errors']}")
    return data.get("data") or {}


# One row per SKU per bin per lot. Pagination lives on the inner `data`
# field. sku: null = all SKUs; a full sweep of ~280 SKUs costs roughly a
# dozen calls / ~1.2k complexity credits (pool ~4k, refills 60/sec).
ITEM_LOCATIONS_QUERY = """
query IL($sku: [String], $after: String) {
    item_locations(sku: $sku, has_inventory: true) {
        complexity
        data(first: 100, after: $after) {
            edges {
                node {
                    sku
                    quantity
                    expiration_lot { id name expires_at }
                    location { id name pickable sellable }
                }
            }
            pageInfo { hasNextPage endCursor }
        }
    }
}
"""


def iter_item_locations(sku: list[str] | None = None) -> "iter[dict]":
    """Yield one node per SKU/bin/lot holding stock, across all pages."""
    cursor: str | None = None
    while True:
        variables: dict = {"sku": sku}
        if cursor:
            variables["after"] = cursor
        data = query(ITEM_LOCATIONS_QUERY, variables)
        page = data["item_locations"]["data"]
        for edge in page["edges"]:
            yield edge["node"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]

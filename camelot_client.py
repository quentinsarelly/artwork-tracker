"""
Camelot 3PL API client — SOAP/Excalibur (Dynamics NAV) web service.

Adapted from the working client in apisandbox/camelot.py. Trimmed to the
read-only calls us-logistics needs (inventory, order/receipt status).

Auth: HTTP Basic Authentication (External User created in Excalibur with
License Type = External User).
Transport: SOAP — all calls go to a single Codeunit endpoint
(TPLWebServiceInt).

Date format used by range functions: MMDDYYYY, T (today), or XD-T
(X days before today). Examples: "T" = today, "7D-T" = 7 days ago.

Usage:
    from camelot_client import client
    print(client.test_connection())
    inventory = client.get_available_inventory()
"""

import os
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

_SOAP_URL = os.getenv("CAMELOT_SOAP_URL")
_USERNAME = os.getenv("CAMELOT_USERNAME")
_PASSWORD = os.getenv("CAMELOT_PASSWORD")

_NS = "urn:microsoft-dynamics-schemas/codeunit/TPLWebServiceInt"
_PIECE_PROFILE = os.getenv("CAMELOT_PIECE_PROFILE", "SAR_PINV_E")
_NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"

_ENVELOPE = """\
<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{action} xmlns="{ns}">
      {params}
    </{action}>
  </soap:Body>
</soap:Envelope>"""


class CamelotError(Exception):
    pass


def _call(action: str, params: dict) -> ET.Element:
    """Send a SOAP call and return the parsed response action element."""
    param_xml = "\n      ".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    envelope = _ENVELOPE.format(action=action, ns=_NS, params=param_xml)
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{_NS}"',
    }
    resp = requests.get(
        _SOAP_URL,
        data=envelope.encode("utf-8"),
        headers=headers,
        auth=HTTPBasicAuth(_USERNAME, _PASSWORD),
        timeout=60,
    )

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        print(f"[Camelot {resp.status_code}] {resp.text}")
        resp.raise_for_status()
        raise

    body = root.find(f"{{{_NS_SOAP}}}Body")

    # SOAP faults arrive as HTTP 500 with a structured Fault element
    fault = body.find(f"{{{_NS_SOAP}}}Fault")
    if fault is not None:
        msg = fault.findtext("faultstring") or ET.tostring(fault, encoding="unicode")
        raise CamelotError(msg)

    if not resp.ok:
        print(f"[Camelot {resp.status_code}] {resp.text}")
        resp.raise_for_status()

    return list(body)[0]


def _text(element: ET.Element, tag: str) -> str:
    """Return text of the first matching tag, trying the service namespace first."""
    return (
        element.findtext(f".//{{{_NS}}}{tag}")
        or element.findtext(f".//{tag}")
        or ""
    )


def _parse_xml_doc(xml_string: str) -> ET.Element | None:
    """Parse the XML string returned in pXMLDoc fields."""
    if not xml_string:
        return None
    try:
        return ET.fromstring(xml_string)
    except ET.ParseError:
        return None


class CamelotClient:
    def __init__(self, interface_profile: str = "", client_code: str = "", trading_partner: str = "", piece_profile: str = ""):
        self.profile = interface_profile or os.getenv("CAMELOT_INTERFACE_PROFILE", "")
        self.piece_profile = piece_profile or _PIECE_PROFILE
        self.client = client_code or os.getenv("CAMELOT_CLIENT", "")
        self.partner = trading_partner or os.getenv("CAMELOT_TRADING_PARTNER", "")

    def _base(self) -> dict:
        """Common params shared by most functions; pXMLDoc is output (filled by server)."""
        return {
            "pInterfaceProfile": self.profile,
            "pClient": self.client,
            "pTradingPartner": self.partner,
            "pXMLDoc": "",
        }

    # -----------------------------------------------------------------------
    # Connection
    # -----------------------------------------------------------------------

    def test_connection(self) -> str:
        """TestConnection() — verify the SOAP endpoint is reachable."""
        resp = _call("TestConnection", {})
        return _text(resp, "return_value")

    # -----------------------------------------------------------------------
    # Read — inventory
    # -----------------------------------------------------------------------

    def get_available_inventory(self, client_filter: str = "", item: str = "") -> ET.Element | None:
        """GetAvailableInventory — XML inventory for all items, or filter by client/item.
        Standard interface object: XMLPort 37005331 PW Item Inventory Export.
        Known response fields: ItemNumber, QtyOnHand, QtyReserved, QtyAvailable.
        Item-level only — for lot/batch detail use get_piece_inventory().
        """
        params = {**self._base(), "pClientFilter": client_filter, "pItem": item}
        resp = _call("GetAvailableInventory", params)
        return _parse_xml_doc(_text(resp, "pXMLDoc"))

    def get_piece_inventory(
        self, client_filter: str = "", item: str = "", interface_profile: str = ""
    ) -> ET.Element | None:
        """Lot-level inventory — same GetAvailableInventory call, different
        interface profile (SAR_PINV_E), which swaps the response payload for
        XMLPort 37005332 (Transaction Type="Piece Inventory").

        One row per item + lot + receipt + bin + inventory status, so an
        item/lot can span several rows. Fields: Item, Lot, Whse, Bin,
        InvStatus, CountQty, CountQtyCommit, CountUnit, Alt1*, GrsWgt,
        Receipt, RececiptDate (Camelot's spelling), PieceCodeDate.

        Note this payload has no QtyAvailableToOrder — availability is
        recomputed downstream as CountQty - CountQtyCommit, with rows
        carrying a non-blank InvStatus (e.g. QC) counted as unavailable.
        Camelot asks that this be pulled no more than once or twice a day.
        """
        params = {**self._base(), "pClientFilter": client_filter, "pItem": item}
        params["pInterfaceProfile"] = interface_profile or self.piece_profile
        resp = _call("GetAvailableInventory", params)
        return _parse_xml_doc(_text(resp, "pXMLDoc"))

    # -----------------------------------------------------------------------
    # Read — order / receipt status
    # -----------------------------------------------------------------------

    def get_order_status_date_range(self, begin_date: str = "T", end_date: str = "T") -> ET.Element | None:
        """GetOrderStatusDateRange — orders shipped within a date range.
        Standard interface object: XMLPort 37036601 PW Expand XML Shipment Export.
        """
        params = {**self._base(), "pBeginDate": begin_date, "pEndDate": end_date}
        resp = _call("GetOrderStatusDateRange", params)
        return _parse_xml_doc(_text(resp, "pXMLDoc"))

    def get_transaction_status_time_range(self, begin_date: str = "T", end_date: str = "T") -> ET.Element | None:
        """GetTransactionStatusTimeRange — transaction status within a time range.
        Standard interface objects: XMLPort 37005389 (eOrder Export), 37036604 (Shipment Export).
        """
        params = {**self._base(), "pBeginDate": begin_date, "pEndDate": end_date}
        resp = _call("GetTransactionStatusTimeRange", params)
        return _parse_xml_doc(_text(resp, "pXMLDoc"))

    def get_order_status_detailed(self, order_number: str, doc_type: int = 0) -> ET.Element | None:
        """GetOrderStatusDetailed — full shipment/order detail XML.
        doc_type: 0=Shipment Order, 38=eOrder
        """
        params = {**self._base(), "pDocType": doc_type, "pDocument": order_number}
        resp = _call("GetOrderStatusDetailed", params)
        return _parse_xml_doc(_text(resp, "pXMLDoc"))

    def get_receipt_status_detailed(self, receipt_number: str) -> ET.Element | None:
        """Receipt inbound detail — GetOrderStatusDetailed with pDocType=1.
        Standard interface object: XMLPort 37036606 PW Expand XML Receipt Export.
        """
        return self.get_order_status_detailed(receipt_number, doc_type=1)


# ---------------------------------------------------------------------------
# Default client instance — import this directly
# ---------------------------------------------------------------------------

client = CamelotClient()

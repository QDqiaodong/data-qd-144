"""Strict validation for the AP reconciliation source document.

The source top level is exactly two arrays, ``receipts`` and ``invoices``::

    {
      "receipts": [ {"id","supplier","po_line","sku","accepted_qty",
                     "price_num","price_den"} ... ],
      "invoices": [ {"id","supplier","lines":[
          {"id","po_line","sku","qty","net_cents","tax_bps","tax_cents"} ]} ]
    }

Validation here is purely structural and value-range based; it never opens a
ledger.  Booleans are never accepted as integers (``type(v) is int`` plus an
explicit ``bool`` rejection), unknown fields and duplicate identifiers are
rejected, and every amount is an exact integer.  Unit prices stay rational via
:class:`fractions.Fraction`; no floating point is ever used.
"""
from fractions import Fraction

from cutting_stock.common import ident

MAX_RECEIPTS = 6
MAX_INVOICES = 6
MAX_OPEN_LINES = 6
MAX_OPEN_QTY = 12
MAX_QTY = 12
MAX_AMOUNT = 10**12
MAX_PRICE_NUM = 10**9
MAX_PRICE_DEN = 10**6
MAX_TAX_BPS = 10_000


class APError(ValueError):
    """Contract violation carrying a stable error code for CLI exit 2."""

    code = "invalid_request"


class IdempotentConflict(APError):
    code = "idempotent_conflict"


class StaleProposal(APError):
    code = "stale_proposal"


class LedgerConflict(APError):
    code = "conflict"


class LedgerStateError(APError):
    code = "ledger_state"


def strict_int(value, minimum, maximum, field):
    if type(value) is not int or isinstance(value, bool):
        raise APError(f"{field} must be a strict integer in [{minimum},{maximum}]")
    if not minimum <= value <= maximum:
        raise APError(f"{field} outside allowed bounds [{minimum},{maximum}]")
    return value


def _require_fields(obj, fields, context):
    if not isinstance(obj, dict):
        raise APError(f"{context} must be an object")
    if set(obj) != set(fields):
        raise APError(
            f"{context} expects exactly fields {sorted(fields)}, "
            f"got {sorted(obj)}"
        )


def _text(value, field):
    try:
        return ident(value)
    except ValueError as exc:
        raise APError(str(exc)) from exc


def parse_source(data):
    """Validate and normalize a decoded source document.

    Returns ``(receipts, invoices)`` where each receipt carries an exact
    ``price`` :class:`Fraction` and invoice lines keep their claimed amounts.
    """
    _require_fields(data, "receipts invoices".split(), "source")
    receipts_raw, invoices_raw = data["receipts"], data["invoices"]
    if not isinstance(receipts_raw, list) or not isinstance(invoices_raw, list):
        raise APError("source receipts and invoices must be arrays")
    if len(receipts_raw) > MAX_RECEIPTS:
        raise APError(f"at most {MAX_RECEIPTS} receipts per source")
    if len(invoices_raw) > MAX_INVOICES:
        raise APError(f"at most {MAX_INVOICES} invoices per source")

    receipts = []
    seen_receipts = set()
    for raw in receipts_raw:
        _require_fields(
            raw,
            "id supplier po_line sku accepted_qty price_num price_den".split(),
            "receipt",
        )
        rid = _text(raw["id"], "receipt id")
        if rid in seen_receipts:
            raise APError("duplicate receipt id: " + rid)
        seen_receipts.add(rid)
        receipt = {
            "id": rid,
            "supplier": _text(raw["supplier"], "receipt supplier"),
            "po_line": _text(raw["po_line"], "receipt po_line"),
            "sku": _text(raw["sku"], "receipt sku"),
            "accepted_qty": strict_int(
                raw["accepted_qty"], 0, MAX_QTY, "accepted_qty"
            ),
            "price_num": strict_int(
                raw["price_num"], 1, MAX_PRICE_NUM, "price_num"
            ),
            "price_den": strict_int(
                raw["price_den"], 1, MAX_PRICE_DEN, "price_den"
            ),
        }
        receipt["price"] = Fraction(receipt["price_num"], receipt["price_den"])
        receipts.append(receipt)

    invoices = []
    seen_invoices = set()
    for raw in invoices_raw:
        _require_fields(raw, "id supplier lines".split(), "invoice")
        iid = _text(raw["id"], "invoice id")
        if iid in seen_invoices:
            raise APError("duplicate invoice id: " + iid)
        seen_invoices.add(iid)
        supplier = _text(raw["supplier"], "invoice supplier")
        lines_raw = raw["lines"]
        if not isinstance(lines_raw, list) or not lines_raw:
            raise APError("invoice must contain a non-empty lines array")
        lines = []
        seen_lines = set()
        for line_raw in lines_raw:
            _require_fields(
                line_raw,
                "id po_line sku qty net_cents tax_bps tax_cents".split(),
                "invoice line",
            )
            lid = _text(line_raw["id"], "invoice line id")
            if lid in seen_lines:
                raise APError(f"duplicate line id {lid} in invoice {iid}")
            seen_lines.add(lid)
            line = {
                "id": lid,
                "po_line": _text(line_raw["po_line"], "line po_line"),
                "sku": _text(line_raw["sku"], "line sku"),
                "qty": strict_int(line_raw["qty"], 1, MAX_QTY, "qty"),
                "net_cents": strict_int(
                    line_raw["net_cents"], 0, MAX_AMOUNT, "net_cents"
                ),
                "tax_bps": strict_int(
                    line_raw["tax_bps"], 0, MAX_TAX_BPS, "tax_bps"
                ),
                "tax_cents": strict_int(
                    line_raw["tax_cents"], 0, MAX_AMOUNT, "tax_cents"
                ),
            }
            lines.append(line)
        invoices.append({"id": iid, "supplier": supplier, "lines": lines})
    return receipts, invoices


def canonical_json(obj):
    """Semantic comparison bytes: sorted keys, compact separators, no NaN."""
    import json

    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")

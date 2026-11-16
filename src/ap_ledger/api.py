"""Testable API: reconciliation checks, atomic posting, reversal and reports.

Layered responsibilities:

* :mod:`ap_ledger.validate` parses the strict procurement source;
* :mod:`ap_ledger.matcher` solves the exact integer allocation matrix and
  enforces the single end-of-line banker's rounding of net and tax money;
* this module enforces ledger consistency (frozen invoice semantics, immutable
  receipt keys, quantity floors), the source-hash/sequence proposal binding,
  global request-id idempotency and the single-transaction write boundary;
* :mod:`ap_ledger.store` owns the SQLite schema.

A missing ledger is read by ``check`` as an empty ledger through a read-only
connection and never creates a file.  The ledger is created atomically by the
first successful ``post`` inside one ``BEGIN IMMEDIATE`` transaction.
"""
import hashlib
import json
import sqlite3

from . import matcher, store
from .validate import (
    APError,
    IdempotentConflict,
    LedgerConflict,
    LedgerStateError,
    StaleProposal,
    MAX_OPEN_LINES,
    MAX_OPEN_QTY,
    canonical_json,
    parse_source,
)


def _byte_sorted(values, key=lambda item: item):
    return sorted(values, key=lambda item: key(item).encode("utf-8"))


# --------------------------------------------------------------------------
# Snapshot loading
# --------------------------------------------------------------------------


def _has_schema(conn):
    try:
        return store.has_schema(conn)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(f"ledger is not a readable database: {exc}")


def _load_snapshot(db_path):
    """Read a consistent snapshot; a missing database is the empty ledger."""
    if not store.ledger_exists(db_path):
        return {
            "exists": False,
            "sequence": 0,
            "used_qty": {},
            "live_invoices": {},
            "receipt_records": {},
        }
    try:
        conn = store.connect_reader(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(f"ledger is not a readable database: {exc}")
    try:
        try:
            has_schema = _has_schema(conn)
        except sqlite3.DatabaseError as exc:
            raise LedgerStateError(
                f"ledger is not a readable database: {exc}"
            )
        if not has_schema:
            raise LedgerStateError(
                "ledger file exists but has no AP schema"
            )
        snapshot = {
            "exists": True,
            "sequence": store.get_sequence(conn),
            "used_qty": store.active_usage_by_receipt(conn),
            "live_invoices": {},
            "receipt_records": store.load_receipt_records(conn),
        }
        for posting in store.load_live_postings(conn):
            snapshot["live_invoices"][posting["invoice_id"]] = posting
        return snapshot
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Consistency between the current source and the frozen ledger
# --------------------------------------------------------------------------


def _check_ledger_consistency(receipts, invoices, snapshot):
    used_qty = dict(snapshot["used_qty"])
    receipt_by_id = {r["id"]: r for r in receipts}
    invoice_by_id = {inv["id"]: inv for inv in invoices}

    # Every receipt still occupied by a live posting must be present, keep its
    # immutable key, and never drop accepted_qty below the historical usage.
    for rid, used in used_qty.items():
        record = snapshot["receipt_records"].get(rid)
        if record is None:  # pragma: no cover - defended by write path
            raise LedgerStateError("ledger references unknown receipt: " + rid)
        current = receipt_by_id.get(rid)
        if current is None:
            raise APError(
                "source must include receipt still used by a live posting: "
                + rid
            )
        for field in ("supplier", "po_line", "sku"):
            if current[field] != record[field]:
                raise APError(
                    f"receipt {field} is immutable once used; receipt " + rid
                )
        if current["accepted_qty"] < used:
            raise APError(
                f"accepted_qty of receipt {rid} cannot be lowered below the "
                f"live occupation {used}"
            )

    # Every invoice of a live posting must be present and semantically equal
    # to the frozen content (canonical JSON ignores formatting/key order).
    for iid, posting in snapshot["live_invoices"].items():
        current = invoice_by_id.get(iid)
        if current is None:
            raise APError(
                "source must include invoice used by a live posting: " + iid
            )
        if canonical_json(current) != posting["invoice_json"].encode("utf-8"):
            raise APError(
                "posted invoice content differs from the frozen invoice: "
                + iid
            )
    return used_qty


def _prior_receipt_occupancy(receipts, used_qty):
    rows = []
    for receipt in _byte_sorted(receipts, key=lambda r: r["id"]):
        prior = used_qty.get(receipt["id"], 0)
        rows.append(
            {
                "receipt_id": receipt["id"],
                "supplier": receipt["supplier"],
                "po_line": receipt["po_line"],
                "sku": receipt["sku"],
                "accepted_qty": receipt["accepted_qty"],
                "prior_used_qty": prior,
                "allocated_qty": 0,
                "remaining_qty": receipt["accepted_qty"] - prior,
                "price_num": receipt["price_num"],
                "price_den": receipt["price_den"],
            }
        )
    return rows


def _project_witness(witness):
    """Canonical comparable shape for a proposal/witness."""
    lines = []
    for line in witness["lines"]:
        lines.append(
            {
                "invoice_id": line["invoice_id"],
                "line_id": line["line_id"],
                "po_line": line["po_line"],
                "sku": line["sku"],
                "qty": line["qty"],
                "net_fraction": line["net_fraction"],
                "net_cents": line["net_cents"],
                "tax_bps": line["tax_bps"],
                "tax_cents": line["tax_cents"],
                "allocations": [
                    {
                        "receipt_id": alloc["receipt_id"],
                        "qty": alloc["qty"],
                        "price_num": alloc["price_num"],
                        "price_den": alloc["price_den"],
                    }
                    for alloc in _byte_sorted(
                        line["allocations"], key=lambda a: a["receipt_id"]
                    )
                ],
            }
        )
    lines = sorted(
        lines, key=lambda ln: (
            ln["invoice_id"].encode("utf-8"),
            ln["line_id"].encode("utf-8"),
        )
    )
    occupancy = sorted(
        witness["receipt_occupancy"],
        key=lambda row: row["receipt_id"].encode("utf-8"),
    )
    return {"lines": lines, "receipt_occupancy": occupancy}


def evaluate(source_data, source_sha256, snapshot):
    """Run validation, consistency checks and the exact matcher."""
    receipts, invoices = parse_source(source_data)
    used_qty = _check_ledger_consistency(receipts, invoices, snapshot)

    live_ids = set(snapshot["live_invoices"])
    open_invoices = [
        inv for inv in _byte_sorted(invoices, key=lambda inv: inv["id"])
        if inv["id"] not in live_ids
    ]
    open_line_count = sum(len(inv["lines"]) for inv in open_invoices)
    open_qty = sum(
        line["qty"] for inv in open_invoices for line in inv["lines"]
    )
    if open_line_count > MAX_OPEN_LINES:
        raise APError(
            f"at most {MAX_OPEN_LINES} lines across unposted invoices"
        )
    if open_qty > MAX_OPEN_QTY:
        raise APError(
            f"at most {MAX_OPEN_QTY} units across unposted invoices"
        )

    status, raw_witnesses = matcher.solve(
        open_invoices, receipts, used_qty
    )
    witnesses = [
        {
            "lines": [
                {
                    "invoice_id": line["invoice_id"],
                    "line_id": line["line_id"],
                    "po_line": line["po_line"],
                    "sku": line["sku"],
                    "qty": line["qty"],
                    "net_fraction": line["net_fraction"],
                    "net_cents": line["net_cents"],
                    "tax_bps": line["tax_bps"],
                    "tax_cents": line["tax_cents"],
                    "allocations": line["allocations"],
                }
                for line in witness["lines"]
            ],
            "receipt_occupancy": witness["receipt_occupancy"],
        }
        for witness in raw_witnesses
    ]
    if witnesses:
        occupancy = witnesses[0]["receipt_occupancy"]
    else:
        occupancy = _prior_receipt_occupancy(receipts, used_qty)
    result = {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "sequence": snapshot["sequence"],
        "status": status,
        "open_invoices": [inv["id"] for inv in open_invoices],
        "open_line_count": open_line_count,
        "open_qty": open_qty,
        "receipt_occupancy": occupancy,
        "witnesses": witnesses,
        "legal_edges": matcher.legal_edges(open_invoices, receipts),
    }
    return result, open_invoices, receipts, used_qty, witnesses


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def check(db_path, source_data, source_sha256):
    """Read-only reconciliation; never creates the ledger file."""
    snapshot = _load_snapshot(db_path)
    result, _, _, _, _ = evaluate(source_data, source_sha256, snapshot)
    return result


# --------------------------------------------------------------------------
# post
# --------------------------------------------------------------------------


def _post_fingerprint(source_sha256, proposal):
    body = (
        b"post\n"
        + source_sha256.encode("utf-8")
        + b"\n"
        + canonical_json(proposal)
    )
    return hashlib.sha256(body).hexdigest()


def _validate_proposal_binding(proposal, source_sha256, sequence, computed):
    if not isinstance(proposal, dict):
        raise APError("proposal must be an object")
    for field in ("source_sha256", "sequence", "status", "witnesses"):
        if field not in proposal:
            raise APError("proposal missing field: " + field)
    if proposal["source_sha256"] != source_sha256:
        raise StaleProposal("proposal is bound to a different source document")
    if proposal["sequence"] != sequence:
        raise StaleProposal(
            f"proposal sequence {proposal['sequence']} != ledger sequence "
            f"{sequence}"
        )
    if proposal["status"] != "unique":
        raise APError(
            "only a uniquely matched proposal can be posted"
        )
    witnesses = proposal["witnesses"]
    if not isinstance(witnesses, list) or len(witnesses) != 1:
        raise APError("a unique proposal must carry exactly one witness")
    if canonical_json(_project_witness(witnesses[0])) != canonical_json(
        _project_witness(computed)
    ):
        raise StaleProposal(
            "proposal witness does not match the recomputed allocation"
        )


def post(db_path, source_data, source_sha256, proposal, request_id):
    """Atomically post every unposted invoice under a unique proposal."""
    from cutting_stock.common import ident

    request_id = ident(request_id)
    fingerprint = _post_fingerprint(source_sha256, proposal)
    # Capture this before connecting: opening a writer connection itself
    # creates the file, so the flag must precede it.
    fresh = not store.ledger_exists(db_path)
    try:
        conn = store.connect_writer(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(f"ledger is not a readable database: {exc}")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise LedgerStateError(
                f"ledger is not a readable database: {exc}"
            )
        created_here = not _has_schema(conn)
        if created_here:
            # The first successful post atomically creates the whole schema;
            # a later failure rolls this back, leaving no committed objects.
            store.initialize_schema(conn)
        # Idempotency is resolved before any expiry/stale check, so a retry
        # never re-occupies quantities and a conflicting payload is rejected.
        prior = store.get_request(conn, request_id)
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                conn.rollback()
                raise IdempotentConflict(
                    "request-id reused with different arguments"
                )
            conn.rollback()
            return json.loads(prior["response_json"])

        sequence = store.get_sequence(conn)
        snapshot = {
            "exists": True,
            "sequence": sequence,
            "used_qty": store.active_usage_by_receipt(conn),
            "live_invoices": {
                posting["invoice_id"]: posting
                for posting in store.load_live_postings(conn)
            },
            "receipt_records": store.load_receipt_records(conn),
        }
        result, open_invoices, receipts, used_qty, witnesses = evaluate(
            source_data, source_sha256, snapshot
        )
        # Structural binding (source hash and sequence) is checked before the
        # empty/ambiguous batch rules so a proposal that no longer matches the
        # ledger sequence is reported as stale rather than mislabelled.
        if not isinstance(proposal, dict):
            raise APError("proposal must be an object")
        for field in ("source_sha256", "sequence", "status", "witnesses"):
            if field not in proposal:
                raise APError("proposal missing field: " + field)
        if proposal["source_sha256"] != source_sha256:
            raise StaleProposal("proposal is bound to a different source document")
        if proposal["sequence"] != sequence:
            raise StaleProposal(
                f"proposal sequence {proposal['sequence']} != ledger sequence "
                f"{sequence}"
            )
        if not open_invoices:
            raise APError("empty posting batch is rejected")
        if result["status"] != "unique":
            raise APError(
                "allocation is " + result["status"] + "; posting refused"
            )
        _validate_proposal_binding(
            proposal, source_sha256, sequence, witnesses[0]
        )
        witness = witnesses[0]

        # Upsert receipt records.  Immutability of used receipts and the
        # accepted_qty floor were enforced during consistency checks.
        for receipt in receipts:
            conn.execute(
                "INSERT INTO receipt_records(receipt_id,supplier,po_line,sku,"
                "accepted_qty) VALUES(?,?,?,?,?) "
                "ON CONFLICT(receipt_id) DO UPDATE SET supplier=excluded.supplier,"
                "po_line=excluded.po_line,sku=excluded.sku,"
                "accepted_qty=excluded.accepted_qty",
                (
                    receipt["id"], receipt["supplier"], receipt["po_line"],
                    receipt["sku"], receipt["accepted_qty"],
                ),
            )

        new_sequence = sequence + 1
        next_posting_id = conn.execute(
            "SELECT COALESCE(MAX(posting_id),0)+1 AS n FROM postings"
        ).fetchone()["n"]
        lines_by_invoice = {}
        for line in witness["lines"]:
            lines_by_invoice.setdefault(line["invoice_id"], []).append(line)

        posted = []
        total_debit = 0
        for invoice in open_invoices:
            iid = invoice["id"]
            posting_id = next_posting_id
            next_posting_id += 1
            line_records = sorted(
                lines_by_invoice[iid],
                key=lambda ln: ln["line_id"].encode("utf-8"),
            )
            net_total = sum(line["net_cents"] for line in line_records)
            tax_total = sum(line["tax_cents"] for line in line_records)
            payable = net_total + tax_total
            conn.execute(
                "INSERT INTO postings(posting_id,sequence,request_id,"
                "source_sha256,invoice_id,supplier,invoice_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    posting_id, new_sequence, request_id, source_sha256, iid,
                    invoice["supplier"],
                    canonical_json(invoice).decode("utf-8"),
                ),
            )
            for line in line_records:
                conn.execute(
                    "INSERT INTO posting_lines(posting_id,line_id,po_line,sku,"
                    "qty,net_cents,tax_bps,tax_cents,net_fraction) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        posting_id, line["line_id"], line["po_line"],
                        line["sku"], line["qty"], line["net_cents"],
                        line["tax_bps"], line["tax_cents"],
                        line["net_fraction"],
                    ),
                )
                for alloc in line["allocations"]:
                    conn.execute(
                        "INSERT INTO posting_allocations(posting_id,line_id,"
                        "receipt_id,qty,price_num,price_den) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            posting_id, line["line_id"], alloc["receipt_id"],
                            alloc["qty"], alloc["price_num"],
                            alloc["price_den"],
                        ),
                    )
            for account, direction, amount in (
                ("purchase_expense", "debit", net_total),
                ("input_tax", "debit", tax_total),
                ("accounts_payable", "credit", payable),
            ):
                conn.execute(
                    "INSERT INTO journal_entries(sequence,posting_id,sign,"
                    "account,direction,amount_cents) VALUES(?,?,1,?,?,?)",
                    (new_sequence, posting_id, account, direction, amount),
                )
            total_debit += net_total + tax_total
            posted.append(
                {
                    "posting_id": posting_id,
                    "sequence": new_sequence,
                    "invoice_id": iid,
                    "net_cents": net_total,
                    "tax_cents": tax_total,
                    "payable_cents": payable,
                }
            )

        store.set_sequence(conn, new_sequence)
        response = {
            "status": "posted",
            "request_id": request_id,
            "source_sha256": source_sha256,
            "sequence_before": sequence,
            "sequence": new_sequence,
            "postings": posted,
            "totals": {
                "debit_cents": total_debit,
                "credit_cents": total_debit,
                "balanced": True,
            },
        }
        conn.execute(
            "INSERT INTO requests(request_id,operation,fingerprint,"
            "response_json,sequence_after) VALUES(?,?,?,?,?)",
            (
                request_id, "post", fingerprint,
                json.dumps(response, sort_keys=True, ensure_ascii=False,
                           allow_nan=False),
                new_sequence,
            ),
        )
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        # A failed first post must leave no ledger behind: remove the empty
        # file the connection created before the (rolled-back) DDL.
        if fresh and store.is_empty_database_file(db_path):
            import os as _os

            _os.unlink(db_path)
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# reverse
# --------------------------------------------------------------------------


def reverse(db_path, posting_id, request_id):
    """Append opposite entries for one posting and release its quantities."""
    from cutting_stock.common import ident

    request_id = ident(request_id)
    if type(posting_id) is not int or isinstance(posting_id, bool) or posting_id < 1:
        raise APError("posting-id must be a positive integer")
    fingerprint = hashlib.sha256(
        b"reverse\n" + str(posting_id).encode("utf-8")
    ).hexdigest()
    if not store.ledger_exists(db_path):
        raise LedgerStateError("ledger does not exist")
    try:
        conn = store.connect_writer(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(f"ledger is not a readable database: {exc}")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise LedgerStateError(
                f"ledger is not a readable database: {exc}"
            )
        prior = store.get_request(conn, request_id)
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                conn.rollback()
                raise IdempotentConflict(
                    "request-id reused with different arguments"
                )
            conn.rollback()
            return json.loads(prior["response_json"])
        if not _has_schema(conn):
            raise LedgerStateError("ledger has no AP schema")
        row = conn.execute(
            "SELECT posting_id,sequence,invoice_id,reversed_by FROM postings "
            "WHERE posting_id=?",
            (posting_id,),
        ).fetchone()
        if row is None:
            raise LedgerConflict("unknown posting-id: " + str(posting_id))
        if row["reversed_by"] is not None:
            raise LedgerConflict(
                f"posting {posting_id} was already reversed at sequence "
                f"{row['reversed_by']}"
            )
        sequence = store.get_sequence(conn)
        new_sequence = sequence + 1
        conn.execute(
            "UPDATE postings SET reversed_by=?, reversed_request_id=? "
            "WHERE posting_id=?",
            (new_sequence, request_id, posting_id),
        )
        # Mirror the original three entries with sign -1; evidence rows are
        # never deleted or rewritten.
        original = conn.execute(
            "SELECT account,direction,amount_cents FROM journal_entries "
            "WHERE posting_id=? AND sign=1 ORDER BY id",
            (posting_id,),
        ).fetchall()
        for entry in original:
            conn.execute(
                "INSERT INTO journal_entries(sequence,posting_id,sign,"
                "account,direction,amount_cents) VALUES(?,?, -1,?,?,?)",
                (
                    new_sequence, posting_id, entry["account"],
                    entry["direction"], entry["amount_cents"],
                ),
            )
        released = [
            dict(alloc)
            for alloc in conn.execute(
                "SELECT line_id,receipt_id,qty,price_num,price_den "
                "FROM posting_allocations WHERE posting_id=? "
                "ORDER BY line_id,receipt_id",
                (posting_id,),
            ).fetchall()
        ]
        store.set_sequence(conn, new_sequence)
        response = {
            "status": "reversed",
            "request_id": request_id,
            "posting_id": posting_id,
            "invoice_id": row["invoice_id"],
            "sequence_before": sequence,
            "sequence": new_sequence,
            "released_allocations": released,
        }
        conn.execute(
            "INSERT INTO requests(request_id,operation,fingerprint,"
            "response_json,sequence_after) VALUES(?,?,?,?,?)",
            (
                request_id, "reverse", fingerprint,
                json.dumps(response, sort_keys=True, ensure_ascii=False,
                           allow_nan=False),
                new_sequence,
            ),
        )
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _posting_report_row(posting):
    net_total = sum(line["net_cents"] for line in posting["lines"])
    tax_total = sum(line["tax_cents"] for line in posting["lines"])
    return {
        "posting_id": posting["posting_id"],
        "sequence": posting["sequence"],
        "request_id": posting["request_id"],
        "source_sha256": posting["source_sha256"],
        "invoice_id": posting["invoice_id"],
        "supplier": posting["supplier"],
        "state": "reversed" if posting["reversed_by"] is not None else "active",
        "reversed_by": posting["reversed_by"],
        "reversed_request_id": posting["reversed_request_id"],
        "frozen_invoice": posting["invoice"],
        "lines": [
            {
                "line_id": line["line_id"],
                "po_line": line["po_line"],
                "sku": line["sku"],
                "qty": line["qty"],
                "net_fraction": line["net_fraction"],
                "net_cents": line["net_cents"],
                "tax_bps": line["tax_bps"],
                "tax_cents": line["tax_cents"],
            }
            for line in posting["lines"]
        ],
        "allocations": posting["allocations"],
        "net_cents": net_total,
        "tax_cents": tax_total,
        "payable_cents": net_total + tax_total,
    }


def build_report(db_path):
    """Deterministic report over postings, effective occupation and journal."""
    if not store.ledger_exists(db_path):
        raise LedgerStateError("ledger does not exist")
    try:
        conn = store.connect_reader(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(f"ledger is not a readable database: {exc}")
    try:
        if not _has_schema(conn):
            raise LedgerStateError("ledger has no AP schema")
        sequence = store.get_sequence(conn)
        postings = [
            _posting_report_row(posting)
            for posting in store.load_all_postings(conn)
        ]
        journal = store.load_journal(conn)
        receipt_records = store.load_receipt_records(conn)
    finally:
        conn.close()

    # Effective occupation counts only non-reversed allocations.
    occupation_map = {}
    for posting in postings:
        if posting["state"] != "active":
            continue
        for alloc in posting["allocations"]:
            entry = occupation_map.setdefault(
                alloc["receipt_id"], {"used_qty": 0}
            )
            entry["used_qty"] += alloc["qty"]
    occupation = []
    for rid in _byte_sorted(occupation_map, key=lambda x: x):
        record = receipt_records.get(rid, {})
        accepted = record.get("accepted_qty")
        used = occupation_map[rid]["used_qty"]
        occupation.append(
            {
                "receipt_id": rid,
                "supplier": record.get("supplier"),
                "po_line": record.get("po_line"),
                "sku": record.get("sku"),
                "accepted_qty": accepted,
                "effective_used_qty": used,
                "remaining_qty": None if accepted is None else accepted - used,
            }
        )

    def totals(entries):
        debit = credit = 0
        per_account = {account: {"debit": 0, "credit": 0}
                       for account in store.JOURNAL_ACCOUNTS}
        for entry in entries:
            amount = entry["amount_cents"] * entry["sign"]
            if entry["direction"] == "debit":
                per_account[entry["account"]]["debit"] += amount
            else:
                per_account[entry["account"]]["credit"] += amount
        for account in store.JOURNAL_ACCOUNTS:
            debit += per_account[account]["debit"]
            credit += per_account[account]["credit"]
        return {
            "debit_cents": debit,
            "credit_cents": credit,
            "balanced": debit == credit,
            "by_account": per_account,
        }

    active_entries = [e for e in journal if e["sign"] == 1]
    report = {
        "schema_version": store.SCHEMA_VERSION,
        "sequence": sequence,
        "postings": postings,
        "effective_receipt_occupation": occupation,
        "journal_totals_gross": totals(journal),
        "journal_totals_active": totals(active_entries),
        "journal_entries": [
            {
                "sequence": entry["sequence"],
                "posting_id": entry["posting_id"],
                "sign": entry["sign"],
                "account": entry["account"],
                "direction": entry["direction"],
                "amount_cents": entry["amount_cents"],
            }
            for entry in journal
        ],
    }
    return report

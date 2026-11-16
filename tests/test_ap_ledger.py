"""Acceptance tests for the AP ledger: posting, reversal, consistency and CLI."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ap_ledger import api, store
from ap_ledger.validate import (
    APError,
    IdempotentConflict,
    LedgerConflict,
    LedgerStateError,
    StaleProposal,
    parse_source,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def write_json(path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def receipt(rid, supplier="s1", po_line="po1", sku="sku1", qty=10,
            num=3, den=2):
    return {
        "id": rid, "supplier": supplier, "po_line": po_line, "sku": sku,
        "accepted_qty": qty, "price_num": num, "price_den": den,
    }


def line(lid, po_line="po1", sku="sku1", qty=1, net=2, bps=0, tax=0):
    return {
        "id": lid, "po_line": po_line, "sku": sku, "qty": qty,
        "net_cents": net, "tax_bps": bps, "tax_cents": tax,
    }


def invoice(iid, lines, supplier="s1"):
    return {"id": iid, "supplier": supplier, "lines": lines}


def base_source():
    return {
        "receipts": [receipt("r1")],
        "invoices": [invoice("inv1", [line("l1", qty=3, net=4)])],
    }


def proposal_from_check(check_result):
    witness = check_result["witnesses"][0]
    return {
        "source_sha256": check_result["source_sha256"],
        "sequence": check_result["sequence"],
        "status": check_result["status"],
        "witnesses": [
            {
                "lines": witness["lines"],
                "receipt_occupancy": witness["receipt_occupancy"],
            }
        ],
    }


class ValidationTest(unittest.TestCase):
    def test_strict_top_level_and_fields(self):
        with self.assertRaises(APError):
            parse_source({"receipts": []})
        with self.assertRaises(APError):
            parse_source({"receipts": [], "invoices": [], "extra": 1})
        with self.assertRaises(APError):
            parse_source({"receipts": {}, "invoices": []})
        bad_receipt = receipt("r1")
        del bad_receipt["sku"]
        with self.assertRaises(APError):
            parse_source({"receipts": [bad_receipt], "invoices": []})
        bad_line = line("l1")
        del bad_line["tax_cents"]
        with self.assertRaises(APError):
            parse_source(
                {"receipts": [], "invoices": [invoice("i", [bad_line])]}
            )

    def test_bounds_and_booleans(self):
        ok = receipt("r", qty=0, num=10**9, den=10**6)
        parse_source({"receipts": [ok], "invoicess": []} if False else
                     {"receipts": [ok], "invoices": []})
        for field, value in (
            ("accepted_qty", 13), ("accepted_qty", -1),
            ("price_num", 0), ("price_num", 10**9 + 1),
            ("price_den", 0), ("price_den", 10**6 + 1),
        ):
            bad = receipt("r")
            bad[field] = value
            with self.assertRaises(APError):
                parse_source({"receipts": [bad], "invoices": []})
        # Booleans are never integers.
        for field in ("accepted_qty", "price_num", "price_den"):
            bad = receipt("r")
            bad[field] = True
            with self.assertRaises(APError):
                parse_source({"receipts": [bad], "invoices": []})
        bad_line = line("l", qty=0)  # invoice qty must be positive
        with self.assertRaises(APError):
            parse_source(
                {"receipts": [], "invoices": [invoice("i", [bad_line])]}
            )
        for field, value in (
            ("net_cents", 10**12 + 1), ("tax_cents", -1),
            ("tax_bps", 10001), ("qty", True),
        ):
            bad = line("l")
            bad[field] = value
            with self.assertRaises(APError):
                parse_source(
                    {"receipts": [], "invoices": [invoice("i", [bad])]}
                )
        good = line("l", net=10**12, tax=10**12, bps=10000)
        parse_source({"receipts": [], "invoices": [invoice("i", [good])]})

    def test_duplicate_identifiers_and_caps(self):
        with self.assertRaises(APError):
            parse_source(
                {"receipts": [receipt(f"r{i}") for i in range(7)],
                 "invoices": []}
            )
        with self.assertRaises(APError):
            parse_source(
                {"receipts": [],
                 "invoices": [invoice(f"i{k}", [line("x")]) for k in range(7)]}
            )
        with self.assertRaises(APError):
            parse_source(
                {"receipts": [receipt("r"), receipt("r")], "invoices": []}
            )
        with self.assertRaises(APError):
            parse_source(
                {"receipts": [],
                 "invoices": [invoice("i", [line("x"), line("x")])]}
            )

    def test_price_is_exact_fraction(self):
        receipts, _ = parse_source(
            {"receipts": [receipt("r", num=1, den=3)], "invoices": []}
        )
        from fractions import Fraction
        self.assertEqual(receipts[0]["price"], Fraction(1, 3))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ap.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_ledger_reads_empty_and_creates_nothing(self):
        result = api.check(self.db, base_source(), "h" * 64)
        self.assertEqual(result["sequence"], 0)
        self.assertEqual(result["status"], "unique")
        self.assertFalse(Path(self.db).exists())
        # Even a failed check must not create the file.
        bad = base_source()
        bad["invoices"][0]["lines"][0]["net_cents"] = 999
        api.check(self.db, bad, "h" * 64)
        self.assertFalse(Path(self.db).exists())

    def test_returns_exact_fraction_and_remaining_quantity(self):
        result = api.check(self.db, base_source(), "h" * 64)
        record = result["witnesses"][0]["lines"][0]
        self.assertEqual(record["net_fraction"], "9/2")
        self.assertEqual(record["net_cents"], 4)
        occupancy = result["receipt_occupancy"][0]
        self.assertEqual(occupancy["remaining_qty"], 7)
        self.assertEqual(result["legal_edges"], [("inv1", "l1", "r1")])

    def test_array_order_does_not_change_result(self):
        source = {
            "receipts": [
                receipt("r2", num=1, den=1),
                receipt("r1", num=1, den=1),
            ],
            "invoices": [
                invoice("i2", [line("l2", qty=1, net=1)]),
                invoice("i1", [line("l1", qty=1, net=1)]),
            ],
        }
        first = api.check(self.db, source, "h" * 64)
        reversed_source = {
            "receipts": list(reversed(source["receipts"])),
            "invoices": list(reversed(source["invoices"])),
        }
        second = api.check(self.db, reversed_source, "h" * 64)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )

    def test_open_batch_caps_enforced(self):
        # Six invoices with seven open lines in total.
        invoices = [invoice(f"i{k}", [line(f"l{k}", qty=1, net=2)])
                    for k in range(6)]
        invoices[0]["lines"].append(line("l0b", qty=1, net=2))
        source = {"receipts": [receipt("r1", qty=12)], "invoices": invoices}
        with self.assertRaises(APError):
            api.check(self.db, source, "h" * 64)
        # Total open qty above 12 is rejected at the check layer.
        big = [invoice(f"i{k}", [line(f"l{k}", qty=2, net=3)])
               for k in range(6)]
        big[0]["lines"][0]["qty"] = 3  # total becomes 13
        source2 = {"receipts": [receipt("r1", qty=13, num=3, den=2)],
                   "invoices": big}
        with self.assertRaises(APError):
            api.check(self.db, source2, "h" * 64)


class PostingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "ap.db")

    def tearDown(self):
        self.tmp.cleanup()

    def post_unique(self, source=None, digest="h" * 64, request_id="R1",
                    db=None):
        source = source or base_source()
        result = api.check(db or self.db, source, digest)
        self.assertEqual(result["status"], "unique")
        proposal = proposal_from_check(result)
        return api.post(db or self.db, source, digest, proposal, request_id)

    def test_first_post_creates_ledger_and_balanced_entries(self):
        response = self.post_unique()
        self.assertTrue(Path(self.db).exists())
        self.assertEqual(response["sequence"], 1)
        self.assertEqual(response["sequence_before"], 0)
        self.assertTrue(response["totals"]["balanced"])
        report = api.build_report(self.db)
        entries = report["journal_entries"]
        # Per-invoice: debit purchase net, debit input tax, credit payable.
        self.assertEqual(len(entries), 3)
        debit = sum(e["amount_cents"] for e in entries
                    if e["direction"] == "debit")
        credit = sum(e["amount_cents"] for e in entries
                     if e["direction"] == "credit")
        self.assertEqual(debit, credit)
        posting = report["postings"][0]
        self.assertEqual(posting["net_cents"], 4)
        self.assertEqual(posting["payable_cents"], 4)
        # Frozen price evidence and exact fraction.
        self.assertEqual(posting["allocations"][0]["price_num"], 3)
        self.assertEqual(posting["lines"][0]["net_fraction"], "9/2")

    def test_multi_invoice_posting_balances_in_total(self):
        source = {
            "receipts": [receipt("r1", qty=12, num=2, den=1)],
            "invoices": [
                invoice("inv1", [line("l1", qty=3, net=6, bps=1000, tax=1)]),
                invoice("inv2", [line("l2", qty=2, net=4, bps=0, tax=0)]),
            ],
        }
        response = self.post_unique(source)
        self.assertEqual(len(response["postings"]), 2)
        report = api.build_report(self.db)
        # 6+1 + 4 = 11 debit; payable 7 + 4 = 11 credit.
        self.assertEqual(report["journal_totals_active"]["debit_cents"], 11)
        self.assertEqual(report["journal_totals_active"]["credit_cents"], 11)
        self.assertTrue(report["journal_totals_active"]["balanced"])
        by_account = report["journal_totals_active"]["by_account"]
        self.assertEqual(by_account["purchase_expense"]["debit"], 10)
        self.assertEqual(by_account["input_tax"]["debit"], 1)
        self.assertEqual(by_account["accounts_payable"]["credit"], 11)

    def test_empty_and_ambiguous_batches_are_rejected(self):
        empty = {"receipts": [receipt("r1")], "invoices": []}
        check = api.check(self.db, empty, "h" * 64)
        proposal = proposal_from_check(check)
        with self.assertRaises(APError):
            api.post(self.db, empty, "h" * 64, proposal, "R-empty")
        self.assertFalse(Path(self.db).exists())
        ambiguous = {
            "receipts": [
                receipt("r1", num=1, den=1, qty=2),
                receipt("r2", num=1, den=1, qty=2),
            ],
            "invoices": [invoice("i1", [line("l1", qty=2, net=2)])],
        }
        check = api.check(self.db, ambiguous, "h" * 64)
        self.assertEqual(check["status"], "ambiguous")
        proposal = {
            "source_sha256": "h" * 64, "sequence": 0,
            "status": "ambiguous", "witnesses": check["witnesses"],
        }
        with self.assertRaises(APError):
            api.post(self.db, ambiguous, "h" * 64, proposal, "R-amb")

    def test_proposal_binding_is_rechecked_inside_transaction(self):
        source = base_source()
        check = api.check(self.db, source, "h" * 64)
        proposal = proposal_from_check(check)
        with self.assertRaises(StaleProposal):
            api.post(self.db, source, "x" * 64, proposal, "R1")
        stale_seq = dict(proposal)
        stale_seq["sequence"] = 9
        with self.assertRaises(StaleProposal):
            api.post(self.db, source, "h" * 64, stale_seq, "R1")
        wrong = json.loads(json.dumps(proposal))
        wrong["witnesses"][0]["lines"][0]["net_cents"] = 5
        with self.assertRaises(StaleProposal):
            api.post(self.db, source, "h" * 64, wrong, "R1")
        # Tampering with an immutable line key is also rejected by binding,
        # not silently corrected.
        tampered = json.loads(json.dumps(proposal))
        tampered["witnesses"][0]["lines"][0]["po_line"] = "TAMPERED"
        with self.assertRaises(StaleProposal):
            api.post(self.db, source, "h" * 64, tampered, "R1")
        self.assertFalse(Path(self.db).exists())
        non_unique = dict(proposal)
        non_unique["status"] = "ambiguous"
        with self.assertRaises(APError):
            api.post(self.db, source, "h" * 64, non_unique, "R1")
        self.assertFalse(Path(self.db).exists())

    def test_idempotent_retry_conflict_and_retry_before_staleness(self):
        source = base_source()
        check = api.check(self.db, source, "h" * 64)
        proposal = proposal_from_check(check)
        first = api.post(self.db, source, "h" * 64, proposal, "R1")
        second = api.post(self.db, source, "h" * 64,
                          json.loads(json.dumps(proposal)), "R1")
        self.assertEqual(first, second)
        report = api.build_report(self.db)
        self.assertEqual(len(report["postings"]), 1)
        self.assertEqual(report["sequence"], 1)
        # Same request id with different arguments is a conflict, even though
        # the stored proposal is now sequence-stale relative to the ledger.
        conflict = dict(proposal)
        conflict["source_sha256"] = "z" * 64
        with self.assertRaises(IdempotentConflict):
            api.post(self.db, source, "z" * 64, conflict, "R1")
        # Retry with the original (now stale-looking) proposal still returns
        # the original result: idempotency wins over the expiry check.
        retry = api.post(self.db, source, "h" * 64, proposal, "R1")
        self.assertEqual(retry, first)
        # A request id cannot be reused across operations.
        with self.assertRaises(IdempotentConflict):
            api.reverse(self.db, 1, "R1")

    def test_failure_does_not_increment_sequence_or_consume_request_id(self):
        response = self.post_unique(request_id="R1")
        self.assertEqual(response["sequence"], 1)
        # Keep the already-posted inv1 in the source and add a bad new invoice.
        bad = base_source()
        bad["invoices"].append(
            invoice("inv2", [line("l2", qty=1, net=999)])
        )
        check = api.check(self.db, bad, "h2" * 32)
        self.assertEqual(check["status"], "infeasible")
        proposal = {
            "source_sha256": "h2" * 32, "sequence": 1,
            "status": "infeasible", "witnesses": [],
        }
        with self.assertRaises(APError):
            api.post(self.db, bad, "h2" * 32, proposal, "R2")
        report = api.build_report(self.db)
        self.assertEqual(report["sequence"], 1)
        # R2 was never recorded, so a corrected post may reuse it.
        fixed_source = base_source()
        fixed_source["invoices"].append(
            invoice("inv2", [line("l2", qty=1, net=2)])
        )
        fixed = self.post_unique(
            fixed_source, digest="h2" * 32, request_id="R2"
        )
        self.assertEqual(fixed["sequence"], 2)


class SourceConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ap.db")
        self.source = base_source()
        check = api.check(self.db, self.source, "h" * 64)
        self.proposal = proposal_from_check(check)
        api.post(self.db, self.source, "h" * 64, self.proposal, "R1")

    def tearDown(self):
        self.tmp.cleanup()

    def check_raises(self, source):
        with self.assertRaises(APError):
            api.check(self.db, source, "h2" * 32)

    def test_source_must_keep_occupied_receipt_and_posted_invoice(self):
        missing_receipt = json.loads(json.dumps(self.source))
        missing_receipt["receipts"] = []
        self.check_raises(missing_receipt)
        missing_invoice = json.loads(json.dumps(self.source))
        missing_invoice["invoices"] = []
        self.check_raises(missing_invoice)

    def test_receipt_key_immutable_and_qty_floor_enforced(self):
        for field in ("supplier", "po_line", "sku"):
            changed = json.loads(json.dumps(self.source))
            changed["receipts"][0][field] = "other"
            self.check_raises(changed)
        lowered = json.loads(json.dumps(self.source))
        lowered["receipts"][0]["accepted_qty"] = 2  # 3 units occupied
        self.check_raises(lowered)
        # Keeping the floor is allowed.
        kept = json.loads(json.dumps(self.source))
        kept["receipts"][0]["accepted_qty"] = 3
        result = api.check(self.db, kept, "h2" * 32)
        self.assertEqual(result["status"], "unique")

    def test_posted_invoice_content_is_frozen(self):
        changed = json.loads(json.dumps(self.source))
        changed["invoices"][0]["lines"][0]["tax_bps"] = 100
        self.check_raises(changed)

    def test_price_correction_only_affects_unoccupied_units(self):
        corrected = json.loads(json.dumps(self.source))
        corrected["receipts"][0]["price_num"] = 5  # 5/2 = 2.5 per unit
        # A second invoice consumes one of the 7 remaining units at new price.
        corrected["invoices"].append(
            invoice("inv2", [line("l2", qty=1, net=3)])  # 2.5 -> round 2?
        )
        # 2.5 half-even rounds to 2; correct the claim and verify.
        corrected["invoices"][1]["lines"][0]["net_cents"] = 2
        result = api.check(self.db, corrected, "h2" * 32)
        self.assertEqual(result["status"], "unique")
        lines = {ln["line_id"]: ln
                 for ln in result["witnesses"][0]["lines"]}
        self.assertEqual(lines["l2"]["net_fraction"], "5/2")
        self.assertEqual(lines["l2"]["net_cents"], 2)
        # The historical posting still shows the original frozen price 3/2.
        report = api.build_report(self.db)
        self.assertEqual(
            report["postings"][0]["allocations"][0]["price_num"], 3
        )

    def test_unposted_invoice_may_be_corrected(self):
        # The ledger already holds a live posting of inv1 (from setUp), so the
        # current source must keep inv1 while adding the unposted inv2.
        source = {
            "receipts": [receipt("r1", qty=12)],
            "invoices": [
                invoice("inv1", [line("l1", qty=3, net=4)]),
                invoice("inv2", [line("l2", qty=1, net=99)]),
            ],
        }
        self.assertEqual(
            api.check(self.db, source, "h2" * 32)["status"], "infeasible"
        )
        source["invoices"][1]["lines"][0]["net_cents"] = 2
        self.assertEqual(
            api.check(self.db, source, "h2" * 32)["status"], "unique"
        )


class ReversalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "ap.db")
        self.source = base_source()
        check = api.check(self.db, self.source, "h" * 64)
        self.proposal = proposal_from_check(check)
        self.posted = api.post(
            self.db, self.source, "h" * 64, self.proposal, "R1"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_reverse_appends_opposite_entries_and_releases_quantity(self):
        response = api.reverse(self.db, 1, "R2")
        self.assertEqual(response["sequence"], 2)
        self.assertEqual(response["posting_id"], 1)
        report = api.build_report(self.db)
        self.assertEqual(len(report["journal_entries"]), 6)
        # Signed gross totals net to zero; active view keeps the original.
        self.assertEqual(report["journal_totals_gross"]["debit_cents"], 0)
        self.assertEqual(report["journal_totals_active"]["debit_cents"], 4)
        self.assertEqual(report["effective_receipt_occupation"], [])
        posting = report["postings"][0]
        self.assertEqual(posting["state"], "reversed")
        self.assertEqual(posting["reversed_by"], 2)
        # Old evidence is untouched.
        self.assertEqual(posting["allocations"][0]["qty"], 3)
        self.assertEqual(posting["lines"][0]["net_fraction"], "9/2")

    def test_double_reverse_rejected_and_retry_idempotent(self):
        first = api.reverse(self.db, 1, "R2")
        with self.assertRaises(LedgerConflict):
            api.reverse(self.db, 1, "R3")
        # Exact retry returns the stored response.
        self.assertEqual(api.reverse(self.db, 1, "R2"), first)
        # The same request id with different arguments conflicts rather than
        # performing a second reversal (idempotency is checked first).
        with self.assertRaises(IdempotentConflict):
            api.reverse(self.db, 2, "R2")

    def test_conflicting_reverse_arguments_do_not_touch_ledger(self):
        api.reverse(self.db, 1, "R2")
        before = api.build_report(self.db)
        with self.assertRaises(IdempotentConflict):
            api.reverse(self.db, 2, "R2")
        self.assertEqual(api.build_report(self.db), before)

    def test_repost_after_reversal_uses_new_price_new_posting(self):
        api.reverse(self.db, 1, "R2")
        corrected = json.loads(json.dumps(self.source))
        corrected["receipts"][0]["price_num"] = 5  # 2.5/unit
        corrected["invoices"][0]["lines"][0]["net_cents"] = 8  # 3*2.5=7.5->8
        check = api.check(self.db, corrected, "h2" * 32)
        self.assertEqual(check["status"], "unique")
        proposal = proposal_from_check(check)
        response = api.post(
            self.db, corrected, "h2" * 32, proposal, "R3"
        )
        self.assertEqual(response["sequence"], 3)
        self.assertEqual(response["postings"][0]["posting_id"], 2)
        report = api.build_report(self.db)
        self.assertEqual(len(report["postings"]), 2)
        old, new = report["postings"]
        self.assertEqual(old["state"], "reversed")
        self.assertEqual(new["state"], "active")
        self.assertEqual(old["allocations"][0]["price_num"], 3)
        self.assertEqual(new["allocations"][0]["price_num"], 5)
        self.assertEqual(new["lines"][0]["net_fraction"], "15/2")
        self.assertEqual(new["lines"][0]["net_cents"], 8)
        occupation = report["effective_receipt_occupation"][0]
        self.assertEqual(occupation["effective_used_qty"], 3)

    def test_reverse_unknown_posting_and_missing_ledger(self):
        with self.assertRaises(LedgerConflict):
            api.reverse(self.db, 999, "R9")
        missing = str(self.dir / "nope.db")
        with self.assertRaises(LedgerStateError):
            api.reverse(missing, 1, "R9")


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ap.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_ledger_reports_cleanly(self):
        Path(self.db).write_text("not a database", encoding="utf-8")
        source = base_source()
        with self.assertRaises(LedgerStateError):
            api.check(self.db, source, "h" * 64)
        with self.assertRaises(LedgerStateError):
            api.build_report(self.db)
        with self.assertRaises(LedgerStateError):
            api.reverse(self.db, 1, "R9")

    def test_byte_deterministic_and_sorted(self):
        source = {
            "receipts": [
                receipt("中", num=1, den=1, qty=5),
            ],
            "invoices": [
                invoice("单", [line("l1", qty=2, net=2)]),
            ],
        }
        check = api.check(self.db, source, "h" * 64)
        self.assertEqual(check["status"], "unique")
        api.post(self.db, source, "h" * 64,
                 proposal_from_check(check), "R1")
        first = json.dumps(api.build_report(self.db), sort_keys=True,
                           ensure_ascii=False, allow_nan=False, indent=2)
        second = json.dumps(api.build_report(self.db), sort_keys=True,
                            ensure_ascii=False, allow_nan=False, indent=2)
        self.assertEqual(first, second)
        occupation = api.build_report(self.db)[
            "effective_receipt_occupation"
        ]
        ids = [row["receipt_id"] for row in occupation]
        self.assertEqual(ids, sorted(ids, key=lambda s: s.encode("utf-8")))
        self.assertEqual(ids, ["中"])

    def test_report_missing_ledger_rejected(self):
        with self.assertRaises(LedgerStateError):
            api.build_report(self.db)
        self.assertFalse(Path(self.db).exists())


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ap.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_failure_leaves_no_half_state_on_fresh_ledger(self):
        source = base_source()
        check = api.check(self.db, source, "h" * 64)
        proposal = proposal_from_check(check)
        original = store.set_sequence
        calls = {"n": 0}

        def boom(conn, value):
            calls["n"] += 1
            raise RuntimeError("injected failure")

        store.set_sequence = boom
        try:
            with self.assertRaises(RuntimeError):
                api.post(self.db, source, "h" * 64, proposal, "R1")
        finally:
            store.set_sequence = original
        # Reopen: no half-written rows; request id is reusable.
        if Path(self.db).exists():
            conn = sqlite3.connect(self.db)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn("postings", tables)
            finally:
                conn.close()
        response = api.post(self.db, source, "h" * 64, proposal, "R1")
        self.assertEqual(response["sequence"], 1)

    def test_failure_after_first_posting_preserves_prior_state(self):
        first_source = base_source()
        check = api.check(self.db, first_source, "h" * 64)
        api.post(self.db, first_source, "h" * 64,
                 proposal_from_check(check), "R1")
        # Keep the posted invoice/receipt; add a new invoice to post next.
        second = {
            "receipts": [receipt("r1", qty=12)],
            "invoices": [
                invoice("inv1", [line("l1", qty=3, net=4)]),
                invoice("inv2", [line("l2", qty=1, net=2)]),
            ],
        }
        check2 = api.check(self.db, second, "h2" * 32)
        proposal2 = proposal_from_check(check2)
        original = store.set_sequence

        def boom2(conn, value):
            raise RuntimeError("injected after prior post")

        store.set_sequence = boom2
        try:
            with self.assertRaises(RuntimeError):
                api.post(self.db, second, "h2" * 32, proposal2, "R2")
        finally:
            store.set_sequence = original
        report = api.build_report(self.db)
        self.assertEqual(report["sequence"], 1)
        self.assertEqual(len(report["postings"]), 1)
        self.assertEqual(
            report["effective_receipt_occupation"][0]["effective_used_qty"], 3
        )
        # R2 was not consumed and the new invoice was not written.
        ok = api.post(self.db, second, "h2" * 32, proposal2, "R2")
        self.assertEqual(ok["sequence"], 2)
        self.assertEqual(
            [p["invoice_id"] for p in ok["postings"]], ["inv2"]
        )

    def test_concurrent_posts_same_sequence_only_one_wins(self):
        source = base_source()
        check = api.check(self.db, source, "h" * 64)
        proposal = proposal_from_check(check)
        errors = []

        def worker(request_id):
            try:
                api.post(self.db, source, "h" * 64,
                         json.loads(json.dumps(proposal)), request_id)
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                errors.append((request_id, type(exc).__name__))

        threads = [threading.Thread(target=worker, args=(f"R{i}",))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        report = api.build_report(self.db)
        self.assertEqual(report["sequence"], 1)
        self.assertEqual(len(report["postings"]), 1)
        self.assertEqual(len(errors), 3)
        # Every loser is rejected rather than serialized into a second
        # posting.  With busy_timeout the losers wait for the lock and then
        # observe that every invoice was already posted (empty batch), or
        # re-validate against the advanced sequence; a lock timeout is also
        # an accepted refusal.  In every case exactly one sequence advances.
        self.assertTrue(
            all(kind in ("APError", "StaleProposal", "OperationalError")
                for _, kind in errors),
            errors,
        )


class LinkageIntegrationTest(unittest.TestCase):
    """End-to-end acceptance: correction, reversal and frozen evidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ap.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_correction_reversal_and_repost_linkage(self):
        # r1 holds 6 units at 3/2 cents.  First invoice takes 3 units.
        source = {
            "receipts": [receipt("r1", qty=6, num=3, den=2)],
            "invoices": [
                invoice("inv1", [line("l1", qty=3, net=4)]),  # 4.5 -> 4
            ],
        }
        check = api.check(self.db, source, "h" * 64)
        self.assertEqual(check["status"], "unique")
        first = api.post(
            self.db, source, "h" * 64,
            proposal_from_check(check), "R1"
        )
        first_posting_id = first["postings"][0]["posting_id"]

        # Correct the unit price to 5/2 = 2.5 cents for the unoccupied 3
        # units, while keeping the posted invoice content frozen.
        corrected = json.loads(json.dumps(source))
        corrected["receipts"][0]["price_num"] = 5
        corrected["invoices"].append(
            invoice("inv2", [line("l2", qty=2, net=5)])  # 2 * 2.5 = 5
        )
        check2 = api.check(self.db, corrected, "h2" * 32)
        self.assertEqual(check2["status"], "unique")
        lines = {ln["line_id"]: ln
                 for ln in check2["witnesses"][0]["lines"]}
        self.assertEqual(lines["l2"]["net_fraction"], "5/1")
        occupancy = {
            row["receipt_id"]: row
            for row in check2["receipt_occupancy"]
        }
        # 3 prior units remain occupied; only 3 remain, two newly allocated.
        self.assertEqual(occupancy["r1"]["prior_used_qty"], 3)
        self.assertEqual(occupancy["r1"]["allocated_qty"], 2)
        self.assertEqual(occupancy["r1"]["remaining_qty"], 1)
        second = api.post(
            self.db, corrected, "h2" * 32,
            proposal_from_check(check2), "R2"
        )
        self.assertEqual(second["sequence"], 2)

        # The first posting still shows the original price and fraction.
        report = api.build_report(self.db)
        old = next(p for p in report["postings"]
                   if p["posting_id"] == first_posting_id)
        self.assertEqual(old["allocations"][0]["price_num"], 3)
        self.assertEqual(old["lines"][0]["net_fraction"], "9/2")
        new = next(p for p in report["postings"]
                   if p["invoice_id"] == "inv2")
        self.assertEqual(new["allocations"][0]["price_num"], 5)
        self.assertEqual(new["lines"][0]["net_fraction"], "5/1")

        # Reverse the first posting: its 3 units are released, evidence stays.
        reversal = api.reverse(self.db, first_posting_id, "R3")
        self.assertEqual(reversal["sequence"], 3)
        report = api.build_report(self.db)
        old = next(p for p in report["postings"]
                   if p["posting_id"] == first_posting_id)
        self.assertEqual(old["state"], "reversed")
        self.assertEqual(old["allocations"][0]["qty"], 3)
        # Only inv2 stays effective: 2 of 6 units occupied.
        occupation = report["effective_receipt_occupation"][0]
        self.assertEqual(occupation["effective_used_qty"], 2)
        self.assertEqual(occupation["remaining_qty"], 4)

        # The freed 3 units can re-pair at the corrected price with a new
        # invoice; a new posting is created without rewriting history.
        repost_source = json.loads(json.dumps(corrected))
        # inv1 is no longer live after reversal; replace it by inv3 using the
        # same (frozen) line identity requirements do not apply to new docs,
        # so issue a fresh invoice against the freed quantity.
        repost_source["invoices"] = [
            inv for inv in repost_source["invoices"]
            if inv["id"] != "inv1"
        ]
        repost_source["invoices"].append(
            invoice("inv3", [line("l3", qty=3, net=8)])  # 3*2.5=7.5 -> 8
        )
        check3 = api.check(self.db, repost_source, "h3" * 32)
        self.assertEqual(check3["status"], "unique")
        third = api.post(
            self.db, repost_source, "h3" * 32,
            proposal_from_check(check3), "R4"
        )
        self.assertEqual(third["sequence"], 4)
        report = api.build_report(self.db)
        newest = next(p for p in report["postings"]
                      if p["invoice_id"] == "inv3")
        self.assertEqual(newest["allocations"][0]["price_num"], 5)
        self.assertEqual(newest["lines"][0]["net_fraction"], "15/2")
        self.assertEqual(newest["lines"][0]["net_cents"], 8)
        # History length is unchanged in content: 3 postings, first reversed.
        self.assertEqual(len(report["postings"]), 3)
        self.assertEqual(
            [p["state"] for p in report["postings"]],
            ["reversed", "active", "active"],
        )
        # Effective occupation is inv2 (2) + inv3 (3) = 5 of 6.
        occupation = report["effective_receipt_occupation"][0]
        self.assertEqual(occupation["effective_used_qty"], 5)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "ap.db"
        self.source = self.dir / "source.json"
        self.output = self.dir / "check.json"
        write_json(self.source, base_source())

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return subprocess.run(
            [sys.executable, "-m", "ap_ledger.cli", *args],
            capture_output=True, text=True, env=env,
        )

    def test_full_cli_flow(self):
        r = self.cli("ap-check", "--source", str(self.source),
                     "--ledger", str(self.db), "--output", str(self.output))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.db.exists())
        proposal = self.dir / "proposal.json"
        check = json.loads(self.output.read_text())
        write_json(proposal, proposal_from_check(check))
        r = self.cli("ap-post", "--source", str(self.source),
                     "--ledger", str(self.db),
                     "--proposal", str(proposal), "--request-id", "R1")
        self.assertEqual(r.returncode, 0, r.stderr)
        first = r.stdout
        r = self.cli("ap-post", "--source", str(self.source),
                     "--ledger", str(self.db),
                     "--proposal", str(proposal), "--request-id", "R1")
        self.assertEqual(r.stdout, first)
        report_path = self.dir / "report.json"
        r = self.cli("ap-report", "--ledger", str(self.db),
                     "--output", str(report_path))
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = report_path.read_bytes()
        self.cli("ap-report", "--ledger", str(self.db),
                 "--output", str(report_path))
        self.assertEqual(report_path.read_bytes(), saved)
        r = self.cli("ap-reverse", "--ledger", str(self.db),
                     "--posting-id", "1", "--request-id", "R2")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_error_paths_exit_two(self):
        # Same source/output path.
        r = self.cli("ap-check", "--source", str(self.source),
                     "--ledger", str(self.db), "--output", str(self.source))
        self.assertEqual(r.returncode, 2)
        # Symlink output.
        link = self.dir / "link.json"
        link.symlink_to(self.source)
        r = self.cli("ap-check", "--source", str(self.source),
                     "--ledger", str(self.db), "--output", str(link))
        self.assertEqual(r.returncode, 2)
        # Strict validation failure (boolean integer).
        bad = self.dir / "bad.json"
        bad.write_text(
            '{"receipts":[{"id":"r","supplier":"s","po_line":"p","sku":"k",'
            '"accepted_qty":true,"price_num":1,"price_den":1}],'
            '"invoices":[]}',
            encoding="utf-8",
        )
        r = self.cli("ap-check", "--source", str(bad),
                     "--ledger", str(self.db),
                     "--output", str(self.dir / "o.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid_request", r.stderr)
        # Duplicate JSON keys.
        dup = self.dir / "dup.json"
        dup.write_text(
            '{"receipts":[],"invoices":[],"invoices":[]}', encoding="utf-8"
        )
        r = self.cli("ap-check", "--source", str(dup),
                     "--ledger", str(self.db),
                     "--output", str(self.dir / "o2.json"))
        self.assertEqual(r.returncode, 2)
        # Report publication failure must not damage a prior report.
        r = self.cli("ap-check", "--source", str(self.source),
                     "--ledger", str(self.db), "--output", str(self.output))
        self.assertEqual(r.returncode, 0, r.stderr)
        check = json.loads(self.output.read_text())
        proposal = self.dir / "proposal.json"
        write_json(proposal, proposal_from_check(check))
        self.cli("ap-post", "--source", str(self.source),
                 "--ledger", str(self.db),
                 "--proposal", str(proposal), "--request-id", "R1")
        report_path = self.dir / "report.json"
        self.cli("ap-report", "--ledger", str(self.db),
                 "--output", str(report_path))
        before = report_path.read_text()
        r = self.cli("ap-report", "--ledger", str(self.db),
                     "--output", str(report_path.parent))  # directory target
        self.assertEqual(r.returncode, 2)
        self.assertEqual(report_path.read_text(), before)


if __name__ == "__main__":
    unittest.main()

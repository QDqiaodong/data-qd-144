"""Exact-matcher tests: boundary cases and independent brute-force cross-check.

The reference brute force enumerates every non-negative integer allocation
matrix over the legal edges without any memoization, recomputes the exact
rational line money and the single end-of-line banker's rounding independently,
and compares classification plus the two lexicographically smallest witnesses
against the production solver on many small random instances.
"""
import random
import unittest
from fractions import Fraction

from ap_ledger import matcher
from ap_ledger.matcher import bank_round_half_even, byte_key, legal_edges, solve


def brute_force(invoices, receipts, used_qty):
    """Plain enumeration of every feasible matrix; independent of the solver."""
    edges = legal_edges(invoices, receipts)
    receipts_sorted = sorted(receipts, key=lambda r: byte_key(r["id"]))
    price = {r["id"]: Fraction(r["price_num"], r["price_den"]) for r in receipts}
    remaining0 = {
        r["id"]: r["accepted_qty"] - used_qty.get(r["id"], 0)
        for r in receipts_sorted
    }
    flat = []
    for inv in sorted(invoices, key=lambda i: byte_key(i["id"])):
        for line in sorted(inv["lines"], key=lambda l: byte_key(l["id"])):
            comp = [
                r["id"]
                for r in receipts_sorted
                if r["supplier"] == inv["supplier"]
                and r["po_line"] == line["po_line"]
                and r["sku"] == line["sku"]
            ]
            flat.append((inv["id"], line, comp))

    solutions = []

    def rec(index, alloc, remaining):
        if index == len(flat):
            solutions.append(dict(alloc))
            return
        inv_id, line, comp = flat[index]
        if not comp:
            return
        split = [0] * len(comp)

        def fill(pos, left):
            if pos == len(comp) - 1:
                rid = comp[pos]
                if left <= remaining[rid]:
                    split[pos] = left
                    for r2, u in zip(comp, split):
                        remaining[r2] -= u
                        alloc[(inv_id, line["id"], r2)] = u
                    total = Fraction(0)
                    for r2, u in zip(comp, split):
                        if u:
                            total += price[r2] * u
                    net = round(total)
                    tax = round(Fraction(net * line["tax_bps"], 10_000))
                    if (net == line["net_cents"]
                            and tax == line["tax_cents"]):
                        rec(index + 1, alloc, remaining)
                    for r2, u in zip(comp, split):
                        remaining[r2] += u
                        del alloc[(inv_id, line["id"], r2)]
                return
            rid = comp[pos]
            for u in range(min(left, remaining[rid]) + 1):
                split[pos] = u
                fill(pos + 1, left - u)

        fill(0, line["qty"])

    rec(0, {}, dict(remaining0))
    vectors = sorted(
        tuple(alloc.get(edge, 0) for edge in edges) for alloc in solutions
    )
    return edges, vectors


def receipt(rid, supplier="s", po_line="p", sku="k", qty=12,
            num=1, den=1):
    return {
        "id": rid, "supplier": supplier, "po_line": po_line, "sku": sku,
        "accepted_qty": qty, "price_num": num, "price_den": den,
    }


def line(lid, po_line="p", sku="k", qty=1, net=1, bps=0, tax=0):
    return {
        "id": lid, "po_line": po_line, "sku": sku, "qty": qty,
        "net_cents": net, "tax_bps": bps, "tax_cents": tax,
    }


def invoice(iid, lines, supplier="s"):
    return {"id": iid, "supplier": supplier, "lines": lines}


class RoundingTest(unittest.TestCase):
    def test_half_even_boundaries(self):
        cases = {
            Fraction(1, 2): 0,    # 0.5 -> 0 (even)
            Fraction(3, 2): 2,    # 1.5 -> 2
            Fraction(5, 2): 2,    # 2.5 -> 2
            Fraction(7, 2): 4,    # 3.5 -> 4
            Fraction(9, 2): 4,    # 4.5 -> 4
            Fraction(11, 2): 6,   # 5.5 -> 6
            Fraction(1, 4): 0,
            Fraction(3, 4): 1,
        }
        for value, expected in cases.items():
            self.assertEqual(bank_round_half_even(value), expected, value)

    def test_matches_stdlib_on_sweep(self):
        for n in range(0, 3000):
            for d in (1, 2, 3, 4, 7, 8, 13, 100, 333):
                value = Fraction(n, d)
                self.assertEqual(bank_round_half_even(value), round(value))

    def test_rejects_floats(self):
        with self.assertRaises(TypeError):
            bank_round_half_even(0.5)


class MatcherAcceptanceTest(unittest.TestCase):
    def test_money_mismatch_forces_later_quantity_plan(self):
        # Same key, two receipts with different unit prices.  The first vector
        # (2,0) prices 20 and fails; the correct claim 11 only fits (1,1),
        # which the search must find after backtracking.
        receipts = [
            receipt("r1", num=10, den=1, qty=2),
            receipt("r2", num=1, den=1, qty=2),
        ]
        invoices = [invoice("i1", [line("l1", qty=2, net=11)])]
        status, witnesses = solve(invoices, receipts, {})
        self.assertEqual(status, "unique")
        vector = witnesses[0]["vector"]
        self.assertEqual(vector, (1, 1))  # edges (i1,l1,r1),(i1,l1,r2)

    def test_per_receipt_rounding_then_sum_is_illegal(self):
        # One unit from each of two receipts priced 3/2.  Per receipt 1.5 would
        # round to 2+2=4, but the contract rounds the exact line total 3.0 once
        # to 3.  Claim 4 must be rejected; claim 3 accepted.
        receipts = [
            receipt("r1", num=3, den=2, qty=1),
            receipt("r2", num=3, den=2, qty=1),
        ]
        bad = [invoice("i1", [line("l1", qty=2, net=4)])]
        self.assertEqual(solve(bad, receipts, {})[0], "infeasible")
        good = [invoice("i1", [line("l1", qty=2, net=3)])]
        status, witnesses = solve(good, receipts, {})
        self.assertEqual(status, "unique")
        record = witnesses[0]["lines"][0]
        self.assertEqual(record["net_fraction"], "3/1")
        self.assertEqual(record["net_cents"], 3)

    def test_tax_half_even_boundary(self):
        # net=1, bps=5000 -> 0.5 cent tax -> half-even gives 0.
        receipts = [receipt("r1", num=1, den=1, qty=1)]
        ok = [invoice("i1", [line("l1", qty=1, net=1, bps=5000, tax=0)])]
        self.assertEqual(solve(ok, receipts, {})[0], "unique")
        bad = [invoice("i1", [line("l1", qty=1, net=1, bps=5000, tax=1)])]
        self.assertEqual(solve(bad, receipts, {})[0], "infeasible")
        # net=3, bps=5000 -> 1.5 -> half-even gives 2.
        receipts3 = [receipt("r1", num=3, den=1, qty=1)]
        ok3 = [invoice("i1", [line("l1", qty=1, net=3, bps=5000, tax=2)])]
        status, witnesses = solve(ok3, receipts3, {})
        self.assertEqual(status, "unique")
        self.assertEqual(witnesses[0]["lines"][0]["tax_cents"], 2)

    def test_two_invoices_compete_for_one_receipt(self):
        # One receipt of capacity 2 cannot cover two invoices needing 2 each.
        receipts = [receipt("r1", qty=2)]
        invoices = [
            invoice("i1", [line("l1", qty=2, net=2)]),
            invoice("i2", [line("l2", qty=2, net=2)]),
        ]
        self.assertEqual(solve(invoices, receipts, {})[0], "infeasible")
        # Capacity 4 makes the joint allocation uniquely feasible.
        receipts[0]["accepted_qty"] = 4
        status, witnesses = solve(invoices, receipts, {})
        self.assertEqual(status, "unique")
        occupancy = {
            row["receipt_id"]: row
            for row in witnesses[0]["receipt_occupancy"]
        }
        self.assertEqual(occupancy["r1"]["allocated_qty"], 4)
        self.assertEqual(occupancy["r1"]["remaining_qty"], 0)

    def test_quantity_feasible_but_money_impossible(self):
        receipts = [
            receipt("r1", num=10, den=1, qty=2),
            receipt("r2", num=1, den=1, qty=2),
        ]
        invoices = [invoice("i1", [line("l1", qty=2, net=99)])]
        status, witnesses = solve(invoices, receipts, {})
        self.assertEqual(status, "infeasible")
        self.assertEqual(witnesses, [])

    def test_same_amount_two_matrices_is_ambiguous(self):
        receipts = [
            receipt("r1", num=1, den=1, qty=2),
            receipt("r2", num=1, den=1, qty=2),
        ]
        invoices = [invoice("i1", [line("l1", qty=2, net=2)])]
        status, witnesses = solve(invoices, receipts, {})
        self.assertEqual(status, "ambiguous")
        self.assertEqual(len(witnesses), 2)
        self.assertEqual(witnesses[0]["vector"], (0, 2))
        self.assertEqual(witnesses[1]["vector"], (1, 1))
        # The differing pairings must not be merged by equal money.
        self.assertNotEqual(
            witnesses[0]["lines"][0]["allocations"],
            witnesses[1]["lines"][0]["allocations"],
        )

    def test_cross_line_ambiguity_two_invoices(self):
        # Two unit lines competing for two equal-priced receipts: the two
        # swapped matrices carry identical money but are distinct witnesses.
        receipts = [
            receipt("r1", num=1, den=1, qty=2),
            receipt("r2", num=1, den=1, qty=2),
        ]
        invoices = [
            invoice("i1", [line("l1", qty=1, net=1)]),
            invoice("i2", [line("l2", qty=1, net=1)]),
        ]
        status, witnesses = solve(invoices, receipts, {})
        self.assertEqual(status, "ambiguous")
        edges = legal_edges(invoices, receipts)
        self.assertEqual(
            edges,
            [("i1", "l1", "r1"), ("i1", "l1", "r2"),
             ("i2", "l2", "r1"), ("i2", "l2", "r2")],
        )
        self.assertEqual(witnesses[0]["vector"], (0, 1, 0, 1))
        self.assertEqual(witnesses[1]["vector"], (0, 1, 1, 0))

    def test_supplier_po_line_sku_gate(self):
        receipts = [
            receipt("r1", supplier="s1"),
            receipt("r2", supplier="s2"),
        ]
        invoices = [invoice("i1", [line("l1", qty=1, net=1)], supplier="s1")]
        # r1 alone can cover the line; r2 is not a legal edge.
        status, witnesses = solve(invoices, receipts, {})
        self.assertEqual(status, "unique")
        self.assertEqual(
            legal_edges(invoices, receipts), [("i1", "l1", "r1")]
        )
        # Without any compatible receipt it is infeasible, not an error.
        invoices[0]["supplier"] = "s3"
        self.assertEqual(solve(invoices, receipts, {})[0], "infeasible")

    def test_prior_occupation_reduces_capacity(self):
        receipts = [receipt("r1", qty=3)]
        invoices = [invoice("i1", [line("l1", qty=2, net=2)])]
        # Two units already held live leave only one for a qty-2 line.
        self.assertEqual(
            solve(invoices, receipts, {"r1": 2})[0], "infeasible"
        )
        status, witnesses = solve(invoices, receipts, {"r1": 1})
        self.assertEqual(status, "unique")
        row = witnesses[0]["receipt_occupancy"][0]
        self.assertEqual(row["prior_used_qty"], 1)
        self.assertEqual(row["allocated_qty"], 2)
        self.assertEqual(row["remaining_qty"], 0)


class BruteForceCrossCheck(unittest.TestCase):
    def test_random_small_instances(self):
        rng = random.Random(20261116)
        key_pairs = [("p1", "k1"), ("p2", "k2")]
        checked = 0
        for _ in range(180):
            n_receipts = rng.randrange(1, 5)
            receipts = []
            for i in range(n_receipts):
                po_line, sku = key_pairs[rng.randrange(len(key_pairs))]
                receipts.append(
                    receipt(
                        f"r{i}",
                        supplier="s",
                        po_line=po_line,
                        sku=sku,
                        qty=rng.randrange(0, 5),
                        num=rng.randrange(1, 6),
                        den=rng.randrange(1, 4),
                    )
                )
            n_invoices = rng.randrange(1, 3)
            invoices = []
            total_qty = 0
            for i in range(n_invoices):
                n_lines = rng.randrange(1, 3)
                lines = []
                for j in range(n_lines):
                    po_line, sku = key_pairs[rng.randrange(len(key_pairs))]
                    qty = rng.randrange(1, 4)
                    total_qty += qty
                    bps = rng.choice([0, 2500, 5000, 7500, 10000])
                    # Half of the instances claim a computed-correct amount,
                    # the rest claim a random amount to create infeasibles.
                    if rng.random() < 0.6:
                        compatible = [
                            r for r in receipts
                            if r["po_line"] == po_line and r["sku"] == sku
                        ]
                        if compatible:
                            chosen = rng.choice(compatible)
                            exact = (
                                Fraction(chosen["price_num"],
                                         chosen["price_den"]) * qty
                            )
                            net = round(exact)
                            tax = round(Fraction(net * bps, 10_000))
                        else:
                            net, tax = rng.randrange(0, 20), 0
                    else:
                        net = rng.randrange(0, 20)
                        tax = rng.randrange(0, 5)
                    lines.append(
                        line(f"l{i}-{j}", po_line=po_line, sku=sku,
                             qty=qty, net=net, bps=bps, tax=tax)
                    )
                invoices.append(invoice(f"i{i}", lines))
            if total_qty > 12:
                continue
            used = {
                r["id"]: rng.randrange(0, max(1, r["accepted_qty"] + 1))
                for r in receipts
                if rng.random() < 0.3
            }
            edges, brute_vectors = brute_force(invoices, receipts, used)
            status, witnesses = solve(invoices, receipts, used)
            if not brute_vectors:
                self.assertEqual(status, "infeasible")
                self.assertEqual(witnesses, [])
            else:
                self.assertIn(status, ("unique", "ambiguous"))
                expected_status = (
                    "unique" if len(brute_vectors) == 1 else "ambiguous"
                )
                self.assertEqual(status, expected_status)
                for k, witness in enumerate(witnesses):
                    self.assertEqual(witness["vector"], brute_vectors[k])
                    # Every reported line must satisfy the claimed money.
                    for record in witness["lines"]:
                        src_line = next(
                            ln for inv in invoices
                            for ln in inv["lines"]
                            if ln["id"] == record["line_id"]
                        )
                        self.assertEqual(record["net_cents"],
                                         src_line["net_cents"])
                        self.assertEqual(record["tax_cents"],
                                         src_line["tax_cents"])
                        total = Fraction(0)
                        for alloc in record["allocations"]:
                            if alloc["qty"]:
                                r = next(
                                    x for x in receipts
                                    if x["id"] == alloc["receipt_id"]
                                )
                                total += (
                                    Fraction(r["price_num"], r["price_den"])
                                    * alloc["qty"]
                                )
                        self.assertEqual(
                            record["net_fraction"],
                            f"{total.numerator}/{total.denominator}",
                        )
            checked += 1
        self.assertGreater(checked, 150)


if __name__ == "__main__":
    unittest.main()

"""Exact integer allocation matching between receipts and open invoices.

For every not-yet-posted invoice line we seek a non-negative integer allocation
matrix ``x[invoice_id, line_id, receipt_id]`` such that:

* the entries of each line sum to exactly the line ``qty`` (no unit splitting,
  no line left uncovered);
* an edge exists only when supplier / po_line / sku are equal;
* the total units taken from each receipt (the live postings supplied by the
  ledger snapshot plus the new allocations) never exceed ``accepted_qty``.

A quantity matrix is only a *feasible solution* when both money claims hold for
every line, computed without any floating point:

* ``net_cents`` equals the single banker's (half-even) rounding of the exact
  rational sum ``sum(x * price_num / price_den)`` performed **once at the end
  of the line** -- per-receipt rounding before summing is illegal;
* ``tax_cents`` equals one identical rounding of
  ``net_cents * tax_bps / 10000``.

A quantity-feasible matrix whose money does not match forces backtracking to
other quantity pairings; it never gets rounded into acceptance.

Witness ordering is fully deterministic.  Every legal edge -- including edges
carrying zero units -- is ordered by the UTF-8 byte ordering of
``(invoice_id, line_id, receipt_id)`` and forms one coordinate of the witness
vector.  Depth-first enumeration walks lines in that canonical order and
receipt quantities ascending, so feasible matrices appear in lexicographically
ascending vector order; only the first two distinct ones are retained.

The search memoizes *suffixes* keyed by ``(line index, remaining capacities)``.
A suffix depends only on the lines still to fill and the capacities left, never
on the prefix that consumed them, so revisiting a state through a different
prefix reuses its (at most two) smallest completions while distinct prefixes
still produce distinct full matrices.  This keeps the contractual size limits
(<= 6 open lines, <= 12 open units, <= 6 receipts) trivially bounded.
"""
from fractions import Fraction

from .validate import MAX_OPEN_LINES, MAX_OPEN_QTY


def byte_key(value):
    return value.encode("utf-8")


def bank_round_half_even(value):
    """Round a non-negative exact Fraction to the nearest integer, half-even.

    Python's ``round(Fraction)`` already implements round-half-even on the
    exact rational, but the rule is spelled out explicitly so the boundary
    behaviour is testable in isolation.
    """
    if not isinstance(value, Fraction):
        raise TypeError("bank_round_half_even expects an exact Fraction")
    floor = value.numerator // value.denominator
    diff = value - floor
    if diff * 2 < 1:
        return floor
    if diff * 2 > 1:
        return floor + 1
    # Exact half: choose the even neighbour.
    return floor if floor % 2 == 0 else floor + 1


def _compatible_receipts(line, invoice_supplier, receipts):
    """Receipts sharing the (supplier, po_line, sku) key, byte-sorted by id."""
    out = [
        receipt
        for receipt in receipts
        if receipt["supplier"] == invoice_supplier
        and receipt["po_line"] == line["po_line"]
        and receipt["sku"] == line["sku"]
    ]
    return sorted(out, key=lambda r: byte_key(r["id"]))


def legal_edges(invoices, receipts):
    """All legal (invoice, line, receipt) edges ordered by UTF-8 bytes.

    Edges with zero allocated quantity are kept explicitly: the contract
    compares full vectors over the legal edge set, including zeros.
    """
    edges = []
    for invoice in sorted(invoices, key=lambda inv: byte_key(inv["id"])):
        for line in sorted(invoice["lines"], key=lambda ln: byte_key(ln["id"])):
            for receipt in _compatible_receipts(line, invoice["supplier"], receipts):
                edges.append((invoice["id"], line["id"], receipt["id"]))
    return edges


def solve(invoices, receipts, used_qty, limit_witnesses=2):
    """Return ``(status, witnesses)`` with status infeasible/unique/ambiguous.

    ``used_qty`` maps receipt id to the units already held by live (not
    reversed) postings; those units reduce the capacity available here.
    Each witness is a dict with the full per-line evidence.
    """
    if sum(len(inv["lines"]) for inv in invoices) > MAX_OPEN_LINES:
        raise ValueError("too many open invoice lines")
    if sum(line["qty"] for inv in invoices for line in inv["lines"]) > MAX_OPEN_QTY:
        raise ValueError("too many open invoice units")

    receipts = sorted(receipts, key=lambda r: byte_key(r["id"]))

    def _price(receipt):
        if "price" in receipt:
            return receipt["price"]
        return Fraction(receipt["price_num"], receipt["price_den"])

    receipt_price = {r["id"]: _price(r) for r in receipts}
    initial_remaining = tuple(
        r["accepted_qty"] - used_qty.get(r["id"], 0) for r in receipts
    )
    if any(cap < 0 for cap in initial_remaining):
        # Defensive: the ledger consistency layer normally rejects this first.
        return "infeasible", []

    # Flatten lines in the canonical edge ordering (invoice id, line id bytes).
    flat_lines = []
    for invoice in sorted(invoices, key=lambda inv: byte_key(inv["id"])):
        for line in sorted(invoice["lines"], key=lambda ln: byte_key(ln["id"])):
            compatible = _compatible_receipts(line, invoice["supplier"], receipts)
            if not compatible:
                return "infeasible", []
            positions = [receipts.index(receipt) for receipt in compatible]
            flat_lines.append(
                {
                    "invoice_id": invoice["id"],
                    "supplier": invoice["supplier"],
                    "line": line,
                    "positions": positions,
                }
            )

    memo = {}

    def suffixes(index, remaining):
        """At most two lexicographically smallest suffix solutions.

        A solution is a nested tuple of per-line split tuples, each split
        holding ``(receipt_position, units)`` pairs (zeros included) in
        receipt byte order.  Memoization is prefix-independent: the suffix
        only depends on the lines left and the capacities remaining.
        """
        key = (index, remaining)
        if key in memo:
            return memo[key]
        if index == len(flat_lines):
            memo[key] = ((),)
            return memo[key]
        item = flat_lines[index]
        line = item["line"]
        positions = item["positions"]
        split = [0] * len(positions)
        found = []

        def money_ok(units_by_pos):
            total = Fraction(0)
            for pos, units in zip(positions, units_by_pos):
                if units:
                    total += receipt_price[receipts[pos]["id"]] * units
            net = bank_round_half_even(total)
            if net != line["net_cents"]:
                return False, total, net
            tax = bank_round_half_even(
                Fraction(net * line["tax_bps"], 10_000)
            )
            return tax == line["tax_cents"], total, net

        def fill(pos_index, left):
            if len(found) >= limit_witnesses:
                return
            if pos_index == len(positions) - 1:
                global_pos = positions[pos_index]
                if left <= remaining[global_pos]:
                    split[pos_index] = left
                    ok, total, net = money_ok(split)
                    if ok:
                        new_remaining = list(remaining)
                        for pos, units in zip(positions, split):
                            new_remaining[pos] -= units
                        for tail in suffixes(index + 1, tuple(new_remaining)):
                            found.append((tuple(split), tail))
                            if len(found) >= limit_witnesses:
                                break
                return
            global_pos = positions[pos_index]
            cap = min(left, remaining[global_pos])
            for units in range(cap + 1):
                split[pos_index] = units
                fill(pos_index + 1, left - units)
                if len(found) >= limit_witnesses:
                    break

        fill(0, line["qty"])
        memo[key] = tuple(found)
        return memo[key]

    raw_solutions = suffixes(0, initial_remaining)
    if not raw_solutions:
        return "infeasible", []

    edges = legal_edges(invoices, receipts)
    witnesses = []
    for root in raw_solutions:
        # Flatten the nested split tuples into per-edge allocated quantities.
        alloc_by_edge = {}
        per_line_splits = []
        node = root
        index = 0
        while node:
            split_tuple, node = node
            per_line_splits.append(split_tuple)
            index += 1
        for item, split_tuple in zip(flat_lines, per_line_splits):
            line = item["line"]
            for pos, units in zip(item["positions"], split_tuple):
                alloc_by_edge[
                    (item["invoice_id"], line["id"], receipts[pos]["id"])
                ] = units

        line_records = []
        for item, split_tuple in zip(flat_lines, per_line_splits):
            line = item["line"]
            total = Fraction(0)
            allocations = []
            for pos, units in zip(item["positions"], split_tuple):
                receipt = receipts[pos]
                if units:
                    total += receipt_price[receipt["id"]] * units
                allocations.append(
                    {
                        "receipt_id": receipt["id"],
                        "qty": units,
                        "price_num": receipt["price_num"],
                        "price_den": receipt["price_den"],
                    }
                )
            net = bank_round_half_even(total)
            tax = bank_round_half_even(
                Fraction(net * line["tax_bps"], 10_000)
            )
            line_records.append(
                {
                    "invoice_id": item["invoice_id"],
                    "line_id": line["id"],
                    "po_line": line["po_line"],
                    "sku": line["sku"],
                    "qty": line["qty"],
                    "net_fraction": f"{total.numerator}/{total.denominator}",
                    "net_cents": net,
                    "tax_bps": line["tax_bps"],
                    "tax_cents": tax,
                    "allocations": allocations,
                }
            )

        new_used_by_receipt = {r["id"]: 0 for r in receipts}
        for (_, _, receipt_id), units in alloc_by_edge.items():
            new_used_by_receipt[receipt_id] += units
        receipt_remaining = [
            {
                "receipt_id": receipt["id"],
                "supplier": receipt["supplier"],
                "po_line": receipt["po_line"],
                "sku": receipt["sku"],
                "accepted_qty": receipt["accepted_qty"],
                "prior_used_qty": used_qty.get(receipt["id"], 0),
                "allocated_qty": new_used_by_receipt[receipt["id"]],
                "remaining_qty": (
                    receipt["accepted_qty"]
                    - used_qty.get(receipt["id"], 0)
                    - new_used_by_receipt[receipt["id"]]
                ),
                "price_num": receipt["price_num"],
                "price_den": receipt["price_den"],
            }
            for receipt in receipts
        ]
        vector = tuple(alloc_by_edge.get(edge, 0) for edge in edges)
        witnesses.append(
            {
                "vector": vector,
                "lines": line_records,
                "receipt_occupancy": receipt_remaining,
            }
        )

    status = "unique" if len(witnesses) == 1 else "ambiguous"
    return status, witnesses

"""Exhaustive cross-check of the exact optimizer on small instances.

The reference implementation is intentionally written in the most direct way:
every work-order subset and every feasible piece->bar assignment is enumerated
without heuristic pruning, and the full contract objective is computed
independently.  The production optimizer must reach the identical optimum
(accepted set, used bars and B tie-break assignment) on random small cases.
"""
import itertools
import random
import unittest

from cutting_stock.stock_planner import (
    best_packing,
    byte_key,
    plan_open_orders,
    sort_by_id,
)


def brute_packing(pieces, bars, kerf):
    """Enumerate every feasible assignment; return the contract-best one."""
    pieces = list(pieces)
    bars = sort_by_id(bars)
    n = len(pieces)
    best = None
    assign = [-1] * n
    used = [0] * len(bars)
    counts = [0] * len(bars)
    tie_order = sorted(
        range(n),
        key=lambda j: (byte_key(pieces[j]["order"]), byte_key(pieces[j]["id"])),
    )

    def dfs(i):
        nonlocal best
        if i == n:
            used_idx = [j for j in range(len(bars)) if counts[j]]
            b_tuple = tuple(byte_key(bars[assign[j]]["id"]) for j in tie_order)
            candidate = (
                len(used_idx),
                sum(bars[j]["length"] for j in used_idx),
                b_tuple,
                tuple(assign),
            )
            if best is None or candidate < best:
                best = candidate
            return
        piece = pieces[i]
        for j, bar in enumerate(bars):
            if bar["grade"] < piece["min_grade"]:
                continue
            extra = piece["length"] + (kerf if counts[j] else 0)
            if used[j] + extra > bar["length"]:
                continue
            used[j] += extra
            counts[j] += 1
            assign[i] = j
            dfs(i + 1)
            used[j] -= extra
            counts[j] -= 1
            assign[i] = -1

    dfs(0)
    return best


def brute_plan(orders, bars, kerf):
    """Enumerate all order subsets; pick the globally best feasible one."""
    orders = sort_by_id(orders)
    bars = sort_by_id(bars)
    best = None
    for width in range(len(orders) + 1):
        for subset in itertools.combinations(range(len(orders)), width):
            chosen = [orders[j] for j in subset]
            pieces = [
                dict(piece, order=order["id"])
                for order in chosen
                for piece in order["pieces"]
            ]
            packing = brute_packing(pieces, bars, kerf)
            if packing is None:
                continue
            priority = sum(orders[j]["priority"] for j in subset)
            accepted_tuple = tuple(byte_key(orders[j]["id"]) for j in subset)
            objective = (
                -priority,
                -len(subset),
                packing[0],
                packing[1],
                accepted_tuple,
                packing[2],
            )
            if best is None or objective < best[0]:
                best = (objective, subset, packing)
    return best


class ExhaustivePlanner(unittest.TestCase):
    def test_packing_matches_brute_force(self):
        rng = random.Random(20261116)
        for trial in range(120):
            bar_n = rng.randrange(1, 5)
            bars = []
            for j in range(bar_n):
                bars.append({
                    "id": f"b{j}",
                    "length": rng.randrange(4, 13),
                    "grade": rng.randrange(0, 4),
                })
            piece_n = rng.randrange(1, 7)
            pieces = []
            for j in range(piece_n):
                pieces.append({
                    "id": f"p{j}",
                    "length": rng.randrange(1, 10),
                    "min_grade": rng.randrange(0, 4),
                    "order": f"o{rng.randrange(0, 3)}",
                })
            kerf = rng.randrange(0, 3)
            expected = brute_packing(pieces, bars, kerf)
            actual = best_packing(pieces, bars, kerf)
            if expected is None:
                self.assertIsNone(actual, trial)
                continue
            self.assertIsNotNone(actual, trial)
            self.assertEqual(
                (actual["bar_count"], actual["bar_length_sum"],
                 actual["b_tuple"]),
                (expected[0], expected[1], expected[2]),
                trial,
            )

    def test_joint_plan_matches_brute_force(self):
        rng = random.Random(424242)
        for trial in range(80):
            orders = []
            used_piece_ids = set()
            for o in range(rng.randrange(1, 4)):
                pieces = []
                for k in range(rng.randrange(1, 3)):
                    pid = f"p{o}-{k}"
                    used_piece_ids.add(pid)
                    pieces.append({
                        "id": pid,
                        "length": rng.randrange(1, 9),
                        "min_grade": rng.randrange(0, 3),
                    })
                orders.append({
                    "id": f"o{o}",
                    "priority": rng.randrange(1, 101),
                    "pieces": pieces,
                })
            bars = [
                {"id": f"b{j}", "length": rng.randrange(5, 14),
                 "grade": rng.randrange(0, 3)}
                for j in range(rng.randrange(1, 5))
            ]
            kerf = rng.randrange(0, 3)
            reuse_min = rng.randrange(1, 5)
            plan = plan_open_orders(orders, bars, kerf, reuse_min)
            expected = brute_plan(orders, bars, kerf)
            expected_subset = {orders[j]["id"] for j in expected[1]}
            self.assertEqual(set(plan["accepted"]), expected_subset, trial)
            expected_bar_count = expected[2][0]
            self.assertEqual(plan["totals"]["bars_used"],
                             expected_bar_count, trial)
            expected_remainder = expected[2][1] - sum(
                p["length"]
                for o in orders if o["id"] in expected_subset
                for p in o["pieces"]
            ) - kerf * (
                sum(len(o["pieces"]) for o in orders
                    if o["id"] in expected_subset)
                - expected_bar_count
            )
            self.assertEqual(plan["totals"]["total_remainder"],
                             expected_remainder, trial)

    def test_utf8_byte_order_tie_break(self):
        # One bar means only one of the equal-priority orders is accepted.
        # Byte order picks the Latin order id and the Latin bar id, not the
        # locale-dependent Unicode collation order.
        bars = [
            {"id": "α", "length": 5, "grade": 0},
            {"id": "a", "length": 5, "grade": 0},
        ]
        orders = [
            {"id": "单", "priority": 5,
             "pieces": [{"id": "p1", "length": 4, "min_grade": 0},
                        {"id": "p2", "length": 4, "min_grade": 0}]},
            {"id": "z", "priority": 5,
             "pieces": [{"id": "p3", "length": 4, "min_grade": 0},
                        {"id": "p4", "length": 4, "min_grade": 0}]},
        ]
        # Each order needs two bars (kerf 0, pieces of 4 cannot share a 5 bar),
        # but only two bars exist, so exactly one order can be accepted.
        plan = plan_open_orders(orders, bars, 0, 2)
        self.assertEqual(plan["accepted"], ["z"])
        self.assertEqual(
            [layout["bar_id"] for layout in plan["layouts"]], ["a", "α"]
        )


if __name__ == "__main__":
    unittest.main()

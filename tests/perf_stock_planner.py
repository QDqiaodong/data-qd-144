"""Reproducible resource record for the bounded exact optimizer.

Run with the project src on the path::

    PYTHONPATH=src python3 tests/perf_stock_planner.py

The optimizer solves an NP-hard cutting-stock problem inside the contractual
bounds (at most 12 available bars, 18 pending pieces, 8 open work orders).
This script reports wall-clock time and tracemalloc peak memory for:

* typical small/medium sizes,
* full-size random realistic instances,
* a deliberately adversarial dense instance (many small pieces on near-uniform
  bars), which is the hardest shape for the branch-and-bound search.

The numbers here are descriptive, not a performance contract; the functional
contract is the exhaustive cross-check in ``test_stock_planner.py``.
"""
import os
import random
import time
import tracemalloc

from cutting_stock.stock_planner import best_packing, plan_open_orders


def _random_instance(rng, n_pieces, n_bars):
    bars = [
        {"id": f"b{i}", "length": rng.randrange(8, 20),
         "grade": rng.randrange(0, 4)}
        for i in range(n_bars)
    ]
    pieces = [
        {"id": f"p{j}", "length": rng.randrange(3, 12),
         "min_grade": rng.randrange(0, 4), "order": "o0"}
        for j in range(n_pieces)
    ]
    return pieces, bars, rng.randrange(0, 3)


def measure(pieces, bars, kerf):
    tracemalloc.start()
    start = time.perf_counter()
    result = best_packing(pieces, bars, kerf)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed_ms, peak / 1024, result


def main():
    rng = random.Random(20261116)
    print("size (pieces/bars)  samples  max ms   median ms   peak KiB")
    sample_counts = {(6, 4): 8, (10, 7): 6, (14, 10): 3, (18, 12): 3}
    for (n_pieces, n_bars), samples in sample_counts.items():
        timings = []
        peak_kiB = 0
        for _ in range(samples):
            pieces, bars, kerf = _random_instance(rng, n_pieces, n_bars)
            ms, kiB, _ = measure(pieces, bars, kerf)
            timings.append(ms)
            peak_kiB = max(peak_kiB, kiB)
        timings.sort()
        print(
            f"{n_pieces:>6d}/{n_bars:<6d}        {samples:>2d}      "
            f"{timings[-1]:7.1f}   {timings[len(timings) // 2]:8.1f}"
            f"   {peak_kiB:9.0f}"
        )

    # Adversarial dense shape: 18 mixed small pieces, 12 uniform bars.  This is
    # the hardest shape for branch-and-bound and can take tens of seconds, so
    # it runs only when RUN_SLOW_PERF=1 is set explicitly.
    if os.environ.get("RUN_SLOW_PERF") == "1":
        dense_bars = [
            {"id": f"b{i}", "length": 12, "grade": i % 4} for i in range(12)
        ]
        dense_pieces = [
            {"id": f"d{i}", "length": (i % 7) + 1, "min_grade": i % 4,
             "order": "d1"}
            for i in range(18)
        ]
        ms, kiB, result = measure(dense_pieces, dense_bars, 0)
        print(
            f"adversarial dense 18/12: {ms:8.1f} ms  peak {kiB:7.0f} KiB"
            f"  bars={'infeasible' if result is None else result['bar_count']}"
        )

    # Joint planning across work-order subsets.  The full 8-order/18-piece
    # limit evaluates 255 subsets and can contain a hard packing instance, so
    # it is opt-in via RUN_SLOW_PERF; the default run uses a smaller joint
    # plan that still exercises subset enumeration and the tie-break logic.
    if os.environ.get("RUN_SLOW_PERF") == "1":
        n_orders, n_bars, pieces_per_order = 8, 12, 2
    else:
        n_orders, n_bars, pieces_per_order = 5, 8, 2
    plan_bars = [
        {"id": f"b{i}", "length": rng.randrange(10, 20),
         "grade": rng.randrange(0, 4)}
        for i in range(n_bars)
    ]
    orders = []
    piece_id = 0
    for o in range(n_orders):
        order_pieces = [
            {"id": f"q{piece_id + j}", "length": rng.randrange(3, 8),
             "min_grade": rng.randrange(0, 4)}
            for j in range(pieces_per_order)
        ]
        piece_id += pieces_per_order
        orders.append({"id": f"o{o}", "priority": rng.randrange(1, 101),
                       "pieces": order_pieces})
    tracemalloc.start()
    start = time.perf_counter()
    plan = plan_open_orders(orders, plan_bars, 2, 3)
    ms = (time.perf_counter() - start) * 1000
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(
        f"plan_open_orders {n_orders} orders: {ms:8.1f} ms  peak "
        f"{peak / 1024:7.0f} KiB"
        f"  accepted {len(plan['accepted'])}"
    )


if __name__ == "__main__":
    main()

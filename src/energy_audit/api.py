"""Testable API for the offline energy-attribution and uncertainty audit.

The audit is strictly read-only: it opens the warehouse with ``mode=ro`` and
``PRAGMA query_only=ON``, never creates a missing database, never writes any
table and never changes the schema.  The warehouse revision and every finished
plan layout are read inside *one* SQLite transaction, so a concurrent
``finish`` can never tear the snapshot: historical layout, finished identity
and revision always come from the same consistent view.

All energy and cost arithmetic uses :class:`fractions.Fraction`; no rounding
happens before allocation and no float ever reaches the report.  The joint
metering inference enumerates the complete Cartesian product of per-interval
candidate consumptions (at most 10 intervals with at most 3 candidates each),
filters it by the conjunction of all work-order budgets, and derives every
extremum and its witness vector from that single feasible set — never from
per-work-order or per-metric marginal bounds.
"""
import json
import os
import sqlite3
from bisect import bisect_right
from fractions import Fraction
from urllib.parse import quote

from .validate import AuditError, WarehouseStateError, parse_source


def _byte_key(value):
    return value.encode("utf-8")


# --- warehouse snapshot --------------------------------------------------------


def _connect_reader(db_path):
    """Open strictly read-only; never creates the database file."""
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.isolation_level = None
    return conn


def _load_warehouse_snapshot(db_path, plan_ids):
    """Read the revision and all referenced finished plans in one transaction.

    Only ``plan_history`` is consulted: open and started (active) plan
    snapshots are never accepted as a layout source, and the current bar stock
    is never used as a substitute for the historical layout.
    """
    if not os.path.exists(db_path):
        raise WarehouseStateError(
            "warehouse database does not exist: " + str(db_path)
        )
    try:
        conn = _connect_reader(db_path)
    except sqlite3.Error as exc:
        raise WarehouseStateError(
            f"warehouse is not a readable database: {exc}"
        ) from exc
    try:
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT value FROM meta WHERE key='revision'"
            ).fetchone()
            if row is None:
                raise WarehouseStateError(
                    "warehouse has no stock-warehouse schema (missing revision)"
                )
            revision = int(row["value"])
            plans = {}
            for plan_id in sorted(plan_ids, key=_byte_key):
                prow = conn.execute(
                    "SELECT plan_id,started_revision,finished_revision,"
                    "plan_json FROM plan_history WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()
                if prow is None:
                    raise WarehouseStateError(
                        "plan is not a finished plan in the warehouse: "
                        + plan_id
                    )
                plans[plan_id] = {
                    "plan_id": prow["plan_id"],
                    "started_revision": prow["started_revision"],
                    "finished_revision": prow["finished_revision"],
                    "plan": json.loads(prow["plan_json"]),
                }
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise WarehouseStateError(
                f"warehouse is not a readable database: {exc}"
            ) from exc
    finally:
        conn.close()
    return {"revision": revision, "plans": plans}


def _layout_weights(plan, plan_id, bar_id):
    """Work-order length shares of one finished bar layout.

    Weights are piece-length proportions only: kerf loss and the remainder
    never enter the denominator.
    """
    for layout in plan["layouts"]:
        if layout["bar_id"] == bar_id:
            lengths = {}
            for piece in layout["pieces"]:
                order = piece["work_order"]
                lengths[order] = lengths.get(order, 0) + piece["length"]
            total = sum(lengths.values())
            return {
                order: Fraction(length, total)
                for order, length in sorted(
                    lengths.items(), key=lambda item: _byte_key(item[0])
                )
            }
    raise WarehouseStateError(
        f"bar {bar_id} is not part of finished plan {plan_id}"
    )


# --- exact time integration ----------------------------------------------------


def _meter_segments(meter, runs, tariffs):
    """Split the meter reading domain at every reading, run and tariff
    boundary.  Each elementary segment lies inside exactly one reading
    interval and one tariff segment and at most one run."""
    points = set()
    for interval in meter["intervals"]:
        points.add(interval["start"])
        points.add(interval["end"])
    for run in runs:
        points.add(run["start"])
        points.add(run["end"])
    for tariff in tariffs:
        points.add(tariff["start"])
        points.add(tariff["end"])
    ordered = sorted(points)
    reading_times = [t for t, _ in meter["readings"]]
    segments = []
    for left, right in zip(ordered, ordered[1:]):
        interval_idx = bisect_right(reading_times, left) - 1
        price = None
        for tariff in tariffs:
            if tariff["start"] <= left < tariff["end"]:
                price = Fraction(tariff["price_num"], tariff["price_den"])
                break
        run_id = None
        for run in runs:
            if run["start"] <= left < run["end"]:
                run_id = run["id"]
                break
        segments.append(
            {
                "start": left,
                "end": right,
                "interval": interval_idx,
                "price": price,
                "run_id": run_id,
            }
        )
    return segments


def _build_coefficients(source, interval_index):
    """Linear forms in the global d-vector for every reported metric.

    Returns a list of ``(metric_key, coefficients)`` pairs where each
    coefficient list is aligned with the canonical interval order.  All
    coefficients are non-negative exact rationals.
    """
    meters = source["meters"]
    runs = source["runs"]
    n_intervals = len(interval_index)
    runs_by_meter = {meter["id"]: [] for meter in meters}
    for run in runs:
        runs_by_meter[run["meter_id"]].append(run)

    run_energy = {run["id"]: [Fraction(0)] * n_intervals for run in runs}
    run_cost = {run["id"]: [Fraction(0)] * n_intervals for run in runs}
    idle_energy = {
        meter["id"]: [Fraction(0)] * n_intervals for meter in meters
    }
    idle_cost = {
        meter["id"]: [Fraction(0)] * n_intervals for meter in meters
    }
    total_cost = [Fraction(0)] * n_intervals

    for meter in meters:
        meter_id = meter["id"]
        tariffs = source["tariffs_by_meter"][meter_id]
        segments = _meter_segments(
            meter, runs_by_meter[meter_id], tariffs
        )
        for segment in segments:
            local = segment["interval"]
            interval = meter["intervals"][local]
            g = interval_index[(meter_id, local)]
            share = Fraction(
                segment["end"] - segment["start"], interval["dt"]
            )
            cost_share = share * segment["price"]
            total_cost[g] += cost_share
            if segment["run_id"] is None:
                idle_energy[meter_id][g] += share
                idle_cost[meter_id][g] += cost_share
            else:
                run_energy[segment["run_id"]][g] += share
                run_cost[segment["run_id"]][g] += cost_share

    forms = []
    work_orders = sorted(
        {order for run in runs for order in run["weights"]},
        key=_byte_key,
    )
    for order in work_orders:
        energy = [Fraction(0)] * n_intervals
        cost = [Fraction(0)] * n_intervals
        for run in runs:
            weight = run["weights"].get(order)
            if weight is None:
                continue
            for g in range(n_intervals):
                energy[g] += weight * run_energy[run["id"]][g]
                cost[g] += weight * run_cost[run["id"]][g]
        forms.append((("work_order", order, "energy"), energy))
        forms.append((("work_order", order, "cost"), cost))
    for meter in meters:
        meter_id = meter["id"]
        forms.append((("idle", meter_id, "energy"), idle_energy[meter_id]))
        forms.append((("idle", meter_id, "cost"), idle_cost[meter_id]))
    forms.append((("total", None, "energy"), [Fraction(1)] * n_intervals))
    forms.append((("total", None, "cost"), total_cost))
    return forms, work_orders


# --- joint interpretation enumeration -------------------------------------------


def _enumerate(source, interval_entries, forms):
    """Enumerate every complete d-vector in canonical lexicographic order and
    keep exactly those satisfying the conjunction of all budgets.

    Returns ``(count, extrema)`` where ``extrema[i]`` holds ``(min, min_wit,
    max, max_wit)`` for form ``i``; witnesses are the lexicographically
    smallest vectors achieving the respective extremum.
    """
    candidates = [entry["candidates"] for entry in interval_entries]
    n_forms = len(forms)
    empty = (0, [(None, None, None, None)] * n_forms)
    if any(not cands for cands in candidates):
        return empty

    budgets = {budget["work_order"]: budget for budget in source["budgets"]}
    budget_rows = []  # (form_index, lo, hi)
    for idx, (key, _) in enumerate(forms):
        if key[0] == "work_order" and key[2] == "energy" and key[1] in budgets:
            budget = budgets[key[1]]
            budget_rows.append(
                (idx, Fraction(budget["min_energy"]), Fraction(budget["max_energy"]))
            )

    n_intervals = len(candidates)
    coeffs = [coeff for _, coeff in forms]
    # Precompute each candidate's contribution vector once; the recursion then
    # only adds/subtracts exact rationals instead of re-multiplying per node.
    contributions = [
        [[coeffs[i][k] * d for i in range(n_forms)] for d in candidates[k]]
        for k in range(n_intervals)
    ]
    # Suffix achievable ranges for the budget forms (coefficients are
    # non-negative, so the extremes sit at the candidate endpoints).
    suffix_min = [[Fraction(0)] * (n_intervals + 1) for _ in budget_rows]
    suffix_max = [[Fraction(0)] * (n_intervals + 1) for _ in budget_rows]
    for row, (idx, _, _) in enumerate(budget_rows):
        coeff = coeffs[idx]
        for k in range(n_intervals - 1, -1, -1):
            suffix_min[row][k] = suffix_min[row][k + 1] + coeff[k] * candidates[k][0]
            suffix_max[row][k] = suffix_max[row][k + 1] + coeff[k] * candidates[k][-1]

    acc = [Fraction(0)] * n_forms
    vector = [0] * n_intervals
    extrema = [[None, None, None, None] for _ in range(n_forms)]
    count = 0

    def recurse(k):
        nonlocal count
        for row, (idx, lo, hi) in enumerate(budget_rows):
            partial = acc[idx]
            if partial + suffix_min[row][k] > hi:
                return
            if partial + suffix_max[row][k] < lo:
                return
        if k == n_intervals:
            count += 1
            for i in range(n_forms):
                value = acc[i]
                row = extrema[i]
                # Enumeration is lexicographic, so the first vector reaching
                # an extremum is the lexicographically smallest achiever.
                if row[0] is None or value < row[0]:
                    row[0] = value
                    row[1] = list(vector)
                if row[2] is None or value > row[2]:
                    row[2] = value
                    row[3] = list(vector)
            return
        for choice, d in enumerate(candidates[k]):
            vector[k] = d
            added = contributions[k][choice]
            for i in range(n_forms):
                if added[i]:
                    acc[i] += added[i]
            recurse(k + 1)
            for i in range(n_forms):
                if added[i]:
                    acc[i] -= added[i]
        vector[k] = 0

    recurse(0)
    if count == 0:
        return empty
    return count, [tuple(row) for row in extrema]


# --- report ---------------------------------------------------------------------


def _fraction_json(value):
    return {"num": value.numerator, "den": value.denominator}


def _metric_json(extremum):
    minimum, min_witness, maximum, max_witness = extremum
    return {
        "min": _fraction_json(minimum),
        "min_witness": min_witness,
        "max": _fraction_json(maximum),
        "max_witness": max_witness,
    }


def audit(warehouse_path, source_data, source_sha256):
    """Run the full audit and return the deterministic report dictionary."""
    source = parse_source(source_data)
    plan_ids = {run["plan_id"] for run in source["runs"]}
    warehouse = _load_warehouse_snapshot(warehouse_path, plan_ids)

    for run in source["runs"]:
        plan_entry = warehouse["plans"][run["plan_id"]]
        run["weights"] = _layout_weights(
            plan_entry["plan"], run["plan_id"], run["bar_id"]
        )

    work_orders = sorted(
        {order for run in source["runs"] for order in run["weights"]},
        key=_byte_key,
    )
    order_set = set(work_orders)
    for budget in source["budgets"]:
        if budget["work_order"] not in order_set:
            raise AuditError(
                "budget work order does not appear in the referenced run "
                "layouts: " + budget["work_order"]
            )

    # Canonical interpretation order: (meter_id UTF-8 bytes, interval start).
    interval_index = {}
    intervals_report = []
    interval_entries = []
    g = 0
    for meter in source["meters"]:
        for local, interval in enumerate(meter["intervals"]):
            interval_index[(meter["id"], local)] = g
            intervals_report.append(
                {
                    "meter_id": meter["id"],
                    "start": interval["start"],
                    "end": interval["end"],
                    "candidates": list(interval["candidates"]),
                }
            )
            interval_entries.append({"candidates": interval["candidates"]})
            g += 1

    forms, _ = _build_coefficients(source, interval_index)
    count, extrema = _enumerate(source, interval_entries, forms)

    plans_report = []
    for plan_id in sorted(plan_ids, key=_byte_key):
        entry = warehouse["plans"][plan_id]
        bars = sorted(
            (run["bar_id"] for run in source["runs"]
             if run["plan_id"] == plan_id),
            key=_byte_key,
        )
        plans_report.append(
            {
                "plan_id": plan_id,
                "started_revision": entry["started_revision"],
                "finished_revision": entry["finished_revision"],
                "bars": bars,
            }
        )
    runs_report = []
    for run in source["runs"]:
        runs_report.append(
            {
                "id": run["id"],
                "meter_id": run["meter_id"],
                "plan_id": run["plan_id"],
                "bar_id": run["bar_id"],
                "start": run["start"],
                "end": run["end"],
                "weights": [
                    {
                        "work_order": order,
                        "weight": _fraction_json(weight),
                    }
                    for order, weight in sorted(
                        run["weights"].items(),
                        key=lambda item: _byte_key(item[0]),
                    )
                ],
            }
        )

    report = {
        "status": "feasible" if count else "infeasible",
        "interpretations": count,
        "warehouse_revision": warehouse["revision"],
        "source_sha256": source_sha256,
        "intervals": intervals_report,
        "plans": plans_report,
        "runs": runs_report,
        "work_orders": None,
        "idle": None,
        "total": None,
    }
    if count:
        extrema_by_key = {
            key: extrema[idx] for idx, (key, _) in enumerate(forms)
        }
        report["work_orders"] = [
            {
                "work_order": order,
                "energy": _metric_json(
                    extrema_by_key[("work_order", order, "energy")]
                ),
                "cost": _metric_json(
                    extrema_by_key[("work_order", order, "cost")]
                ),
            }
            for order in work_orders
        ]
        report["idle"] = [
            {
                "meter_id": meter["id"],
                "energy": _metric_json(
                    extrema_by_key[("idle", meter["id"], "energy")]
                ),
                "cost": _metric_json(
                    extrema_by_key[("idle", meter["id"], "cost")]
                ),
            }
            for meter in source["meters"]
        ]
        report["total"] = {
            "energy": _metric_json(extrema_by_key[("total", None, "energy")]),
            "cost": _metric_json(extrema_by_key[("total", None, "cost")]),
        }
    return report

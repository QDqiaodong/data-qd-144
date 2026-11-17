"""Acceptance, oracle-comparison and CLI tests for the offline energy audit.

The oracle in this file is an independent brute-force implementation: it
recomputes candidate sets, attribution weights (read straight from
``plan_history``), per-vector metrics (via direct three-way time overlaps)
and every extremum from the raw source, then compares counts, ranges and
lexicographic tie witnesses against the package under test.
"""
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path

from cutting_stock import stock_api
from energy_audit import api
from energy_audit.validate import AuditError, WarehouseStateError, parse_source

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures"


# --- warehouse construction ----------------------------------------------------


def make_warehouse(db_path, groups, kerf=1, reuse_min=100):
    """Build a warehouse and finish one plan per group.

    Each group is ``(received_bars, orders)`` where ``received_bars`` is a
    list of ``(bar_id, length, grade)`` and ``orders`` a list of
    ``(order_id, [(length, min_grade), ...])``.  Bars are received in the same
    batch as the orders so every plan sees exactly its own stock.  Returns the
    finished plan ids in order.
    """
    stock_api.initialize(
        str(db_path), {"kerf": kerf, "reuse_min": reuse_min, "bars": []}
    )
    plan_ids = []
    revision = 0
    for index, (bars, orders) in enumerate(groups):
        operations = [
            {"op": "receive",
             "bar": {"id": bar_id, "length": length, "grade": grade}}
            for bar_id, length, grade in bars
        ]
        for order_id, pieces in orders:
            operations.append(
                {
                    "op": "order",
                    "id": order_id,
                    "priority": 50,
                    "pieces": [
                        {"id": f"{order_id}-p{j}", "length": length,
                         "min_grade": min_grade}
                        for j, (length, min_grade) in enumerate(pieces)
                    ],
                }
            )
        stock_api.apply_request(
            str(db_path),
            {"batch_id": f"g{index}-order", "expected_revision": revision,
             "operations": operations},
        )
        revision += 1
        stock_api.apply_request(
            str(db_path),
            {"batch_id": f"g{index}-start", "expected_revision": revision,
             "operations": [{"op": "start"}]},
        )
        revision += 1
        result = stock_api.apply_request(
            str(db_path),
            {"batch_id": f"g{index}-finish", "expected_revision": revision,
             "operations": [{"op": "finish"}]},
        )
        revision += 1
        plan_ids.append(result["finished_plan_id"])
    return plan_ids


def fixture_warehouse(db_path):
    """The warehouse matching fixtures/energy_source.json."""
    return make_warehouse(
        db_path,
        [([("bar-alpha", 10, 0)],
          [("wo-frame", [(2, 0)]), ("wo-brace", [(4, 0)])])],
    )


# --- independent oracle ----------------------------------------------------------


def oracle_intervals(source):
    """Canonical (meter_id UTF-8, interval start) interval list."""
    intervals = []
    for meter in sorted(source["meters"],
                        key=lambda item: item["id"].encode("utf-8")):
        readings = sorted((r["t"], r["value"]) for r in meter["readings"])
        for (t0, v0), (t1, v1) in zip(readings, readings[1:]):
            dt = t1 - t0
            delta = (v1 - v0) % meter["modulus"]
            cap = meter["max_power"] * dt
            candidates = tuple(range(delta, cap + 1, meter["modulus"]))
            intervals.append((meter["id"], t0, t1, dt, candidates))
    return intervals


def oracle_weights(db_path, source):
    """Attribution weights recomputed straight from plan_history."""
    conn = sqlite3.connect(str(db_path))
    weights = {}
    try:
        for run in source["runs"]:
            row = conn.execute(
                "SELECT plan_json FROM plan_history WHERE plan_id=?",
                (run["plan_id"],),
            ).fetchone()
            plan = json.loads(row[0])
            layout = next(
                item for item in plan["layouts"]
                if item["bar_id"] == run["bar_id"]
            )
            lengths = {}
            for piece in layout["pieces"]:
                order = piece["work_order"]
                lengths[order] = lengths.get(order, 0) + piece["length"]
            total = sum(lengths.values())
            weights[run["id"]] = {
                order: Fraction(length, total)
                for order, length in lengths.items()
            }
    finally:
        conn.close()
    return weights


def oracle_evaluate(source, weights, vector):
    """All metric values for one complete d-vector, from raw definitions."""
    intervals = oracle_intervals(source)
    run_energy, run_cost = {}, {}
    for run in source["runs"]:
        energy = Fraction(0)
        cost = Fraction(0)
        for g, (meter_id, start, end, dt, _) in enumerate(intervals):
            if meter_id != run["meter_id"]:
                continue
            d = vector[g]
            overlap = max(0, min(run["end"], end) - max(run["start"], start))
            if overlap:
                energy += Fraction(d * overlap, dt)
            for tariff in source["tariffs"]:
                if tariff["meter_id"] != meter_id:
                    continue
                shared = max(
                    0,
                    min(run["end"], end, tariff["end"])
                    - max(run["start"], start, tariff["start"]),
                )
                if shared:
                    cost += Fraction(d * shared, dt) * Fraction(
                        tariff["price_num"], tariff["price_den"]
                    )
        run_energy[run["id"]] = energy
        run_cost[run["id"]] = cost
    metrics = {}
    for run in source["runs"]:
        for order, weight in weights[run["id"]].items():
            e_key = ("work_order", order, "energy")
            c_key = ("work_order", order, "cost")
            metrics[e_key] = metrics.get(e_key, Fraction(0)) \
                + weight * run_energy[run["id"]]
            metrics[c_key] = metrics.get(c_key, Fraction(0)) \
                + weight * run_cost[run["id"]]
    meter_energy, meter_cost = {}, {}
    for g, (meter_id, start, end, dt, _) in enumerate(intervals):
        d = vector[g]
        meter_energy[meter_id] = meter_energy.get(meter_id, Fraction(0)) + d
        cost = Fraction(0)
        for tariff in source["tariffs"]:
            if tariff["meter_id"] != meter_id:
                continue
            shared = max(0, min(end, tariff["end"]) - max(start, tariff["start"]))
            if shared:
                cost += Fraction(d * shared, dt) * Fraction(
                    tariff["price_num"], tariff["price_den"]
                )
        meter_cost[meter_id] = meter_cost.get(meter_id, Fraction(0)) + cost
    for meter_id in meter_energy:
        run_e = sum(
            (run_energy[run["id"]] for run in source["runs"]
             if run["meter_id"] == meter_id),
            Fraction(0),
        )
        run_c = sum(
            (run_cost[run["id"]] for run in source["runs"]
             if run["meter_id"] == meter_id),
            Fraction(0),
        )
        metrics[("idle", meter_id, "energy")] = meter_energy[meter_id] - run_e
        metrics[("idle", meter_id, "cost")] = meter_cost[meter_id] - run_c
    metrics[("total", "energy")] = sum(meter_energy.values(), Fraction(0))
    metrics[("total", "cost")] = sum(meter_cost.values(), Fraction(0))
    return metrics


def oracle_extrema(source, weights):
    """Full joint enumeration over the Cartesian product of candidates."""
    intervals = oracle_intervals(source)
    candidate_lists = [candidates for *_, candidates in intervals]
    count = 0
    best = {}
    for vector in itertools.product(*candidate_lists):
        metrics = oracle_evaluate(source, weights, vector)
        feasible = all(
            Fraction(budget["min_energy"])
            <= metrics.get(
                ("work_order", budget["work_order"], "energy"), Fraction(0)
            )
            <= Fraction(budget["max_energy"])
            for budget in source["budgets"]
        )
        if not feasible:
            continue
        count += 1
        for key, value in metrics.items():
            row = best.setdefault(key, [None, None, None, None])
            if row[0] is None or value < row[0]:
                row[0] = value
                row[1] = list(vector)
            if row[2] is None or value > row[2]:
                row[2] = value
                row[3] = list(vector)
    return count, best


def report_metrics(report):
    metrics = {}
    for entry in report["work_orders"] or []:
        metrics[("work_order", entry["work_order"], "energy")] = entry["energy"]
        metrics[("work_order", entry["work_order"], "cost")] = entry["cost"]
    for entry in report["idle"] or []:
        metrics[("idle", entry["meter_id"], "energy")] = entry["energy"]
        metrics[("idle", entry["meter_id"], "cost")] = entry["cost"]
    if report["total"] is not None:
        metrics[("total", "energy")] = report["total"]["energy"]
        metrics[("total", "cost")] = report["total"]["cost"]
    return metrics


def metric_fraction(metric, extreme):
    return Fraction(metric[extreme]["num"], metric[extreme]["den"])


class OracleMixin:
    def assert_oracle_matches(self, db_path, source, report):
        weights = oracle_weights(db_path, source)
        count, best = oracle_extrema(source, weights)
        self.assertEqual(report["interpretations"], count)
        self.assertEqual(report["status"],
                         "feasible" if count else "infeasible")
        metrics = report_metrics(report)
        if count == 0:
            self.assertIsNone(report["work_orders"])
            self.assertIsNone(report["idle"])
            self.assertIsNone(report["total"])
            self.assertEqual(metrics, {})
            return weights
        self.assertEqual(set(metrics), set(best))
        for key, metric in metrics.items():
            omin, omin_witness, omax, omax_witness = best[key]
            self.assertEqual(metric_fraction(metric, "min"), omin, key)
            self.assertEqual(metric["min_witness"], omin_witness, key)
            self.assertEqual(metric_fraction(metric, "max"), omax, key)
            self.assertEqual(metric["max_witness"], omax_witness, key)
        for run in report["runs"]:
            reported = {
                item["work_order"]: metric_fraction(item, "weight")
                for item in run["weights"]
            }
            self.assertEqual(reported, weights[run["id"]])
        self.recheck_witnesses(source, weights, metrics)
        return weights

    def recheck_witnesses(self, source, weights, metrics):
        """Independently re-evaluate every extremum witness vector."""
        intervals = oracle_intervals(source)
        for key, metric in metrics.items():
            for extreme in ("min", "max"):
                witness = metric[extreme + "_witness"]
                self.assertEqual(len(witness), len(intervals))
                for d, (*_, candidates) in zip(witness, intervals):
                    self.assertIn(d, candidates)
                values = oracle_evaluate(source, weights, witness)
                # A witness must be a globally feasible interpretation.
                for budget in source["budgets"]:
                    energy = values.get(
                        ("work_order", budget["work_order"], "energy"),
                        Fraction(0),
                    )
                    self.assertLessEqual(
                        Fraction(budget["min_energy"]), energy
                    )
                    self.assertLessEqual(
                        energy, Fraction(budget["max_energy"])
                    )
                # It must reach the reported extremum.
                self.assertEqual(
                    values[key], metric_fraction(metric, extreme),
                    f"{key} {extreme}",
                )
                # Conservation: work orders + idle == whole-meter integral.
                wo_energy = sum(
                    (value for k, value in values.items()
                     if k[0] == "work_order" and k[2] == "energy"),
                    Fraction(0),
                )
                idle_energy = sum(
                    (value for k, value in values.items()
                     if k[0] == "idle" and k[2] == "energy"),
                    Fraction(0),
                )
                self.assertEqual(wo_energy + idle_energy,
                                 values[("total", "energy")])
                self.assertEqual(values[("total", "energy")], sum(witness))
                wo_cost = sum(
                    (value for k, value in values.items()
                     if k[0] == "work_order" and k[2] == "cost"),
                    Fraction(0),
                )
                idle_cost = sum(
                    (value for k, value in values.items()
                     if k[0] == "idle" and k[2] == "cost"),
                    Fraction(0),
                )
                self.assertEqual(wo_cost + idle_cost,
                                 values[("total", "cost")])


# --- validation ------------------------------------------------------------------


def valid_source():
    return {
        "meters": [
            {"id": "m1", "modulus": 100, "max_power": 2,
             "readings": [{"t": 0, "value": 0},
                          {"t": 10, "value": 5}]}
        ],
        "runs": [],
        "tariffs": [
            {"meter_id": "m1", "start": 0, "end": 10,
             "price_num": 1, "price_den": 1}
        ],
        "budgets": [],
    }


class ValidationTest(unittest.TestCase):
    def assert_bad(self, mutate):
        source = valid_source()
        mutate(source)
        with self.assertRaises(AuditError):
            parse_source(source)

    def test_top_level_exact_keys(self):
        self.assert_bad(lambda s: s.update(extra=1))
        self.assert_bad(lambda s: s.pop("meters"))
        self.assert_bad(lambda s: s.pop("budgets"))
        with self.assertRaises(AuditError):
            parse_source(["not", "an", "object"])

    def test_meter_fields_and_ranges(self):
        self.assert_bad(lambda s: s["meters"][0].update(unknown=1))
        self.assert_bad(lambda s: s["meters"][0].pop("modulus"))
        self.assert_bad(lambda s: s["meters"][0].update(modulus=1))
        self.assert_bad(lambda s: s["meters"][0].update(modulus=10**6 + 1))
        self.assert_bad(lambda s: s["meters"][0].update(modulus=True))
        self.assert_bad(lambda s: s["meters"][0].update(max_power=-1))
        self.assert_bad(lambda s: s["meters"][0].update(max_power=10**6 + 1))
        self.assert_bad(lambda s: s["meters"].append(
            {"id": "m1", "modulus": 2, "max_power": 0,
             "readings": [{"t": 0, "value": 0}, {"t": 1, "value": 0}]}))
        self.assert_bad(lambda s: s["meters"].append(
            {"id": "m2", "modulus": 2, "max_power": 0,
             "readings": [{"t": 0, "value": 0}]}))

    def test_reading_validation(self):
        def set_readings(source, readings):
            source["meters"][0]["readings"] = readings
        self.assert_bad(lambda s: set_readings(
            s, [{"t": 0, "value": 0}, {"t": 0, "value": 1}]))
        self.assert_bad(lambda s: set_readings(
            s, [{"t": -1, "value": 0}, {"t": 1, "value": 0}]))
        self.assert_bad(lambda s: set_readings(
            s, [{"t": 0, "value": 0}, {"t": 10**9 + 1, "value": 0}]))
        self.assert_bad(lambda s: set_readings(
            s, [{"t": 0, "value": 0}, {"t": 1, "value": 100}]))
        self.assert_bad(lambda s: set_readings(
            s, [{"t": 0, "value": 0}, {"t": 1, "value": False}]))
        self.assert_bad(lambda s: set_readings(
            s, [{"t": 0, "value": 0, "x": 1}, {"t": 1, "value": 0}]))
        # max_power*dt > 2*modulus is a format error.
        self.assert_bad(lambda s: s["meters"][0].update(
            modulus=2, max_power=3,
            readings=[{"t": 0, "value": 0}, {"t": 2, "value": 0}]))

    def test_interval_count_bounds(self):
        def eleven_intervals(source):
            source["meters"] = [
                {"id": "m1", "modulus": 100, "max_power": 0,
                 "readings": [{"t": t, "value": 0} for t in range(7)]},
                {"id": "m2", "modulus": 100, "max_power": 0,
                 "readings": [{"t": t, "value": 0} for t in range(6)]},
            ]
            source["tariffs"] = [
                {"meter_id": "m1", "start": 0, "end": 6,
                 "price_num": 1, "price_den": 1},
                {"meter_id": "m2", "start": 0, "end": 5,
                 "price_num": 1, "price_den": 1},
            ]
        self.assert_bad(eleven_intervals)
        self.assert_bad(lambda s: s.update(meters=[], tariffs=[]))

    def test_run_validation(self):
        plan_run = {"id": "r1", "meter_id": "m1", "plan_id": "p",
                    "bar_id": "b", "start": 0, "end": 5}
        self.assert_bad(lambda s: s["runs"].append(
            dict(plan_run, meter_id="nope")))
        self.assert_bad(lambda s: s["runs"].append(
            dict(plan_run, start=5, end=5)))
        self.assert_bad(lambda s: s["runs"].append(
            dict(plan_run, start=-1)))
        self.assert_bad(lambda s: s["runs"].append(
            dict(plan_run, end=11)))
        self.assert_bad(lambda s: s["runs"].append(dict(plan_run, extra=1)))

        def overlapping(source):
            source["runs"] = [
                dict(plan_run, id="r1", start=0, end=6),
                dict(plan_run, id="r2", plan_id="p2", start=6, end=10),
                dict(plan_run, id="r3", plan_id="p3", start=5, end=9),
            ]
        self.assert_bad(overlapping)

        def duplicate_pair(source):
            source["runs"] = [
                dict(plan_run, id="r1", start=0, end=5),
                dict(plan_run, id="r2", start=5, end=10),
            ]
        self.assert_bad(duplicate_pair)

        def duplicate_id(source):
            source["runs"] = [
                dict(plan_run, plan_id="p1", start=0, end=5),
                dict(plan_run, plan_id="p2", start=5, end=10),
            ]
        self.assert_bad(duplicate_id)

        def too_many(source):
            source["meters"][0]["readings"] = [
                {"t": t, "value": 0} for t in (0, 30)
            ]
            source["tariffs"] = [
                {"meter_id": "m1", "start": 0, "end": 30,
                 "price_num": 1, "price_den": 1}
            ]
            source["runs"] = [
                {"id": f"r{i}", "meter_id": "m1", "plan_id": f"p{i}",
                 "bar_id": "b", "start": i, "end": i + 1}
                for i in range(21)
            ]
        self.assert_bad(too_many)

    def test_tariff_validation(self):
        self.assert_bad(lambda s: s["tariffs"].append(
            {"meter_id": "nope", "start": 0, "end": 10,
             "price_num": 1, "price_den": 1}))
        self.assert_bad(lambda s: s.update(tariffs=[]))
        self.assert_bad(lambda s: s["tariffs"].append(
            {"meter_id": "m1", "start": 10, "end": 20,
             "price_num": 1, "price_den": 1}))

        def gap(source):
            source["tariffs"] = [
                {"meter_id": "m1", "start": 0, "end": 4,
                 "price_num": 1, "price_den": 1},
                {"meter_id": "m1", "start": 5, "end": 10,
                 "price_num": 1, "price_den": 1},
            ]
        self.assert_bad(gap)

        def overlap(source):
            source["tariffs"] = [
                {"meter_id": "m1", "start": 0, "end": 6,
                 "price_num": 1, "price_den": 1},
                {"meter_id": "m1", "start": 5, "end": 10,
                 "price_num": 1, "price_den": 1},
            ]
        self.assert_bad(overlap)
        self.assert_bad(lambda s: s["tariffs"][0].update(price_den=0))
        self.assert_bad(lambda s: s["tariffs"][0].update(price_num=10**6 + 1))
        self.assert_bad(lambda s: s["tariffs"][0].update(start=5, end=5))

    def test_budget_validation(self):
        budget = {"work_order": "w", "min_energy": 0, "max_energy": 1}
        self.assert_bad(lambda s: s.update(budgets=[budget, budget]))
        self.assert_bad(lambda s: s.update(budgets=[
            {"work_order": "w", "min_energy": 2, "max_energy": 1}]))
        self.assert_bad(lambda s: s.update(budgets=[
            {"work_order": "w", "min_energy": 0, "max_energy": 10**12 + 1}]))
        self.assert_bad(lambda s: s.update(budgets=[
            {"work_order": "w", "min_energy": 0, "max_energy": 1, "x": 1}]))
        self.assert_bad(lambda s: s.update(budgets=[
            {"work_order": " ", "min_energy": 0, "max_energy": 1}]))


# --- attribution acceptance --------------------------------------------------------


class AttributionTest(OracleMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "wh.db"
        self.plan_ids = fixture_warehouse(self.db)
        self.source = json.loads(
            (FIXTURES / "energy_source.json").read_text(encoding="utf-8")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def audit(self, source=None):
        return api.audit(str(self.db), source or self.source, "0" * 64)

    def test_length_ratio_crossing_boundaries_and_conservation(self):
        # Two work orders on the same bar with piece lengths 2 and 4 (ratio
        # 1:2).  The run [2,8) crosses a reading boundary (t=6) and a tariff
        # boundary (t=5); idle energy sits on both sides of the run.
        report = self.audit()
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["interpretations"], 1)
        self.assertEqual(report["warehouse_revision"], 3)
        self.assertEqual(
            report["plans"],
            [{"plan_id": "plan-r1-1", "started_revision": 1,
              "finished_revision": 3, "bars": ["bar-alpha"]}],
        )
        run = report["runs"][0]
        weights = {w["work_order"]: metric_fraction(w, "weight")
                   for w in run["weights"]}
        self.assertEqual(weights, {"wo-frame": Fraction(1, 3),
                                   "wo-brace": Fraction(2, 3)})
        metrics = report_metrics(report)
        self.assertEqual(
            metric_fraction(metrics[("work_order", "wo-frame", "energy")],
                            "min"),
            Fraction(2),
        )
        self.assertEqual(
            metric_fraction(metrics[("work_order", "wo-brace", "energy")],
                            "min"),
            Fraction(4),
        )
        self.assertEqual(
            metric_fraction(metrics[("work_order", "wo-frame", "cost")],
                            "min"),
            Fraction(5),
        )
        self.assertEqual(
            metric_fraction(metrics[("work_order", "wo-brace", "cost")],
                            "min"),
            Fraction(10),
        )
        self.assertEqual(
            metric_fraction(metrics[("idle", "meter-main", "energy")], "min"),
            Fraction(4),
        )
        self.assertEqual(
            metric_fraction(metrics[("idle", "meter-main", "cost")], "min"),
            Fraction(10),
        )
        self.assertEqual(
            metric_fraction(metrics[("total", "energy")], "min"), Fraction(10)
        )
        self.assertEqual(
            metric_fraction(metrics[("total", "cost")], "min"), Fraction(25)
        )
        # Conservation: 2 + 4 + 4 == 10 and 5 + 10 + 10 == 25, checked again
        # per witness by the oracle recheck.
        self.assert_oracle_matches(str(self.db), self.source, report)

    def test_array_order_does_not_change_results(self):
        base = self.audit()
        permuted = json.loads(json.dumps(self.source))
        permuted["meters"][0]["readings"].reverse()
        permuted["tariffs"].reverse()
        permuted["runs"].reverse()
        permuted["budgets"].reverse()
        report = api.audit(str(self.db), permuted, "1" * 64)
        self.assertEqual(
            {k: v for k, v in base.items() if k != "source_sha256"},
            {k: v for k, v in report.items() if k != "source_sha256"},
        )

    def test_full_domain_run_leaves_no_idle(self):
        source = json.loads(json.dumps(self.source))
        source["runs"][0]["start"] = 0
        source["runs"][0]["end"] = 10
        report = self.audit(source)
        metrics = report_metrics(report)
        self.assertEqual(
            metric_fraction(metrics[("idle", "meter-main", "energy")], "min"),
            Fraction(0),
        )
        self.assertEqual(
            metric_fraction(metrics[("total", "energy")], "min"), Fraction(10)
        )
        self.assert_oracle_matches(str(self.db), source, report)


# --- warehouse history semantics ---------------------------------------------------


class HistoryTest(OracleMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "wh.db"

    def tearDown(self):
        self.tmp.cleanup()

    def source_for(self, plan_id, bar_id, run_id="r1"):
        return {
            "meters": [
                {"id": "m1", "modulus": 100, "max_power": 1,
                 "readings": [{"t": 0, "value": 0}, {"t": 10, "value": 5}]}
            ],
            "runs": [
                {"id": run_id, "meter_id": "m1", "plan_id": plan_id,
                 "bar_id": bar_id, "start": 0, "end": 10}
            ],
            "tariffs": [
                {"meter_id": "m1", "start": 0, "end": 10,
                 "price_num": 1, "price_den": 1}
            ],
            "budgets": [],
        }

    def test_consumed_bar_and_reused_remainder_keep_historical_layout(self):
        # Finish a plan whose remainder re-enters stock and is consumed by a
        # later plan; the original finished layout stays the only weight
        # source for the first bar.
        stock_api.initialize(
            str(self.db),
            {"kerf": 1, "reuse_min": 2,
             "bars": [{"id": "b1", "length": 10, "grade": 0}]},
        )
        stock_api.apply_request(str(self.db), {
            "batch_id": "g0", "expected_revision": 0,
            "operations": [
                {"op": "order", "id": "wo-a", "priority": 50,
                 "pieces": [{"id": "a1", "length": 3, "min_grade": 0}]},
                {"op": "order", "id": "wo-b", "priority": 50,
                 "pieces": [{"id": "b1p", "length": 4, "min_grade": 0}]},
            ],
        })
        stock_api.apply_request(str(self.db), {
            "batch_id": "g1", "expected_revision": 1,
            "operations": [{"op": "start"}],
        })
        stock_api.apply_request(str(self.db), {
            "batch_id": "g2", "expected_revision": 2,
            "operations": [{"op": "finish"}],
        })
        # Remainder rem-plan-r1-1-1 (length 2) is back in stock; a second
        # order consumes it through a new plan.
        stock_api.apply_request(str(self.db), {
            "batch_id": "g3", "expected_revision": 3,
            "operations": [
                {"op": "order", "id": "wo-c", "priority": 50,
                 "pieces": [{"id": "c1", "length": 2, "min_grade": 0}]},
            ],
        })
        stock_api.apply_request(str(self.db), {
            "batch_id": "g4", "expected_revision": 4,
            "operations": [{"op": "start"}],
        })
        # The started plan must be refused as a weight source.
        with self.assertRaises(WarehouseStateError):
            api.audit(str(self.db),
                      self.source_for("plan-r4-2", "rem-plan-r1-1-1"),
                      "0" * 64)
        stock_api.apply_request(str(self.db), {
            "batch_id": "g5", "expected_revision": 5,
            "operations": [{"op": "finish"}],
        })
        # The consumed original bar keeps its finished layout weights.
        report = api.audit(str(self.db), self.source_for("plan-r1-1", "b1"),
                           "0" * 64)
        run = report["runs"][0]
        weights = {w["work_order"]: metric_fraction(w, "weight")
                   for w in run["weights"]}
        self.assertEqual(weights, {"wo-a": Fraction(3, 7),
                                   "wo-b": Fraction(4, 7)})
        self.assertEqual(report["warehouse_revision"], 6)
        # The reused remainder's own finished layout is available too.
        report2 = api.audit(
            str(self.db),
            self.source_for("plan-r4-2", "rem-plan-r1-1-1", run_id="r2"),
            "0" * 64,
        )
        weights2 = {w["work_order"]: metric_fraction(w, "weight")
                    for w in report2["runs"][0]["weights"]}
        self.assertEqual(weights2, {"wo-c": Fraction(1)})

    def test_started_and_open_plans_rejected(self):
        stock_api.initialize(
            str(self.db),
            {"kerf": 1, "reuse_min": 100,
             "bars": [{"id": "b1", "length": 10, "grade": 0}]},
        )
        stock_api.apply_request(str(self.db), {
            "batch_id": "g0", "expected_revision": 0,
            "operations": [
                {"op": "order", "id": "wo-a", "priority": 50,
                 "pieces": [{"id": "a1", "length": 3, "min_grade": 0}]},
            ],
        })
        # Open (not started) plan snapshot is not a weight source.
        with self.assertRaises(WarehouseStateError):
            api.audit(str(self.db), self.source_for("plan-r1-1", "b1"),
                      "0" * 64)
        stock_api.apply_request(str(self.db), {
            "batch_id": "g1", "expected_revision": 1,
            "operations": [{"op": "start"}],
        })
        # Started (active) plan is not finished history either.
        with self.assertRaises(WarehouseStateError):
            api.audit(str(self.db), self.source_for("plan-r1-1", "b1"),
                      "0" * 64)
        # Neither is the recomputed open remainder snapshot.
        with self.assertRaises(WarehouseStateError):
            api.audit(str(self.db),
                      self.source_for("plan-r1-1-open", "b1"), "0" * 64)

    def test_unknown_plan_and_bar_rejected(self):
        (plan_id,) = fixture_warehouse(self.db)
        with self.assertRaises(WarehouseStateError):
            api.audit(str(self.db), self.source_for("plan-r9-9", "bar-alpha"),
                      "0" * 64)
        with self.assertRaises(WarehouseStateError):
            api.audit(str(self.db), self.source_for(plan_id, "bar-unknown"),
                      "0" * 64)

    def test_missing_and_foreign_warehouse_rejected(self):
        missing = Path(self.tmp.name) / "missing.db"
        with self.assertRaises(WarehouseStateError):
            api.audit(str(missing), self.source_for("p", "b"), "0" * 64)
        self.assertFalse(missing.exists())
        foreign = Path(self.tmp.name) / "foreign.db"
        conn = sqlite3.connect(str(foreign))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(WarehouseStateError):
            api.audit(str(foreign), self.source_for("p", "b"), "0" * 64)

    def test_budget_work_order_must_appear_in_layouts(self):
        (plan_id,) = fixture_warehouse(self.db)
        source = self.source_for(plan_id, "bar-alpha")
        source["budgets"] = [
            {"work_order": "wo-ghost", "min_energy": 0, "max_energy": 1}
        ]
        with self.assertRaises(AuditError):
            api.audit(str(self.db), source, "0" * 64)


# --- metering uncertainty -------------------------------------------------------


class MeteringTest(OracleMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "wh.db"
        stock_api.initialize(
            str(self.db), {"kerf": 1, "reuse_min": 100, "bars": []}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def audit(self, source, db=None):
        return api.audit(str(db or self.db), source, "0" * 64)

    def base_source(self, readings, max_power, modulus=100):
        last_t = readings[-1]["t"]
        return {
            "meters": [
                {"id": "m1", "modulus": modulus, "max_power": max_power,
                 "readings": readings}
            ],
            "runs": [],
            "tariffs": [
                {"meter_id": "m1", "start": readings[0]["t"], "end": last_t,
                 "price_num": 1, "price_den": 1}
            ],
            "budgets": [],
        }

    def test_wraparound_candidates(self):
        # Readings 10 -> 20, modulus 100, dt 10, max_power 20: the jump of 10
        # is explained by d=10 (no wrap) or d=110 (one wrap); two wraps (210)
        # exceed the power cap.
        source = self.base_source(
            [{"t": 0, "value": 10}, {"t": 10, "value": 20}], 20
        )
        report = self.audit(source)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["interpretations"], 2)
        self.assertEqual(report["intervals"][0]["candidates"], [10, 110])
        metrics = report_metrics(report)
        idle = metrics[("idle", "m1", "energy")]
        self.assertEqual(metric_fraction(idle, "min"), Fraction(10))
        self.assertEqual(idle["min_witness"], [10])
        self.assertEqual(metric_fraction(idle, "max"), Fraction(110))
        self.assertEqual(idle["max_witness"], [110])
        self.assert_oracle_matches(str(self.db), source, report)

    def test_negative_jump_without_wrap_budget_is_infeasible(self):
        # Readings 20 -> 10 need a forward jump of 90 (mod 100), but the power
        # cap of 10 admits no candidate: metering infeasibility, not a format
        # error.
        source = self.base_source(
            [{"t": 0, "value": 20}, {"t": 10, "value": 10}], 1
        )
        report = self.audit(source)
        self.assertEqual(report["status"], "infeasible")
        self.assertEqual(report["interpretations"], 0)
        self.assertIsNone(report["work_orders"])
        self.assertIsNone(report["idle"])
        self.assertIsNone(report["total"])
        self.assertEqual(report["intervals"][0]["candidates"], [])
        self.assert_oracle_matches(str(self.db), source, report)

    def test_distinct_vectors_same_attribution_still_count(self):
        # Interval 2 is fully idle; its two candidate consumptions produce
        # identical work-order attribution but remain distinct
        # interpretations.
        db2 = self.dir / "wh2.db"
        (plan_id,) = make_warehouse(
            db2,
            [([("b1", 10, 0)], [("wo-a", [(3, 0)])])],
        )
        source = self.base_source(
            [{"t": 0, "value": 0}, {"t": 10, "value": 0},
             {"t": 20, "value": 0}],
            10,
        )
        source["runs"] = [
            {"id": "r1", "meter_id": "m1", "plan_id": plan_id,
             "bar_id": "b1", "start": 0, "end": 10}
        ]
        source["budgets"] = [
            {"work_order": "wo-a", "min_energy": 0, "max_energy": 0}
        ]
        report = self.audit(source, db=db2)
        self.assertEqual(report["interpretations"], 2)
        metrics = report_metrics(report)
        wo = metrics[("work_order", "wo-a", "energy")]
        self.assertEqual(metric_fraction(wo, "min"), Fraction(0))
        self.assertEqual(metric_fraction(wo, "max"), Fraction(0))
        self.assertEqual(wo["min_witness"], [0, 0])
        self.assertEqual(wo["max_witness"], [0, 0])
        idle = metrics[("idle", "m1", "energy")]
        self.assertEqual(metric_fraction(idle, "min"), Fraction(0))
        self.assertEqual(idle["min_witness"], [0, 0])
        self.assertEqual(metric_fraction(idle, "max"), Fraction(100))
        self.assertEqual(idle["max_witness"], [0, 100])
        self.assert_oracle_matches(str(db2), source, report)

    def test_zero_price_and_all_idle(self):
        source = self.base_source(
            [{"t": 0, "value": 0}, {"t": 10, "value": 5}], 1
        )
        source["tariffs"][0]["price_num"] = 0
        report = self.audit(source)
        self.assertEqual(report["work_orders"], [])
        metrics = report_metrics(report)
        self.assertEqual(
            metric_fraction(metrics[("idle", "m1", "energy")], "min"),
            Fraction(5),
        )
        self.assertEqual(
            metric_fraction(metrics[("idle", "m1", "cost")], "min"),
            Fraction(0),
        )
        self.assertEqual(
            metric_fraction(metrics[("total", "cost")], "max"), Fraction(0)
        )
        self.assert_oracle_matches(str(self.db), source, report)


# --- joint budget coupling ---------------------------------------------------------


class LinkageTest(OracleMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "wh.db"
        # One finished plan, one bar, two work orders with equal piece
        # lengths: both share the whole run window with weight 1/2.
        (self.plan_id,) = make_warehouse(
            self.db,
            [([("b1", 10, 0)],
              [("wo-x", [(1, 0)]), ("wo-y", [(1, 0)])])],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def coupled_source(self, budgets):
        return {
            "meters": [
                {"id": "m1", "modulus": 100, "max_power": 10,
                 "readings": [{"t": 0, "value": 0}, {"t": 10, "value": 0},
                              {"t": 20, "value": 0}]}
            ],
            "runs": [
                {"id": "r1", "meter_id": "m1", "plan_id": self.plan_id,
                 "bar_id": "b1", "start": 0, "end": 20}
            ],
            "tariffs": [
                {"meter_id": "m1", "start": 0, "end": 20,
                 "price_num": 1, "price_den": 1}
            ],
            "budgets": budgets,
        }

    def test_joint_budgets_exclude_marginal_extremum(self):
        # Unconstrained, wo-x ranges over {0, 50, 100}; pinning wo-y to 50
        # forces d1+d2=100 and thereby wo-x to exactly 50 — the marginal
        # maximum 100 disappears from the joint feasible set.
        free = api.audit(str(self.db), self.coupled_source([]), "0" * 64)
        free_metrics = report_metrics(free)
        self.assertEqual(free["interpretations"], 4)
        self.assertEqual(
            metric_fraction(free_metrics[("work_order", "wo-x", "energy")],
                            "max"),
            Fraction(100),
        )
        source = self.coupled_source(
            [{"work_order": "wo-y", "min_energy": 50, "max_energy": 50}]
        )
        report = api.audit(str(self.db), source, "0" * 64)
        self.assertEqual(report["interpretations"], 2)
        metrics = report_metrics(report)
        wo_x = metrics[("work_order", "wo-x", "energy")]
        self.assertEqual(metric_fraction(wo_x, "min"), Fraction(50))
        self.assertEqual(metric_fraction(wo_x, "max"), Fraction(50))
        self.assertEqual(wo_x["min_witness"], [0, 100])
        self.assertEqual(wo_x["max_witness"], [0, 100])
        self.assert_oracle_matches(str(self.db), source, report)

    def test_budget_correction_moves_other_order_range(self):
        # The feasible set is shared: tightening wo-y's budget changes wo-x's
        # range and witnesses even though wo-x itself never gets a budget.
        def audit_with(bound):
            source = self.coupled_source(
                [{"work_order": "wo-y",
                  "min_energy": bound, "max_energy": bound}]
            )
            return api.audit(str(self.db), source, "0" * 64), source

        report0, source0 = audit_with(0)
        metrics0 = report_metrics(report0)
        wo_x0 = metrics0[("work_order", "wo-x", "energy")]
        self.assertEqual(metric_fraction(wo_x0, "min"), Fraction(0))
        self.assertEqual(metric_fraction(wo_x0, "max"), Fraction(0))
        self.assertEqual(wo_x0["max_witness"], [0, 0])
        self.assert_oracle_matches(str(self.db), source0, report0)

        report50, source50 = audit_with(50)
        metrics50 = report_metrics(report50)
        wo_x50 = metrics50[("work_order", "wo-x", "energy")]
        self.assertEqual(metric_fraction(wo_x50, "min"), Fraction(50))
        self.assertEqual(metric_fraction(wo_x50, "max"), Fraction(50))
        self.assertEqual(wo_x50["max_witness"], [0, 100])
        self.assert_oracle_matches(str(self.db), source50, report50)

        report100, source100 = audit_with(100)
        metrics100 = report_metrics(report100)
        wo_x100 = metrics100[("work_order", "wo-x", "energy")]
        self.assertEqual(metric_fraction(wo_x100, "min"), Fraction(100))
        self.assertEqual(metric_fraction(wo_x100, "max"), Fraction(100))
        self.assertEqual(wo_x100["max_witness"], [100, 100])
        self.assert_oracle_matches(str(self.db), source100, report100)

    def test_individually_feasible_but_jointly_infeasible(self):
        # Each budget alone is satisfiable ((0,0) and (100,100)); their
        # conjunction is not, because both orders see (d1+d2)/2.
        alone_x = self.coupled_source(
            [{"work_order": "wo-x", "min_energy": 0, "max_energy": 0}]
        )
        self.assertEqual(
            api.audit(str(self.db), alone_x, "0" * 64)["interpretations"], 1
        )
        alone_y = self.coupled_source(
            [{"work_order": "wo-y", "min_energy": 100, "max_energy": 100}]
        )
        self.assertEqual(
            api.audit(str(self.db), alone_y, "0" * 64)["interpretations"], 1
        )
        both = self.coupled_source(
            [{"work_order": "wo-x", "min_energy": 0, "max_energy": 0},
             {"work_order": "wo-y", "min_energy": 100, "max_energy": 100}]
        )
        report = api.audit(str(self.db), both, "0" * 64)
        self.assertEqual(report["status"], "infeasible")
        self.assertEqual(report["interpretations"], 0)
        self.assertIsNone(report["work_orders"])
        self.assert_oracle_matches(str(self.db), both, report)

    def test_cross_run_same_work_order_merges(self):
        # A second warehouse where one plan spreads wo-x over two bars:
        # b1 = {wo-x: 1/3, wo-y: 2/3}, b2 = {wo-x: 1}.  Two runs on the same
        # meter merge wo-x across runs with exact rational weights.
        db2 = Path(self.tmp.name) / "wh2.db"
        (plan_id,) = make_warehouse(
            db2,
            [([("b1", 4, 1), ("b2", 4, 0)],
              [("wo-x", [(1, 0), (2, 0)]), ("wo-y", [(2, 1)])])],
        )
        source = self.coupled_source([])
        source["runs"] = [
            {"id": "r1", "meter_id": "m1", "plan_id": plan_id,
             "bar_id": "b1", "start": 0, "end": 10},
            {"id": "r2", "meter_id": "m1", "plan_id": plan_id,
             "bar_id": "b2", "start": 10, "end": 20},
        ]
        report = api.audit(str(db2), source, "0" * 64)
        self.assertEqual(report["interpretations"], 4)
        metrics = report_metrics(report)
        wo_x = metrics[("work_order", "wo-x", "energy")]
        # wo-x sees d1/3 + d2: extremes 0 and 100/3 + 100 = 400/3.
        self.assertEqual(metric_fraction(wo_x, "min"), Fraction(0))
        self.assertEqual(metric_fraction(wo_x, "max"), Fraction(400, 3))
        self.assertEqual(wo_x["max_witness"], [100, 100])
        wo_y = metrics[("work_order", "wo-y", "energy")]
        self.assertEqual(metric_fraction(wo_y, "max"), Fraction(200, 3))
        self.assertEqual(wo_y["max_witness"], [100, 0])
        self.assert_oracle_matches(str(db2), source, report)


# --- randomized oracle comparison -------------------------------------------------


class OracleComparisonTest(OracleMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "wh.db"
        cls.plan_ids = make_warehouse(
            cls.db,
            [([("b1", 10, 0)], [("wo-a", [(3, 0)]), ("wo-b", [(1, 0)])]),
             ([("b2", 10, 0)], [("wo-c", [(2, 0)]), ("wo-d", [(2, 0), (1, 0)])])],
        )
        cls.plan_pairs = [
            (cls.plan_ids[0], "b1", ("wo-a", "wo-b")),
            (cls.plan_ids[1], "b2", ("wo-c", "wo-d")),
        ]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def random_source(self, rng):
        n_meters = rng.choice([1, 1, 2])
        meters = []
        for i in range(n_meters):
            modulus = rng.randint(2, 6)
            max_power = rng.randint(0, 3)
            n_readings = rng.randint(2, 3)
            if max_power:
                max_dt = max(1, (2 * modulus) // max_power)
            else:
                max_dt = 6
            times = [0]
            for _ in range(n_readings - 1):
                times.append(times[-1] + rng.randint(1, min(6, max_dt)))
            meters.append(
                {"id": f"m{i}", "modulus": modulus, "max_power": max_power,
                 "readings": [{"t": t, "value": rng.randint(0, modulus - 1)}
                              for t in times]}
            )
        tariffs = []
        for meter in meters:
            t0 = meter["readings"][0]["t"]
            t1 = meter["readings"][-1]["t"]
            if t1 - t0 < 2 or rng.random() < 0.5:
                cuts = [t0, t1]
            else:
                cuts = [t0, rng.randint(t0 + 1, t1 - 1), t1]
            for a, b in zip(cuts, cuts[1:]):
                tariffs.append(
                    {"meter_id": meter["id"], "start": a, "end": b,
                     "price_num": rng.randint(0, 3),
                     "price_den": rng.randint(1, 3)}
                )
        pairs = list(self.plan_pairs)
        rng.shuffle(pairs)
        windows = {meter["id"]: [] for meter in meters}
        runs = []
        wanted = rng.randint(0, min(2, len(pairs)))
        attempts = 0
        while len(runs) < wanted and attempts < 25:
            attempts += 1
            meter = rng.choice(meters)
            t0 = meter["readings"][0]["t"]
            t1 = meter["readings"][-1]["t"]
            a = rng.randint(t0, t1 - 1)
            b = rng.randint(a + 1, t1)
            if any(a < wb and wa < b for wa, wb in windows[meter["id"]]):
                continue
            windows[meter["id"]].append((a, b))
            plan_id, bar_id, _ = pairs[len(runs)]
            runs.append(
                {"id": f"r{len(runs)}", "meter_id": meter["id"],
                 "plan_id": plan_id, "bar_id": bar_id, "start": a, "end": b}
            )
        present = set()
        for run in runs:
            for plan_id, bar_id, orders in self.plan_pairs:
                if (plan_id, bar_id) == (run["plan_id"], run["bar_id"]):
                    present.update(orders)
        budgets = []
        for order in sorted(present):
            if rng.random() < 0.6:
                lo = rng.randint(0, 6)
                budgets.append(
                    {"work_order": order, "min_energy": lo,
                     "max_energy": lo + rng.randint(0, 6)}
                )
        return {"meters": meters, "runs": runs, "tariffs": tariffs,
                "budgets": budgets}

    def test_random_instances_against_oracle(self):
        import random
        rng = random.Random(20261117)
        for case in range(60):
            source = self.random_source(rng)
            with self.subTest(case=case):
                report = api.audit(str(self.db), source, "0" * 64)
                self.assert_oracle_matches(str(self.db), source, report)
                # The report must be JSON-serializable and deterministic.
                again = api.audit(str(self.db), source, "0" * 64)
                self.assertEqual(
                    json.dumps(report, sort_keys=True),
                    json.dumps(again, sort_keys=True),
                )


# --- CLI ---------------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "wh.db"
        self.source = self.dir / "source.json"
        self.output = self.dir / "report.json"
        fixture_warehouse(self.db)
        shutil.copy(FIXTURES / "energy_source.json", self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return subprocess.run(
            [sys.executable, "-m", "energy_audit.cli", *args],
            capture_output=True, text=True, env=env,
        )

    def run_audit(self, source=None, output=None):
        return self.cli(
            "--warehouse", str(self.db),
            "--source", str(source or self.source),
            "--output", str(output or self.output),
        )

    def test_full_cli_flow_and_determinism(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.output.read_bytes()
        report = json.loads(saved)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["interpretations"], 1)
        self.assertEqual(report["warehouse_revision"], 3)
        import hashlib
        expected_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(report["source_sha256"], expected_sha)
        stdout_report = json.loads(result.stdout)
        self.assertEqual(stdout_report, report)
        # Same state -> identical bytes.
        result2 = self.run_audit()
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertEqual(self.output.read_bytes(), saved)
        # No floats anywhere in the published report.
        def check_no_floats(node):
            if isinstance(node, dict):
                for value in node.values():
                    check_no_floats(value)
            elif isinstance(node, list):
                for value in node:
                    check_no_floats(value)
            else:
                self.assertNotIsInstance(node, float)
        check_no_floats(report)

    def test_database_untouched_by_audit(self):
        import hashlib
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        conn = sqlite3.connect(str(self.db))
        try:
            revision = conn.execute(
                "SELECT value FROM meta WHERE key='revision'"
            ).fetchone()[0]
            self.assertEqual(revision, "3")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("energy_audit", tables)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM plan_history").fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_error_paths_exit_two_and_keep_old_report(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.output.read_bytes()

        # Unknown plan id: contract violation, old report preserved.
        bad = self.dir / "bad_plan.json"
        payload = json.loads(self.source.read_text())
        payload["runs"][0]["plan_id"] = "plan-r9-9"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_audit(source=bad)
        self.assertEqual(result.returncode, 2)
        self.assertIn("warehouse_state", result.stderr)
        self.assertEqual(self.output.read_bytes(), saved)

        # Duplicate JSON keys are rejected by the strict parser.
        dup = self.dir / "dup.json"
        dup.write_text(
            '{"meters": [], "meters": [], "runs": [], "tariffs": [],'
            ' "budgets": []}',
            encoding="utf-8",
        )
        result = self.run_audit(source=dup)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), saved)

        # Boolean passed as an integer.
        bools = self.dir / "bool.json"
        bools.write_text(
            '{"meters": [{"id": "m", "modulus": true, "max_power": 0,'
            ' "readings": [{"t": 0, "value": 0}, {"t": 1, "value": 0}]}],'
            ' "runs": [],'
            ' "tariffs": [{"meter_id": "m", "start": 0, "end": 1,'
            ' "price_num": 1, "price_den": 1}], "budgets": []}',
            encoding="utf-8",
        )
        result = self.run_audit(source=bools)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), saved)

    def test_missing_warehouse_is_not_created(self):
        missing = self.dir / "missing.db"
        result = self.cli(
            "--warehouse", str(missing),
            "--source", str(self.source),
            "--output", str(self.output),
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())
        self.assertFalse(self.output.exists())

    def test_missing_source_and_path_collisions(self):
        result = self.cli(
            "--warehouse", str(self.db),
            "--source", str(self.dir / "nope.json"),
            "--output", str(self.output),
        )
        self.assertEqual(result.returncode, 2)
        # Output must not overwrite the source.
        result = self.cli(
            "--warehouse", str(self.db),
            "--source", str(self.source),
            "--output", str(self.source),
        )
        self.assertEqual(result.returncode, 2)
        # Output must not overwrite the warehouse.
        result = self.cli(
            "--warehouse", str(self.db),
            "--source", str(self.source),
            "--output", str(self.db),
        )
        self.assertEqual(result.returncode, 2)
        # Symlink outputs are refused.
        link = self.dir / "link.json"
        link.symlink_to(self.source)
        result = self.run_audit(output=link)
        self.assertEqual(result.returncode, 2)


# --- concurrency ---------------------------------------------------------------------


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "wh.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_finish_reads_single_snapshot(self):
        # Plan A is finished; a worker finishes plan B while audits run.
        (plan_a,) = make_warehouse(
            self.db, [([("b1", 10, 0)], [("wo-a", [(3, 0)])])]
        )
        self.assertEqual(plan_a, "plan-r1-1")
        source = {
            "meters": [
                {"id": "m1", "modulus": 100, "max_power": 1,
                 "readings": [{"t": 0, "value": 0}, {"t": 10, "value": 5}]}
            ],
            "runs": [
                {"id": "r1", "meter_id": "m1", "plan_id": "plan-r1-1",
                 "bar_id": "b1", "start": 0, "end": 5},
                {"id": "r2", "meter_id": "m1", "plan_id": "plan-r4-2",
                 "bar_id": "b2", "start": 5, "end": 10},
            ],
            "tariffs": [
                {"meter_id": "m1", "start": 0, "end": 10,
                 "price_num": 1, "price_den": 1}
            ],
            "budgets": [],
        }
        errors = []

        def worker():
            try:
                stock_api.apply_request(str(self.db), {
                    "batch_id": "w0", "expected_revision": 3,
                    "operations": [
                        {"op": "receive",
                         "bar": {"id": "b2", "length": 10, "grade": 0}},
                        {"op": "order", "id": "wo-b", "priority": 50,
                         "pieces": [{"id": "bp", "length": 2,
                                     "min_grade": 0}]},
                    ],
                })
                stock_api.apply_request(str(self.db), {
                    "batch_id": "w1", "expected_revision": 4,
                    "operations": [{"op": "start"}],
                })
                stock_api.apply_request(str(self.db), {
                    "batch_id": "w2", "expected_revision": 5,
                    "operations": [{"op": "finish"}],
                })
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        seen_finished_b = 0
        for _ in range(200):
            try:
                report = api.audit(str(self.db), source, "0" * 64)
            except WarehouseStateError:
                # plan-r4-2 not finished yet: a consistent earlier snapshot.
                continue
            seen_finished_b += 1
            plans = {p["plan_id"]: p for p in report["plans"]}
            # Revision and finished identity come from one snapshot.
            self.assertGreaterEqual(report["warehouse_revision"], 6)
            self.assertEqual(plans["plan-r4-2"]["finished_revision"], 6)
            self.assertEqual(plans["plan-r1-1"]["finished_revision"], 3)
            weights = {
                w["work_order"]: metric_fraction(w, "weight")
                for w in report["runs"][1]["weights"]
            }
            self.assertEqual(weights, {"wo-b": Fraction(1)})
        thread.join()
        self.assertEqual(errors, [])
        self.assertGreater(seen_finished_b, 0)
        # After the dust settles the final state is consistent.
        final = api.audit(str(self.db), source, "0" * 64)
        self.assertEqual(final["warehouse_revision"], 6)
        self.assertEqual(final["status"], "feasible")


if __name__ == "__main__":
    unittest.main()

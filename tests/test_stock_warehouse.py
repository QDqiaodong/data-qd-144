"""Acceptance tests for the offline bounded cutting-plan warehouse."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cutting_stock import stock_api
from cutting_stock.common import strict_json_bytes
from cutting_stock.stock_store import connect

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class WarehouseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "wh.db"

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, payload):
        return stock_api.initialize(str(self.db), payload)

    def apply(self, batch_id, revision, operations):
        return stock_api.apply_request(
            str(self.db),
            {
                "batch_id": batch_id,
                "expected_revision": revision,
                "operations": operations,
            },
        )

    def report(self):
        return stock_api.build_report(str(self.db))

    # --- exactness versus FFD ---------------------------------------------

    def test_global_priority_beats_per_order_ffd(self):
        # Three length-10 bars.  A per-order greedy (FFD-style) planer accepts
        # two small orders first (each fitting comfortably), which together
        # consume all three bars, so the big order is deferred with total
        # priority 90.  The exact joint optimum accepts only the big order for
        # priority 100 instead.
        self.init({
            "kerf": 1,
            "reuse_min": 100,
            "bars": [
                {"id": "b1", "length": 10, "grade": 0},
                {"id": "b2", "length": 10, "grade": 0},
                {"id": "b3", "length": 10, "grade": 0},
            ],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "big", "priority": 100, "pieces": [
                {"id": "x1", "length": 6, "min_grade": 0},
                {"id": "x2", "length": 6, "min_grade": 0},
                {"id": "x3", "length": 6, "min_grade": 0},
            ]},
            {"op": "order", "id": "small-a", "priority": 45, "pieces": [
                {"id": "y1", "length": 7, "min_grade": 0}]},
            {"op": "order", "id": "small-b", "priority": 45, "pieces": [
                {"id": "z1", "length": 7, "min_grade": 0}]},
        ])
        report = self.report()
        plan = report["open_plan"]
        self.assertEqual(plan["accepted"], ["big"])
        self.assertEqual(plan["totals"]["priority_sum"], 100)
        states = {o["id"]: o for o in report["work_orders"]}
        self.assertEqual(states["big"]["state"], "accepted")
        self.assertEqual(states["small-a"]["state"], "deferred")
        self.assertIn("quantity", states["small-a"]["deferral_reasons"])
        self.assertEqual(plan["totals"]["bars_used"], 3)

    def test_grade_and_length_substitution_global_choice(self):
        # A local first choice on the long grade-2 bar (placing the grade-0
        # piece g2 there) leaves the grade-2 piece without a suitable bar and
        # the high-priority order infeasible.  The global optimum assigns g1
        # to the high-grade bar and pairs g2 with the other order's piece on
        # the grade-0 bar, accepting both orders for a higher priority sum.
        self.init({
            "kerf": 1,
            "reuse_min": 100,
            "bars": [
                {"id": "b1", "length": 12, "grade": 0},
                {"id": "b2", "length": 10, "grade": 2},
            ],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o-hi", "priority": 80, "pieces": [
                {"id": "g1", "length": 9, "min_grade": 2},
                {"id": "g2", "length": 3, "min_grade": 0},
            ]},
            {"op": "order", "id": "o-lo", "priority": 20, "pieces": [
                {"id": "l1", "length": 8, "min_grade": 0}]},
        ])
        plan = self.report()["open_plan"]
        self.assertEqual(plan["accepted"], ["o-hi", "o-lo"])
        self.assertEqual(plan["totals"]["priority_sum"], 100)
        layout = {l["bar_id"]: l for l in plan["layouts"]}
        # g1 alone must take the high-grade bar; b1 carries g2 + l1.
        self.assertEqual(
            [p["piece_id"] for p in layout["b2"]["pieces"]], ["g1"]
        )
        self.assertEqual(
            sorted(p["piece_id"] for p in layout["b1"]["pieces"]),
            ["g2", "l1"],
        )

    def test_deferral_reasons_grade_length_quantity(self):
        self.init({
            "kerf": 1,
            "reuse_min": 100,
            "bars": [
                {"id": "b1", "length": 5, "grade": 0},
                {"id": "b2", "length": 5, "grade": 0},
            ],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "grade-blocked", "priority": 10, "pieces": [
                {"id": "p1", "length": 2, "min_grade": 5}]},
            {"op": "order", "id": "too-long", "priority": 10, "pieces": [
                {"id": "p2", "length": 9, "min_grade": 0}]},
            {"op": "order", "id": "ok", "priority": 1, "pieces": [
                {"id": "p3", "length": 5, "min_grade": 0}]},
        ])
        states = {o["id"]: o for o in self.report()["work_orders"]}
        self.assertIn("grade", states["grade-blocked"]["deferral_reasons"])
        self.assertIn("length", states["too-long"]["deferral_reasons"])
        self.assertEqual(states["ok"]["state"], "accepted")

    # --- kerf and reuse boundaries -----------------------------------------

    def test_kerf_first_piece_free_then_each_cut(self):
        self.init({
            "kerf": 2,
            "reuse_min": 100,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 1, "pieces": [
                {"id": "p1", "length": 4, "min_grade": 0},
                {"id": "p2", "length": 4, "min_grade": 0},
            ]},
        ])
        layout = self.report()["open_plan"]["layouts"][0]
        # 4 + kerf 2 + 4 = 10, no remainder.
        self.assertEqual((layout["used"], layout["kerf_loss"],
                          layout["planned_remainder"]), (10, 2, 0))
        # A third piece of 4 cannot share: 4+2+4+2+4 > 10, whole order defers.
        self.apply("b2", 1, [
            {"op": "amend", "id": "o1", "priority": 1, "pieces": [
                {"id": "q1", "length": 4, "min_grade": 0},
                {"id": "q2", "length": 4, "min_grade": 0},
                {"id": "q3", "length": 4, "min_grade": 0},
            ]},
        ])
        states = {o["id"]: o for o in self.report()["work_orders"]}
        self.assertEqual(states["o1"]["state"], "deferred")

    def test_kerf_zero_boundary(self):
        self.init({
            "kerf": 0,
            "reuse_min": 100,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 1, "pieces": [
                {"id": "p" + str(i), "length": 2, "min_grade": 0}
                for i in range(5)
            ]},
        ])
        plan = self.report()["open_plan"]
        self.assertEqual(plan["totals"]["bars_used"], 1)
        self.assertEqual(plan["layouts"][0]["kerf_loss"], 0)

    def test_exact_reuse_min_becomes_stock_only_after_finish(self):
        self.init({
            "kerf": 1,
            "reuse_min": 4,
            "bars": [{"id": "b1", "length": 10, "grade": 3}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 10, "pieces": [
                {"id": "p1", "length": 6, "min_grade": 2}]},
        ])
        plan = self.report()["open_plan"]
        layout = plan["layouts"][0]
        self.assertEqual(layout["planned_remainder"], 4)
        self.assertTrue(layout["planned_reusable"])
        # The planned remainder is not a usable bar yet.
        bar_ids = {b["id"] for b in self.report()["bars"]}
        self.assertNotIn("rem-plan-r1-1-1", bar_ids)
        self.apply("s1", 1, [{"op": "start"}])
        # Even while started, the remainder cannot serve a new order.
        self.apply("b2", 2, [
            {"op": "order", "id": "o2", "priority": 1, "pieces": [
                {"id": "p2", "length": 4, "min_grade": 0}]},
        ])
        states = {o["id"]: o for o in self.report()["work_orders"]}
        self.assertEqual(states["o2"]["state"], "deferred")
        self.apply("f1", 3, [{"op": "finish"}])
        report = self.report()
        rem = [b for b in report["bars"] if b["origin"] == "remainder"]
        self.assertEqual(len(rem), 1)
        self.assertEqual((rem[0]["length"], rem[0]["grade"]), (4, 3))
        # o2 was deferred while the plan was running; after finish it is still
        # an open order and must now be replanned against the carried-over
        # grade-3 remainder (the only available bar).
        states = {o["id"]: o for o in report["work_orders"]}
        self.assertEqual(states["o2"]["state"], "accepted")
        plan = report["open_plan"]
        self.assertEqual(plan["accepted"], ["o2"])
        self.assertEqual(plan["layouts"][0]["bar_id"], rem[0]["id"])

    def test_sub_reuse_min_is_waste(self):
        self.init({
            "kerf": 1,
            "reuse_min": 5,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 10, "pieces": [
                {"id": "p1", "length": 6, "min_grade": 0}]},
        ])
        self.apply("s1", 1, [{"op": "start"}])
        self.apply("f1", 2, [{"op": "finish"}])
        report = self.report()
        self.assertEqual(report["waste"][0]["length"], 4)
        self.assertFalse(
            any(b["origin"] == "remainder" for b in report["bars"])
        )

    # --- lifecycle, freeze and revision ------------------------------------

    def test_start_freezes_all_accepted_orders_and_bars_atomically(self):
        self.init({
            "kerf": 1, "reuse_min": 100,
            "bars": [
                {"id": "b1", "length": 10, "grade": 0},
                {"id": "b2", "length": 10, "grade": 0},
            ],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 5, "pieces": [
                {"id": "p1", "length": 4, "min_grade": 0}]},
            {"op": "order", "id": "o2", "priority": 6, "pieces": [
                {"id": "p2", "length": 4, "min_grade": 0}]},
        ])
        before = self.report()
        self.apply("s1", 1, [{"op": "start"}])
        after = self.report()
        # Open snapshot still exists for the remaining open orders (none yet).
        self.assertEqual(after["open_plan"]["accepted"], [])
        self.assertIsNotNone(after["active_plan"])
        self.assertEqual(
            set(after["active_plan"]["accepted"]), {"o1", "o2"}
        )
        for order in after["work_orders"]:
            if order["id"] in {"o1", "o2"}:
                self.assertEqual(order["state"], "started")
        allocated_bars = {
            layout["bar_id"]
            for layout in after["active_plan"]["layouts"]
        }
        for bar in after["bars"]:
            if bar["id"] in allocated_bars:
                self.assertEqual(bar["status"], "occupied")
        self.assertEqual(after["revision"], before["revision"] + 1)
        # Withdraw of a started order is refused.
        with self.assertRaises(stock_api.StockError):
            self.apply("w1", 2, [{"op": "withdraw", "id": "o1"}])

    def test_stale_revision_and_partial_finish_rejected(self):
        self.init({
            "kerf": 1, "reuse_min": 100,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 5, "pieces": [
                {"id": "p1", "length": 4, "min_grade": 0}]},
        ])
        with self.assertRaises(stock_api.RevisionError):
            self.apply("old", 0, [
                {"op": "order", "id": "o2", "priority": 1, "pieces": [
                    {"id": "p2", "length": 1, "min_grade": 0}]},
            ])
        self.apply("s1", 1, [{"op": "start"}])
        # finish cannot be mixed with another operation.
        with self.assertRaises(stock_api.StockError):
            self.apply("mix", 2, [
                {"op": "finish"},
                {"op": "withdraw", "id": "o1"},
            ])
        # A second start is refused.
        with self.assertRaises(stock_api.StockError):
            self.apply("s2", 2, [{"op": "start"}])
        self.apply("f1", 2, [{"op": "finish"}])
        # Finishing again has no active plan.
        with self.assertRaises(stock_api.StockError):
            self.apply("f2", 3, [{"op": "finish"}])

    def test_receive_and_corrections_during_started_period(self):
        # One bar: only the high-priority order is accepted and started; the
        # other order stays open and deferred.
        self.init({
            "kerf": 1, "reuse_min": 100,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 9, "pieces": [
                {"id": "p1", "length": 6, "min_grade": 0}]},
            {"op": "order", "id": "o2", "priority": 1, "pieces": [
                {"id": "p2", "length": 8, "min_grade": 0}]},
        ])
        self.assertEqual(self.report()["open_plan"]["accepted"], ["o1"])
        self.apply("s1", 1, [{"op": "start"}])
        occupied = {
            b["id"] for b in self.report()["bars"]
            if b["status"] == "occupied"
        }
        self.assertEqual(occupied, {"b1"})
        # Receiving an existing/occupied id is rejected.
        with self.assertRaises(stock_api.StockError):
            self.apply("bad", 2, [
                {"op": "receive", "bar": {
                    "id": "b1", "length": 5, "grade": 0}}])
        # Before the new bar arrives, the open order cannot use the occupied
        # one and stays deferred.
        self.assertEqual(
            self.report()["open_plan"]["accepted"], [])
        self.apply("r1", 2, [
            {"op": "receive", "bar": {
                "id": "b4", "length": 9, "grade": 0}}])
        plan = self.report()["open_plan"]
        # o2 can now use the newly received bar; the occupied bar is excluded.
        self.assertEqual(plan["accepted"], ["o2"])
        self.assertEqual(plan["layouts"][0]["bar_id"], "b4")
        for layout in plan["layouts"]:
            self.assertNotIn(layout["bar_id"], occupied)
        # A second started plan cannot be opened while one is active.
        with self.assertRaises(stock_api.StockError):
            self.apply("s2", 3, [{"op": "start"}])

    def test_amend_and_withdraw_open_orders_replan(self):
        self.init({
            "kerf": 1, "reuse_min": 100,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "o1", "priority": 5, "pieces": [
                {"id": "p1", "length": 9, "min_grade": 0}]},
            {"op": "order", "id": "o2", "priority": 4, "pieces": [
                {"id": "p2", "length": 5, "min_grade": 0}]},
        ])
        self.assertEqual(self.report()["open_plan"]["accepted"], ["o1"])
        # Shrinking o1 lets o2 fit instead? both still cannot share one bar;
        # make o1 withdrawable and confirm replanning.
        self.apply("w1", 1, [{"op": "withdraw", "id": "o1"}])
        plan = self.report()["open_plan"]
        self.assertEqual(plan["accepted"], ["o2"])
        self.assertEqual(
            {o["id"]: o["state"] for o in self.report()["work_orders"]},
            {"o1": "withdrawn", "o2": "accepted"},
        )
        # Reusing a withdrawn order id or its piece ids is forbidden.
        with self.assertRaises(stock_api.StockError):
            self.apply("b2", 2, [
                {"op": "order", "id": "o1", "priority": 1, "pieces": [
                    {"id": "p9", "length": 1, "min_grade": 0}]}])
        with self.assertRaises(stock_api.StockError):
            self.apply("b3", 2, [
                {"op": "order", "id": "o3", "priority": 1, "pieces": [
                    {"id": "p2", "length": 1, "min_grade": 0}]}])

    def test_idempotent_retry_and_payload_conflict(self):
        self.init({
            "kerf": 0, "reuse_min": 100,
            "bars": [{"id": "b1", "length": 5, "grade": 0}],
        })
        request = {
            "batch_id": "dup",
            "expected_revision": 0,
            "operations": [
                {"op": "order", "id": "o1", "priority": 1, "pieces": [
                    {"id": "p1", "length": 2, "min_grade": 0}]}
            ],
        }
        first = stock_api.apply_request(str(self.db), request)
        second = stock_api.apply_request(str(self.db), dict(request))
        self.assertEqual(first, second)
        self.assertEqual(self.report()["revision"], 1)
        conflict = dict(request)
        conflict["operations"][0]["priority"] = 99
        with self.assertRaises(stock_api.IdempotentConflict):
            stock_api.apply_request(str(self.db), conflict)
        self.assertEqual(self.report()["revision"], 1)

    # --- validation ---------------------------------------------------------

    def test_strict_validation(self):
        # init only creates a new database.
        self.init({"kerf": 0, "reuse_min": 1, "bars": []})
        with self.assertRaises(stock_api.stock_store.DatabaseExistsError):
            self.init({"kerf": 0, "reuse_min": 1, "bars": []})
        bad_inits = [
            {"kerf": -1, "reuse_min": 1, "bars": []},
            {"kerf": 10**6 + 1, "reuse_min": 1, "bars": []},
            {"kerf": 0, "reuse_min": 0, "bars": []},
            {"kerf": True, "reuse_min": 1, "bars": []},
            {"kerf": 0, "reuse_min": 1, "bars": [
                {"id": "b", "length": 1, "grade": 10}]},
            {"kerf": 0, "reuse_min": 1, "bars": [
                {"id": "b", "length": 0, "grade": 0}]},
            {"kerf": 0, "reuse_min": 1, "bars": [
                {"id": " b", "length": 1, "grade": 0}]},
            {"kerf": 0, "reuse_min": 1, "bars": [
                {"id": "b", "length": 1, "grade": 0},
                {"id": "b", "length": 1, "grade": 0}]},
            {"kerf": 0, "reuse_min": 1, "unknown": 1, "bars": []},
        ]
        for payload in bad_inits:
            with self.subTest(payload=payload):
                with self.assertRaises((stock_api.StockError, ValueError)):
                    stock_api.initialize(
                        str(Path(self.tmp.name) / ("x" + str(id(payload)))),
                        payload,
                    )
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [{"op": "frobnicate"}])
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "start"},
                {"op": "receive", "bar": {
                    "id": "b9", "length": 1, "grade": 0}}])
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "order", "id": "o1", "priority": 101, "pieces": [
                    {"id": "p1", "length": 1, "min_grade": 0}]}])
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "order", "id": "o1", "priority": 1, "pieces": []}])
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "order", "id": "o1", "priority": 1, "pieces": [
                    {"id": "p1", "length": True, "min_grade": 0}]}])

    def test_duplicate_json_keys_rejected(self):
        raw = b'{"kerf":0,"kerf":1,"reuse_min":1,"bars":[]}'
        with self.assertRaises(ValueError):
            strict_json_bytes(raw)
        with self.assertRaises(ValueError):
            strict_json_bytes(b'{"kerf":NaN,"reuse_min":1,"bars":[]}')

    def test_live_bar_and_order_caps(self):
        self.init({
            "kerf": 0, "reuse_min": 100,
            "bars": [
                {"id": "b" + str(i), "length": 5, "grade": 0}
                for i in range(12)
            ],
        })
        with self.assertRaises(stock_api.StockError):
            self.apply("r", 0, [
                {"op": "receive", "bar": {
                    "id": "b12", "length": 1, "grade": 0}}])
        orders = [
            {"op": "order", "id": "o" + str(i), "priority": 1, "pieces": [
                {"id": "p" + str(i), "length": 1, "min_grade": 0}]}
            for i in range(9)
        ]
        with self.assertRaises(stock_api.StockError):
            self.apply("o", 0, orders)
        # Nothing changed after either failed batch.
        self.assertEqual(self.report()["revision"], 0)

    def test_batch_target_appears_once(self):
        self.init({
            "kerf": 0, "reuse_min": 100,
            "bars": [{"id": "b1", "length": 5, "grade": 0}],
        })
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "withdraw", "id": "o1"},
                {"op": "withdraw", "id": "o1"},
            ])
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "receive", "bar": {
                    "id": "r1", "length": 1, "grade": 0}},
                {"op": "receive", "bar": {
                    "id": "r1", "length": 2, "grade": 0}},
            ])

    def test_failed_batch_rolls_back_everything(self):
        self.init({
            "kerf": 0, "reuse_min": 100,
            "bars": [{"id": "b1", "length": 5, "grade": 0}],
        })
        # A valid op followed by an invalid op in the same batch: no facts,
        # allocations or revision changes may survive.
        with self.assertRaises(stock_api.StockError):
            self.apply("x", 0, [
                {"op": "order", "id": "o1", "priority": 1, "pieces": [
                    {"id": "p1", "length": 1, "min_grade": 0}]},
                {"op": "order", "id": "o1", "priority": 1, "pieces": [
                    {"id": "p2", "length": 1, "min_grade": 0}]},
            ])
        report = self.report()
        self.assertEqual(report["revision"], 0)
        self.assertEqual(report["work_orders"], [])
        self.assertEqual(report["bars"][0]["status"], "available")
        conn = connect(str(self.db))
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM work_orders").fetchone()["n"],
                0,
            )
        finally:
            conn.close()

    def test_order_independence_from_input_line_order(self):
        self.init({
            "kerf": 1, "reuse_min": 100,
            "bars": [
                {"id": "b1", "length": 10, "grade": 0},
                {"id": "b2", "length": 10, "grade": 0},
            ],
        })
        ops_a = [
            {"op": "order", "id": "o1", "priority": 5, "pieces": [
                {"id": "p1", "length": 6, "min_grade": 0}]},
            {"op": "order", "id": "o2", "priority": 7, "pieces": [
                {"id": "p2", "length": 6, "min_grade": 0}]},
        ]
        self.apply("a", 0, ops_a)
        report_a = json.dumps(
            self.report(), sort_keys=True, ensure_ascii=False,
            allow_nan=False, separators=(",", ":"),
        )
        # Rebuild with reversed operation order.
        self.tearDown()
        self.setUp()
        self.init({
            "kerf": 1, "reuse_min": 100,
            "bars": [
                {"id": "b1", "length": 10, "grade": 0},
                {"id": "b2", "length": 10, "grade": 0},
            ],
        })
        self.apply("a", 0, list(reversed(ops_a)))
        report_b = json.dumps(
            self.report(), sort_keys=True, ensure_ascii=False,
            allow_nan=False, separators=(",", ":"),
        )
        self.assertEqual(report_a, report_b)

    def test_report_byte_deterministic_and_sorted(self):
        self.init({
            "kerf": 1, "reuse_min": 3,
            "bars": [
                {"id": "中", "length": 10, "grade": 0},
                {"id": "a", "length": 10, "grade": 0},
            ],
        })
        self.apply("b1", 0, [
            {"op": "order", "id": "单", "priority": 3, "pieces": [
                {"id": "p1", "length": 4, "min_grade": 0}]},
            {"op": "order", "id": "z", "priority": 3, "pieces": [
                {"id": "p2", "length": 4, "min_grade": 0}]},
        ])
        first = json.dumps(
            self.report(), sort_keys=True, ensure_ascii=False,
            allow_nan=False, indent=2,
        )
        second = json.dumps(
            self.report(), sort_keys=True, ensure_ascii=False,
            allow_nan=False, indent=2,
        )
        self.assertEqual(first, second)
        report = self.report()
        bar_ids = [b["id"] for b in report["bars"]]
        self.assertEqual(
            bar_ids, sorted(bar_ids, key=lambda s: s.encode("utf-8"))
        )

    def test_empty_warehouse_report(self):
        self.init({"kerf": 0, "reuse_min": 1, "bars": []})
        report = self.report()
        self.assertEqual(report["revision"], 0)
        self.assertEqual(report["work_orders"], [])
        self.assertEqual(report["bars"], [])
        self.assertEqual(report["open_plan"]["accepted"], [])
        self.assertEqual(report["totals"]["priority_sum"], 0)


class CliWarehouse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "wh.db"
        self.source = self.dir / "source.json"
        self.request = self.dir / "request.json"
        self.output = self.dir / "report.json"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return subprocess.run(
            [sys.executable, "-m", "cutting_stock.stock_cli", *args],
            capture_output=True, text=True, env=env,
        )

    def test_full_cli_flow(self):
        write_json(self.source, {
            "kerf": 1, "reuse_min": 4,
            "bars": [{"id": "b1", "length": 10, "grade": 0}],
        })
        r = self.cli("stock-init", "--db", str(self.db),
                     "--source", str(self.source))
        self.assertEqual(r.returncode, 0, r.stderr)
        # Re-init must fail without touching the database.
        before = self.db.read_bytes()
        r = self.cli("stock-init", "--db", str(self.db),
                     "--source", str(self.source))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.db.read_bytes(), before)
        write_json(self.request, {
            "batch_id": "b1", "expected_revision": 0,
            "operations": [
                {"op": "order", "id": "o1", "priority": 9, "pieces": [
                    {"id": "p1", "length": 6, "min_grade": 0}]}
            ],
        })
        r = self.cli("stock-apply", "--db", str(self.db),
                     "--request", str(self.request))
        self.assertEqual(r.returncode, 0, r.stderr)
        # Exact retry of the same request file returns the first response.
        first = r.stdout
        r2 = self.cli("stock-apply", "--db", str(self.db),
                      "--request", str(self.request))
        self.assertEqual(r2.stdout, first)
        r = self.cli("stock-report", "--db", str(self.db),
                     "--output", str(self.output))
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = self.output.read_bytes()
        self.cli("stock-report", "--db", str(self.db),
                 "--output", str(self.output))
        self.assertEqual(self.output.read_bytes(), saved)
        report = json.loads(saved)
        self.assertEqual(report["revision"], 1)
        self.assertEqual(report["open_plan"]["accepted"], ["o1"])

    def test_cli_rejects_duplicate_keys_and_bad_revision(self):
        write_json(self.source, {
            "kerf": 0, "reuse_min": 1, "bars": []})
        self.assertEqual(
            self.cli("stock-init", "--db", str(self.db),
                     "--source", str(self.source)).returncode, 0
        )
        (self.dir / "dup.json").write_text(
            '{"batch_id":"x","expected_revision":0,'
            '"operations":[],"operations":[]}', encoding="utf-8"
        )
        r = self.cli("stock-apply", "--db", str(self.db),
                     "--request", str(self.dir / "dup.json"))
        self.assertEqual(r.returncode, 2)
        write_json(self.request, {
            "batch_id": "x", "expected_revision": 5,
            "operations": [
                {"op": "withdraw", "id": "nope"}],
        })
        r = self.cli("stock-apply", "--db", str(self.db),
                     "--request", str(self.request))
        self.assertEqual(r.returncode, 2)
        self.assertIn("revision_conflict", r.stderr)


if __name__ == "__main__":
    unittest.main()

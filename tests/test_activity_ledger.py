"""Acceptance, oracle and CLI tests for the auditable activity ledger.

The oracle below is an independent implementation of the contract: it keeps
its own generation-stamped revision histories, selects the highest revision
known at a generation, resolves each distinct ``event_time`` with a plain BFS
over the undirected link graph and recomputes the period groups, then compares
them against the package under test.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from activity_ledger import api, identity, store
from activity_ledger.validate import (
    ActivityError,
    GenerationConflict,
    IdempotentConflict,
    LedgerStateError,
    UnknownCloseError,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def event(event_id, revision, op="put", account="A", time=0, amount=0):
    if op == "delete":
        return {"event_id": event_id, "revision": revision, "op": op,
                "account_id": None, "event_time": None, "amount_minor": None}
    return {"event_id": event_id, "revision": revision, "op": op,
            "account_id": account, "event_time": time,
            "amount_minor": amount}


def link(link_id, revision, op="put", left="A", right="B",
         valid_from=0, valid_to=10**12):
    if op == "delete":
        return {"link_id": link_id, "revision": revision, "op": op,
                "left_account": None, "right_account": None,
                "valid_from": None, "valid_to": None}
    return {"link_id": link_id, "revision": revision, "op": op,
            "left_account": left, "right_account": right,
            "valid_from": valid_from, "valid_to": valid_to}


def apply(db, request_id, expected, events=(), links=()):
    return api.apply_request(
        db,
        {"request_id": request_id, "expected_generation": expected,
         "events": list(events), "links": list(links)},
    )


def close(db, close_id, expected, start, end):
    return api.close_period(
        db, {"close_id": close_id, "expected_generation": expected,
             "start": start, "end": end},
    )


# --- independent oracle -------------------------------------------------------


class OracleLedger:
    """Generation-stamped histories with a from-scratch resolver."""

    def __init__(self):
        self.events = {}   # event_id -> [(generation, record)]
        self.links = {}    # link_id -> [(generation, record)]

    def apply(self, generation, events=(), links=()):
        for record in events:
            self.events.setdefault(record["event_id"], []).append(
                (generation, record)
            )
        for record in links:
            self.links.setdefault(record["link_id"], []).append(
                (generation, record)
            )

    @staticmethod
    def _select(revisions, generation):
        chosen = None
        for known, record in revisions:
            if known <= generation:
                chosen = record
        return chosen

    def active_events(self, generation):
        active = {}
        for event_id, revisions in self.events.items():
            chosen = self._select(revisions, generation)
            if chosen is None or chosen["op"] == "delete":
                continue
            active[event_id] = chosen
        return active

    def effective_links(self, generation):
        links = []
        for link_id, revisions in self.links.items():
            chosen = self._select(revisions, generation)
            if chosen is None or chosen["op"] == "delete":
                continue
            links.append(chosen)
        return links

    @staticmethod
    def canonical(account, instant, links):
        adjacency = {}
        for row in links:
            if row["valid_from"] <= instant < row["valid_to"]:
                adjacency.setdefault(row["left_account"], set()).add(
                    row["right_account"]
                )
                adjacency.setdefault(row["right_account"], set()).add(
                    row["left_account"]
                )
        if account not in adjacency:
            return account
        seen = {account}
        queue = [account]
        while queue:
            node = queue.pop()
            for neighbour in adjacency.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        return min(seen, key=lambda value: value.encode("utf-8"))

    def groups(self, generation, start, end):
        links = self.effective_links(generation)
        grouped = {}
        for event_id, record in self.active_events(generation).items():
            if not start <= record["event_time"] < end:
                continue
            account = self.canonical(
                record["account_id"], record["event_time"], links
            )
            grouped.setdefault(account, []).append(
                (event_id, record["amount_minor"])
            )
        rows = []
        for account in sorted(grouped, key=lambda value: value.encode("utf-8")):
            members = sorted(grouped[account],
                             key=lambda pair: pair[0].encode("utf-8"))
            rows.append(
                {
                    "account_id": account,
                    "event_ids": [pair[0] for pair in members],
                    "event_count": len(members),
                    "amount_minor": sum(pair[1] for pair in members),
                }
            )
        return rows


def assert_matches_oracle(testcase, report_rows, oracle_rows):
    testcase.assertEqual(
        [(row["account_id"], row["event_ids"], row["event_count"],
          row["amount_minor"]) for row in report_rows],
        [(row["account_id"], row["event_ids"], row["event_count"],
          row["amount_minor"]) for row in oracle_rows],
    )


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "activity.db")
        api.initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, report, key="restated"):
        return [
            (row["account_id"], row["event_ids"], row["event_count"],
             row["amount_minor"])
            for row in report[key]
        ]

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "activity_ledger.cli", *args],
            cwd=str(ROOT), capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )


# --- init ---------------------------------------------------------------------


class InitTest(BaseCase):
    def test_init_creates_only_new_database_at_generation_zero(self):
        with self.assertRaises(ActivityError):
            api.initialize(self.db)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='generation'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "0")

    def test_init_failure_leaves_no_partial_database(self):
        target = str(Path(self.tmp.name) / "fresh.db")
        original = store.initialize_schema
        calls = {"n": 0}

        def boom(conn):
            calls["n"] += 1
            raise RuntimeError("injected init failure")

        store.initialize_schema = boom
        try:
            with self.assertRaises(RuntimeError):
                api.initialize(target)
        finally:
            store.initialize_schema = original
        self.assertFalse(os.path.exists(target))
        self.assertEqual(api.initialize(target)["generation"], 0)


# --- strict validation --------------------------------------------------------


class ValidationTest(BaseCase):
    def base_request(self, **overrides):
        request = {
            "request_id": "R1",
            "expected_generation": 0,
            "events": [event("e1", 1)],
            "links": [link("l1", 1)],
        }
        request.update(overrides)
        return request

    def test_unknown_and_missing_fields_rejected(self):
        bad = self.base_request()
        bad["extra"] = 1
        with self.assertRaises(ActivityError):
            api.apply_request(self.db, bad)
        bad = self.base_request()
        del bad["links"]
        with self.assertRaises(ActivityError):
            api.apply_request(self.db, bad)
        bad = self.base_request(events=[{**event("e1", 1), "extra": 0}])
        with self.assertRaises(ActivityError):
            api.apply_request(self.db, bad)

    def test_booleans_are_not_integers(self):
        for field, value in (("revision", True), ("event_time", False),
                             ("amount_minor", True)):
            bad_event = event("e1", 1)
            bad_event[field] = value
            with self.assertRaises(ActivityError):
                api.apply_request(
                    self.db, self.base_request(events=[bad_event], links=[])
                )
        bad = self.base_request(
            expected_generation=True, events=[event("e1", 1)], links=[]
        )
        with self.assertRaises(ActivityError):
            api.apply_request(self.db, bad)

    def test_identifier_and_range_rules(self):
        for bad_id in ("", " a", "a ", "a\n", "x" * 129, 5):
            with self.assertRaises(ActivityError):
                api.apply_request(
                    self.db,
                    self.base_request(events=[event(bad_id, 1)], links=[]),
                )
        for field, value in (("event_time", 10**12 + 1),
                             ("amount_minor", 10**12 + 1),
                             ("revision", 0)):
            bad_event = event("e1", 1)
            bad_event[field] = value
            with self.assertRaises(ActivityError):
                api.apply_request(
                    self.db, self.base_request(events=[bad_event], links=[])
                )

    def test_duplicate_identifiers_and_self_links(self):
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db,
                self.base_request(
                    events=[event("e1", 1), event("e1", 2)], links=[]
                ),
            )
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db,
                self.base_request(
                    events=[], links=[link("l1", 1), link("l1", 2)]
                ),
            )
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db,
                self.base_request(
                    events=[], links=[link("l1", 1, left="A", right="A")]
                ),
            )

    def test_link_interval_and_delete_shape(self):
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db,
                self.base_request(
                    events=[],
                    links=[link("l1", 1, valid_from=10, valid_to=10)],
                ),
            )
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db,
                self.base_request(
                    events=[],
                    links=[link("l1", 1, valid_from=11, valid_to=10)],
                ),
            )
        bad_delete = link("l1", 1, op="delete")
        bad_delete["left_account"] = "A"
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db, self.base_request(events=[], links=[bad_delete])
            )

    def test_empty_and_oversized_batches(self):
        with self.assertRaises(ActivityError):
            api.apply_request(self.db, self.base_request(events=[], links=[]))
        events = [event(f"e{i}", 1) for i in range(5001)]
        with self.assertRaises(ActivityError):
            api.apply_request(
                self.db, self.base_request(events=events, links=[])
            )

    def test_duplicate_json_keys_rejected(self):
        from cutting_stock.common import strict_json_bytes
        with self.assertRaises(ValueError):
            strict_json_bytes(b'{"request_id":"R1","request_id":"R2"}')

    def test_close_request_shape(self):
        for bad in (
            {"close_id": "C", "expected_generation": 0, "start": 5, "end": 5},
            {"close_id": "C", "expected_generation": 0, "start": 6, "end": 5},
            {"close_id": "", "expected_generation": 0, "start": 0, "end": 5},
            {"close_id": "C", "expected_generation": False,
             "start": 0, "end": 5},
            {"close_id": "C", "expected_generation": 0, "start": 0},
        ):
            with self.assertRaises(ActivityError):
                api.close_period(self.db, bad)

    def test_missing_database_is_a_state_error(self):
        missing = str(Path(self.tmp.name) / "missing.db")
        with self.assertRaises(LedgerStateError):
            api.apply_request(
                missing,
                {"request_id": "R", "expected_generation": 0,
                 "events": [event("e1", 1)], "links": []},
            )
        with self.assertRaises(LedgerStateError):
            api.close_period(
                missing,
                {"close_id": "C", "expected_generation": 0,
                 "start": 0, "end": 1},
            )
        with self.assertRaises(LedgerStateError):
            api.build_report(missing, "C")
        self.assertFalse(os.path.exists(missing))

    def test_corrupt_and_foreign_databases_are_state_errors(self):
        garbage = Path(self.tmp.name) / "garbage.db"
        garbage.write_bytes(b"not a database at all")
        with self.assertRaises(LedgerStateError):
            api.apply_request(
                str(garbage),
                {"request_id": "R", "expected_generation": 0,
                 "events": [event("e1", 1)], "links": []},
            )
        foreign = str(Path(self.tmp.name) / "foreign.db")
        conn = sqlite3.connect(foreign)
        try:
            conn.execute("CREATE TABLE unrelated(x)")
            conn.commit()
        finally:
            conn.close()
        for call in (
            lambda: api.apply_request(
                foreign,
                {"request_id": "R", "expected_generation": 0,
                 "events": [event("e1", 1)], "links": []},
            ),
            lambda: api.close_period(
                foreign,
                {"close_id": "C", "expected_generation": 0,
                 "start": 0, "end": 1},
            ),
            lambda: api.build_report(foreign, "C"),
        ):
            with self.assertRaises(LedgerStateError):
                call()
        with self.assertRaises(ActivityError):
            api.initialize(foreign)


# --- apply idempotency / revisions / generations ------------------------------


class ApplyTest(BaseCase):
    def test_replay_returns_original_generation_after_advance(self):
        first = apply(self.db, "R1", 0, [event("e1", 1)], [])
        self.assertEqual(first["generation"], 1)
        apply(self.db, "R2", 1, [event("e2", 1)], [])
        replay = apply(self.db, "R1", 0, [event("e1", 1)], [])
        self.assertEqual(replay, first)
        # Replay is checked before expected_generation: stale 0 still replayed.
        self.assertEqual(replay["generation"], 1)

    def test_reordered_arrays_are_the_same_canonical_request(self):
        first = apply(
            self.db, "R1", 0,
            [event("e1", 1), event("e2", 1)],
            [link("l1", 1), link("l2", 1)],
        )
        replay = apply(
            self.db, "R1", 0,
            [event("e2", 1), event("e1", 1)],
            [link("l2", 1), link("l1", 1)],
        )
        self.assertEqual(replay, first)

    def test_conflicting_reuse_of_request_id(self):
        apply(self.db, "R1", 0, [event("e1", 1)], [])
        with self.assertRaises(IdempotentConflict):
            apply(self.db, "R1", 0, [event("e1", 1, amount=9)], [])

    def test_revision_must_be_consecutive_and_delete_needs_history(self):
        apply(self.db, "R1", 0, [event("e1", 1)], [])
        with self.assertRaises(ActivityError):  # skip to 3
            apply(self.db, "R2", 1, [event("e1", 3)], [])
        with self.assertRaises(ActivityError):  # unknown delete
            apply(self.db, "R2", 1, [], [link("lX", 1, op="delete")])
        with self.assertRaises(ActivityError):  # unknown event delete
            apply(self.db, "R2", 1, [event("eX", 1, op="delete")], [])
        # A failed batch must not advance the generation or leave facts.
        conn = sqlite3.connect(self.db)
        try:
            generation = conn.execute(
                "SELECT value FROM meta WHERE key='generation'"
            ).fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((generation, events), ("1", 1))
        apply(self.db, "R2", 1, [event("e1", 2, amount=5)], [])
        apply(self.db, "R3", 2, [event("e1", 3, op="delete")], [])
        with self.assertRaises(ActivityError):  # nothing left to delete
            apply(self.db, "R4", 3, [event("e1", 4, op="delete")], [])
        # -- and a re-put after deletion starts at the next revision, not 1.
        with self.assertRaises(ActivityError):
            apply(self.db, "R4", 3, [event("e1", 1)], [])
        apply(self.db, "R4", 3, [event("e1", 4, amount=7)], [])

    def test_link_delete_is_knowledge_time_retraction(self):
        apply(self.db, "R1", 0, [event("e1", 1, account="B", time=5)],
              [link("l1", 1, left="A", right="B", valid_from=0,
                    valid_to=100)])
        close(self.db, "C1", 1, 0, 10)
        apply(self.db, "R2", 1, [], [link("l1", 2, op="delete")])
        report = api.build_report(self.db, "C1")
        # as_booked was resolved at generation 1 and still sees the link.
        self.assertEqual(self.rows(report, "as_booked"),
                         [("A", ["e1"], 1, 0)])
        # Restated at generation 2 sees the retraction (B has no edge).
        self.assertEqual(self.rows(report, "restated"),
                         [("B", ["e1"], 1, 0)])
        # The old put revision is still stored, not erased.
        apply(self.db, "R3", 2, [], [link("l1", 3, left="A", right="B",
                                          valid_from=0, valid_to=100)])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e1"], 1, 0)])

    def test_generation_conflict_and_concurrency(self):
        apply(self.db, "R1", 0, [event("e1", 1)], [])
        with self.assertRaises(GenerationConflict):
            apply(self.db, "R2", 0, [event("e2", 1)], [])
        outcomes = []

        def worker(index):
            try:
                apply(self.db, f"T{index}", 1,
                      [event(f"t{index}", 1)], [])
                outcomes.append("ok")
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(outcomes.count("GenerationConflict"), 3, outcomes)

    def test_transaction_failure_rolls_back_all_facts(self):
        apply(self.db, "R1", 0, [event("e1", 1)], [link("l1", 1)])
        original = store.set_generation

        def boom(conn, value):
            raise RuntimeError("injected failure after inserts")

        store.set_generation = boom
        try:
            with self.assertRaises(RuntimeError):
                apply(self.db, "R2", 1, [event("e2", 1)],
                      [link("l2", 1)])
        finally:
            store.set_generation = original
        conn = sqlite3.connect(self.db)
        try:
            generation = conn.execute(
                "SELECT value FROM meta WHERE key='generation'"
            ).fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            receipts = conn.execute(
                "SELECT COUNT(*) FROM requests"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((generation, events, links, receipts),
                         ("1", 1, 1, 1))
        # R2 was not consumed and applies cleanly now.
        self.assertEqual(apply(self.db, "R2", 1, [event("e2", 1)],
                               [link("l2", 1)])["generation"], 2)


# --- acceptance: valid time boundaries ----------------------------------------


class ValidTimeTest(BaseCase):
    def setUp(self):
        super().setUp()
        apply(
            self.db, "R1", 0,
            [
                event("e9", 1, account="B", time=9, amount=100),
                event("e10", 1, account="B", time=10, amount=200),
                event("e29", 1, account="B", time=29, amount=300),
                event("e30", 1, account="B", time=30, amount=400),
            ],
            [link("l-ab", 1, left="A", right="B", valid_from=10,
                  valid_to=30)],
        )

    def test_boundaries_half_open_interval(self):
        report = close(self.db, "C1", 1, 0, 100)
        self.assertEqual(
            report["as_booked"],
            [
                {"account_id": "A", "event_ids": ["e10", "e29"],
                 "event_count": 2, "amount_minor": 500},
                {"account_id": "B", "event_ids": ["e30", "e9"],
                 "event_count": 2, "amount_minor": 500},
            ],
        )

    def test_later_link_revision_only_moves_10_to_19(self):
        close(self.db, "C1", 1, 0, 100)
        frozen = api.build_report(self.db, "C1")["as_booked"]
        apply(self.db, "R2", 1, [],
              [link("l-ab", 2, left="A", right="B", valid_from=20,
                    valid_to=30)])
        report = api.build_report(self.db, "C1")
        self.assertEqual(report["as_booked"], frozen)
        self.assertEqual(report["as_booked_generation"], 1)
        self.assertEqual(report["restated_generation"], 2)
        self.assertEqual(
            report["restated"],
            [
                {"account_id": "A", "event_ids": ["e29"],
                 "event_count": 1, "amount_minor": 300},
                {"account_id": "B",
                 "event_ids": ["e10", "e30", "e9"],
                 "event_count": 3, "amount_minor": 700},
            ],
        )
        self.assertEqual(
            report["changes"],
            [
                {"account_id": "A",
                 "as_booked_event_ids": ["e10", "e29"],
                 "restated_event_ids": ["e29"],
                 "as_booked_event_count": 2,
                 "as_booked_amount_minor": 500,
                 "restated_event_count": 1,
                 "restated_amount_minor": 300,
                 "delta_event_count": -1, "delta_amount_minor": -200},
                {"account_id": "B",
                 "as_booked_event_ids": ["e30", "e9"],
                 "restated_event_ids": ["e10", "e30", "e9"],
                 "as_booked_event_count": 2,
                 "as_booked_amount_minor": 500,
                 "restated_event_count": 3,
                 "restated_amount_minor": 700,
                 "delta_event_count": 1, "delta_amount_minor": 200},
            ],
        )


# --- acceptance: graph split and merge ----------------------------------------


class SplitMergeTest(BaseCase):
    def setUp(self):
        super().setUp()
        apply(
            self.db, "R1", 0,
            [
                event("e-a", 1, account="A", time=5, amount=10),
                event("e-b", 1, account="B", time=5, amount=20),
                event("e-c", 1, account="C", time=5, amount=30),
            ],
            [
                link("l-ab", 1, left="A", right="B"),
                link("l-bc", 1, left="B", right="C"),
            ],
        )
        close(self.db, "C1", 1, 0, 10)

    def test_transitive_component_uses_byte_minimum(self):
        report = close(self.db, "C2", 1, 0, 10)
        self.assertEqual(
            report["as_booked"],
            [{"account_id": "A", "event_ids": ["e-a", "e-b", "e-c"],
              "event_count": 3, "amount_minor": 60}],
        )

    def test_retract_ab_splits_component(self):
        apply(self.db, "R2", 1, [], [link("l-ab", 2, op="delete")])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "as_booked"),
                         [("A", ["e-a", "e-b", "e-c"], 3, 60)])
        self.assertEqual(
            report["restated"],
            [
                {"account_id": "A", "event_ids": ["e-a"],
                 "event_count": 1, "amount_minor": 10},
                {"account_id": "B", "event_ids": ["e-b", "e-c"],
                 "event_count": 2, "amount_minor": 50},
            ],
        )
        # Restoring a fresh relation re-merges under the new canonical ID.
        apply(self.db, "R3", 2, [], [
            link("l-ab", 3, left="A", right="B")
        ])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e-a", "e-b", "e-c"], 3, 60)])

    def test_retract_bc_splits_c_into_its_own_component(self):
        apply(self.db, "R2", 1, [], [link("l-bc", 2, op="delete")])
        report = api.build_report(self.db, "C1")
        self.assertEqual(
            self.rows(report, "restated"),
            [("A", ["e-a", "e-b"], 2, 30), ("C", ["e-c"], 1, 30)],
        )

    def test_cycle_is_one_component(self):
        apply(self.db, "R2", 1, [], [link("l-ca", 1, left="C", right="A")])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e-a", "e-b", "e-c"], 3, 60)])


# --- acceptance: projection linkage -------------------------------------------


class ProjectionLinkageTest(BaseCase):
    def setUp(self):
        super().setUp()
        apply(
            self.db, "R1", 0,
            [
                event("e-a", 1, account="A", time=5, amount=10),
                event("e-b", 1, account="B", time=5, amount=20),
            ],
            [link("l-ab", 1, left="A", right="B")],
        )
        close(self.db, "C1", 1, 0, 10)

    def test_membership_swap_with_equal_totals_is_reported(self):
        # Replace e-b by a different event with the same amount: count and
        # amount are unchanged, yet membership changed and must be visible.
        apply(self.db, "R2", 1,
              [event("e-b", 2, op="delete"),
               event("e-z", 1, account="B", time=5, amount=20)], [])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e-a", "e-z"], 2, 30)])
        self.assertEqual(report["changes"], [
            {"account_id": "A",
             "as_booked_event_ids": ["e-a", "e-b"],
             "restated_event_ids": ["e-a", "e-z"],
             "as_booked_event_count": 2,
             "as_booked_amount_minor": 30,
             "restated_event_count": 2,
             "restated_amount_minor": 30,
             "delta_event_count": 0, "delta_amount_minor": 0},
        ])

    def test_amount_correction_moves_old_group_delta(self):
        apply(self.db, "R2", 1,
              [event("e-b", 2, account="B", time=5, amount=25)], [])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "as_booked"),
                         [("A", ["e-a", "e-b"], 2, 30)])
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e-a", "e-b"], 2, 35)])
        self.assertEqual(report["changes"], [
            {"account_id": "A",
             "as_booked_event_ids": ["e-a", "e-b"],
             "restated_event_ids": ["e-a", "e-b"],
             "as_booked_event_count": 2,
             "as_booked_amount_minor": 30, "restated_event_count": 2,
             "restated_amount_minor": 35, "delta_event_count": 0,
             "delta_amount_minor": 5},
        ])

    def test_move_event_across_accounts_shows_both_sides(self):
        apply(self.db, "R2", 1,
              [event("e-b", 2, account="C", time=5, amount=20)], [])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e-a"], 1, 10), ("C", ["e-b"], 1, 20)])
        self.assertEqual(
            [(row["account_id"], row["delta_event_count"],
              row["delta_amount_minor"]) for row in report["changes"]],
            [("A", -1, -20), ("C", 1, 20)],
        )

    def test_delete_event_reduces_old_group(self):
        apply(self.db, "R2", 1, [event("e-b", 2, op="delete")], [])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e-a"], 1, 10)])
        self.assertEqual(report["changes"], [
            {"account_id": "A",
             "as_booked_event_ids": ["e-a", "e-b"],
             "restated_event_ids": ["e-a"],
             "as_booked_event_count": 2,
             "as_booked_amount_minor": 30, "restated_event_count": 1,
             "restated_amount_minor": 10, "delta_event_count": -1,
             "delta_amount_minor": -20},
        ])
        self.assertEqual(report["changes_totals"],
                         {"delta_event_count": -1,
                          "delta_amount_minor": -20})

    def test_period_boundary_excludes_events(self):
        apply(self.db, "R2", 1,
              [event("e-late", 1, account="B", time=10, amount=99)], [])
        report = api.build_report(self.db, "C1")
        self.assertEqual(report["restated_totals"]["event_count"], 2)
        self.assertNotIn("e-late", json.dumps(report["restated"]))

    def test_canonical_recompute_after_relationship_change(self):
        # A new link pulls in a byte-smaller account, so the surviving group's
        # canonical key changes from "A" to "0" once the original edge goes;
        # membership-driven re-keying, not just appending a new group.
        apply(self.db, "R2", 1, [],
              [link("l-b0", 1, left="0", right="B")])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "as_booked"),
                         [("A", ["e-a", "e-b"], 2, 30)])
        self.assertEqual(self.rows(report, "restated"),
                         [("0", ["e-a", "e-b"], 2, 30)])
        apply(self.db, "R3", 2, [], [link("l-ab", 2, op="delete")])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "restated"),
                         [("0", ["e-b"], 1, 20), ("A", ["e-a"], 1, 10)])
        self.assertEqual(
            [(row["account_id"], row["as_booked_event_ids"],
              row["restated_event_ids"], row["delta_event_count"],
              row["delta_amount_minor"]) for row in report["changes"]],
            [("0", [], ["e-b"], 1, 20),
             ("A", ["e-a", "e-b"], ["e-a"], -1, -20)],
        )


# --- knowledge-time hindsight and oracle comparison ---------------------------


class IdentityStressTest(unittest.TestCase):
    def test_segment_tree_matches_per_instant_bfs(self):
        import random
        rng = random.Random(42)
        for trial in range(20):
            accounts = [f"acct-{i}" for i in range(6)]
            links, events = [], {}
            for i in range(rng.randrange(1, 12)):
                left, right = rng.sample(accounts, 2)
                start = rng.randrange(0, 20)
                end = start + rng.randrange(1, 12)
                links.append({
                    "link_id": f"l{i}", "left_account": left,
                    "right_account": right, "valid_from": start,
                    "valid_to": end,
                })
            for i in range(rng.randrange(1, 15)):
                events[f"e{i}"] = {
                    "event_id": f"e{i}", "account_id": rng.choice(accounts),
                    "event_time": rng.randrange(0, 32), "amount_minor": 0,
                }
            fast = identity.canonical_accounts(events, links)
            for event in events.values():
                self.assertEqual(
                    fast[event["event_id"]],
                    OracleLedger.canonical(
                        event["account_id"], event["event_time"], links
                    ),
                )



class KnowledgeTimeTest(BaseCase):
    def test_link_added_after_close_only_changes_restated(self):
        apply(self.db, "R1", 0,
              [event("e1", 1, account="B", time=5, amount=1)], [])
        close(self.db, "C1", 1, 0, 10)
        apply(self.db, "R2", 1, [],
              [link("l1", 1, left="A", right="B")])
        report = api.build_report(self.db, "C1")
        self.assertEqual(self.rows(report, "as_booked"),
                         [("B", ["e1"], 1, 1)])
        self.assertEqual(self.rows(report, "restated"),
                         [("A", ["e1"], 1, 1)])

    def test_close_replay_returns_original_result(self):
        apply(self.db, "R1", 0,
              [event("e1", 1, account="A", time=5, amount=1)], [])
        first = close(self.db, "C1", 1, 0, 10)
        apply(self.db, "R2", 1,
              [event("e2", 1, account="A", time=6, amount=2)], [])
        replay = close(self.db, "C1", 1, 0, 10)
        self.assertEqual(replay, first)
        with self.assertRaises(IdempotentConflict):
            close(self.db, "C1", 2, 0, 10)
        with self.assertRaises(GenerationConflict):
            close(self.db, "C2", 1, 0, 10)

    def test_empty_report(self):
        close(self.db, "C1", 0, 0, 10)
        report = api.build_report(self.db, "C1")
        self.assertEqual(report["as_booked"], [])
        self.assertEqual(report["restated"], [])
        self.assertEqual(report["changes"], [])
        self.assertEqual(report["as_booked_totals"],
                         {"event_count": 0, "amount_minor": 0})

    def test_unknown_close(self):
        with self.assertRaises(UnknownCloseError):
            api.build_report(self.db, "nope")

    def test_oracle_comparison_random_history(self):
        import random
        rng = random.Random(20261117)
        oracle = OracleLedger()
        generation = 0
        event_state = {}
        link_state = {}
        for step in range(30):
            batch_events, batch_links = [], []
            touched_events, touched_links = set(), set()
            for _ in range(rng.randint(0, 3)):
                event_id = rng.choice([f"e{i}" for i in range(8)])
                if event_id in touched_events:
                    continue
                touched_events.add(event_id)
                revision, last_op = event_state.get(event_id, (0, None))
                revision += 1
                if last_op == "put" and rng.random() < 0.25:
                    record = event(event_id, revision, op="delete")
                    last_op = "delete"
                else:
                    record = event(
                        event_id, revision,
                        account=rng.choice(["A", "B", "C", "D"]),
                        time=rng.randrange(0, 20),
                        amount=rng.randrange(0, 100),
                    )
                    last_op = "put"
                event_state[event_id] = (revision, last_op)
                batch_events.append(record)
            for _ in range(rng.randint(0, 2)):
                link_id = rng.choice([f"l{i}" for i in range(5)])
                if link_id in touched_links:
                    continue
                touched_links.add(link_id)
                revision, last_op = link_state.get(link_id, (0, None))
                revision += 1
                if last_op == "put" and rng.random() < 0.3:
                    record = link(link_id, revision, op="delete")
                    last_op = "delete"
                else:
                    left, right = rng.sample(["A", "B", "C", "D"], 2)
                    start = rng.randrange(0, 15)
                    end = start + rng.randrange(1, 10)
                    record = link(link_id, revision, left=left, right=right,
                                  valid_from=start, valid_to=end)
                    last_op = "put"
                link_state[link_id] = (revision, last_op)
                batch_links.append(record)
            if not batch_events and not batch_links:
                continue
            apply(self.db, f"R{step}", generation, batch_events, batch_links)
            generation += 1
            oracle.apply(generation, batch_events, batch_links)
            if step % 5 == 0:
                start, end = 0, 20
                close(self.db, f"C{step}", generation, start, end)
                report = api.build_report(self.db, f"C{step}")
                assert_matches_oracle(
                    self, report["as_booked"],
                    oracle.groups(generation, start, end),
                )
        close(self.db, "C-final", generation, 0, 20)
        report = api.build_report(self.db, "C-final")
        assert_matches_oracle(
            self, report["restated"], oracle.groups(generation, 0, 20)
        )
        for row in report["restated"]:
            self.assertEqual(
                row["amount_minor"],
                sum(
                    event_row["amount_minor"]
                    for event_row in oracle.active_events(generation).values()
                    if event_row["event_id"] in row["event_ids"]
                ),
            )


# --- report determinism, read-only and publication ----------------------------


class FixtureTest(BaseCase):
    def test_fixture_boundary_scenario(self):
        request = json.loads(
            (FIXTURES / "activity_request.json").read_text()
        )
        close_request = json.loads(
            (FIXTURES / "activity_close.json").read_text()
        )
        applied = api.apply_request(self.db, request)
        self.assertEqual(applied["generation"], 1)
        closed = api.close_period(self.db, close_request)
        self.assertEqual(
            closed["as_booked"],
            [
                {"account_id": "acct-A", "event_ids": ["evt-t10", "evt-t29"],
                 "event_count": 2, "amount_minor": 500},
                {"account_id": "acct-B", "event_ids": ["evt-t30", "evt-t9"],
                 "event_count": 2, "amount_minor": 500},
            ],
        )
        # Re-narrow the link in a later generation; only 10..19 is restated.
        api.apply_request(self.db, {
            "request_id": "ACT-0002", "expected_generation": 1, "events": [],
            "links": [{"link_id": "lnk-ab", "revision": 2, "op": "put",
                       "left_account": "acct-A", "right_account": "acct-B",
                       "valid_from": 20, "valid_to": 30}],
        })
        report = api.build_report(self.db, "CLOSE-0001")
        self.assertEqual(report["as_booked"], closed["as_booked"])
        self.assertEqual(
            report["restated"],
            [
                {"account_id": "acct-A", "event_ids": ["evt-t29"],
                 "event_count": 1, "amount_minor": 300},
                {"account_id": "acct-B",
                 "event_ids": ["evt-t10", "evt-t30", "evt-t9"],
                 "event_count": 3, "amount_minor": 700},
            ],
        )


class ReportPublicationTest(BaseCase):
    def setUp(self):
        super().setUp()
        apply(self.db, "R1", 0,
              [event("e-b", 1, account="B", time=5, amount=2),
               event("e-a", 1, account="A", time=5, amount=1)], [])
        close(self.db, "C1", 1, 0, 10)

    def test_report_is_deterministic_and_read_only(self):
        with open(self.db, "rb") as stream:
            before = stream.read()
        first = api.build_report(self.db, "C1")
        second = api.build_report(self.db, "C1")
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        with open(self.db, "rb") as stream:
            after = stream.read()
        self.assertEqual(before, after)
        accounts = [row["account_id"] for row in first["restated"]]
        self.assertEqual(accounts,
                         sorted(accounts, key=lambda s: s.encode("utf-8")))

    def test_frozen_as_booked_bytes_survive_later_revisions(self):
        conn = sqlite3.connect(self.db)
        try:
            before = conn.execute(
                "SELECT as_booked_json FROM closes WHERE close_id='C1'"
            ).fetchone()[0]
        finally:
            conn.close()
        apply(self.db, "R2", 1,
              [event("e-a", 2, account="Z", time=5, amount=7)], [])
        apply(self.db, "R3", 2,
              [event("e-b", 2, op="delete")],
              [link("l1", 1, left="A", right="B")])
        conn = sqlite3.connect(self.db)
        try:
            after = conn.execute(
                "SELECT as_booked_json FROM closes WHERE close_id='C1'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, after)
        self.assertEqual(api.build_report(self.db, "C1")["as_booked"],
                         json.loads(before))

    def test_report_against_missing_database_creates_nothing(self):
        missing = str(Path(self.tmp.name) / "absent.db")
        with self.assertRaises(LedgerStateError):
            api.build_report(missing, "C1")
        self.assertFalse(os.path.exists(missing))

    def test_cli_publishes_atomically_and_protects_symlinks(self):
        output = Path(self.tmp.name) / "report.json"
        result = self.cli("activity-report", "--db", self.db,
                          "--close-id", "C1", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        published = output.read_bytes()
        payload = json.loads(published)
        self.assertEqual(payload["close_id"], "C1")

        link_path = Path(self.tmp.name) / "link.json"
        link_path.symlink_to(output)
        result = self.cli("activity-report", "--db", self.db,
                          "--close-id", "C1", "--output", str(link_path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(output.read_bytes(), published)

    def test_cli_failure_keeps_previous_report_bytes(self):
        output = Path(self.tmp.name) / "report.json"
        self.assertEqual(
            self.cli("activity-report", "--db", self.db, "--close-id", "C1",
                     "--output", str(output)).returncode, 0
        )
        before = output.read_bytes()
        result = self.cli("activity-report", "--db", self.db,
                          "--close-id", "missing", "--output", str(output))
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown_close", result.stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_cli_full_chain(self):
        db = str(Path(self.tmp.name) / "cli.db")
        request = Path(self.tmp.name) / "req.json"
        request.write_text(json.dumps({
            "request_id": "R1", "expected_generation": 0,
            "events": [event("e1", 1, account="B", time=5, amount=3)],
            "links": [link("l1", 1, left="A", right="B")],
        }))
        close_request = Path(self.tmp.name) / "close.json"
        close_request.write_text(json.dumps({
            "close_id": "C1", "expected_generation": 1, "start": 0, "end": 10,
        }))
        self.assertEqual(
            self.cli("activity-init", "--db", db).returncode, 0
        )
        applied = self.cli("activity-apply", "--db", db,
                           "--request", str(request))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["generation"], 1)
        closed = self.cli("activity-close", "--db", db,
                          "--request", str(close_request))
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(
            json.loads(closed.stdout)["close_generation"], 1
        )
        self.assertEqual(
            self.cli("activity-init", "--db", db).returncode, 2
        )
        self.assertEqual(
            self.cli("activity-apply", "--db", db,
                     "--request", str(close_request)).returncode, 2
        )


if __name__ == "__main__":
    unittest.main()

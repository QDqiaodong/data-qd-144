"""Testable API: initialization, bitemporal apply, close and restated report.

Layered responsibilities:

* :mod:`activity_ledger.validate` – strict request shape, identifier, range and
  duplicate/bool/unknown-field rejection.  It never opens a database.
* :mod:`activity_ledger.identity` – knowledge-time revision selection
  (``link_id``/``event_id`` highest revision at a generation, delete retracts)
  and valid-time graph connectivity per ``event_time``.
* :mod:`activity_ledger.projection` – period filtering, grouping by canonical
  account, summary rows and the ``restated - as_booked`` diff.
* :mod:`activity_ledger.store` – SQLite schema and accessors.

Write boundaries
----------------
Every successful apply runs in one ``BEGIN IMMEDIATE`` transaction: all event
and link revisions, the idempotency receipt and the single generation bump are
committed together, or nothing at all.  A close freezes its ``as_booked`` rows
and the close receipt in one transaction but never advances the generation.
A failure anywhere rolls the transaction back, so no partial fact survives.

Idempotency
-----------
The request fingerprint is taken over the *canonical* (array-order independent)
request.  A retry of the same canonical request returns the original response
verbatim — including the original generation — even after later applies moved
the current generation forward.  The same identifier with different arguments
is a conflict.  Replay is resolved *before* the ``expected_generation`` check
so a legitimate retry never fails on a stale generation.
"""
import json
import os
import sqlite3

from cutting_stock.common import ident

from . import projection, store
from .validate import (
    ActivityError,
    GenerationConflict,
    IdempotentConflict,
    LedgerStateError,
    UnknownCloseError,
    canonical_json,
    fingerprint,
    parse_apply_request,
    parse_close_request,
)


def _load_revisions(conn):
    return store.load_event_revisions(conn), store.load_link_revisions(conn)


def _check_schema(conn):
    try:
        has_schema = store.has_schema(conn)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(
            f"activity database is not readable: {exc}"
        ) from exc
    if not has_schema:
        raise LedgerStateError("database has no activity-ledger schema")


def _require_existing(db_path):
    if not store.database_exists(db_path):
        raise LedgerStateError(
            "activity database does not exist: " + str(db_path)
        )


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def initialize(db_path):
    """Create a brand-new activity database with generation 0."""
    if os.path.exists(db_path):
        raise ActivityError(
            "database already exists; init only creates new databases"
        )
    try:
        conn = store.connect(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(
            f"cannot create activity database: {exc}"
        ) from exc
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if existing:
                raise ActivityError(
                    "database already exists; init only creates new databases"
                )
            store.initialize_schema(conn)
            conn.commit()
            committed = True
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
        if not committed:
            # A failed init must not leave a half-created database behind.
            _remove_empty_database(db_path)
    return {"status": "initialized", "generation": 0,
            "schema_version": store.SCHEMA_VERSION}


def _remove_empty_database(db_path):
    if not os.path.exists(db_path):
        return
    try:
        if os.path.getsize(db_path) == 0:
            os.unlink(db_path)
            return
        conn = sqlite3.connect(str(db_path))
        try:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            conn.close()
        if tables == 0:
            os.unlink(db_path)
    except (OSError, sqlite3.DatabaseError):
        pass


# --------------------------------------------------------------------------
# history validation for apply
# --------------------------------------------------------------------------


def _check_event_revisions(request_events, history):
    for revision in request_events:
        event_id = revision["event_id"]
        prior = history.get(event_id, [])
        expected = (prior[-1]["revision"] if prior else 0) + 1
        if revision["revision"] != expected:
            raise ActivityError(
                f"event {event_id} revision {revision['revision']} is not "
                f"the next consecutive revision {expected}"
            )
        if revision["op"] == "delete":
            if not prior:
                raise ActivityError(
                    "event " + event_id + " has no prior revision to delete"
                )
            if prior[-1]["op"] != "put":
                raise ActivityError(
                    "event " + event_id + " has no undeleted prior revision"
                )


def _check_link_revisions(request_links, history):
    for revision in request_links:
        link_id = revision["link_id"]
        prior = history.get(link_id, [])
        expected = (prior[-1]["revision"] if prior else 0) + 1
        if revision["revision"] != expected:
            raise ActivityError(
                f"link {link_id} revision {revision['revision']} is not "
                f"the next consecutive revision {expected}"
            )
        if revision["op"] == "delete":
            if not prior:
                raise ActivityError(
                    "link " + link_id + " has no prior revision to delete"
                )
            if prior[-1]["op"] != "put":
                raise ActivityError(
                    "link " + link_id + " has no undeleted prior revision"
                )


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def apply_request(db_path, request):
    """Validate and atomically apply one batch; idempotent on request_id."""
    normalized = parse_apply_request(request)
    request_id = normalized["request_id"]
    expected = normalized["expected_generation"]
    request_fingerprint = fingerprint(
        {
            "request_id": request_id,
            "expected_generation": expected,
            "events": normalized["events"],
            "links": normalized["links"],
        }
    )

    _require_existing(db_path)
    try:
        conn = store.connect(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(
            f"activity database is not readable: {exc}"
        ) from exc
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise LedgerStateError(
                f"activity database is not readable: {exc}"
            ) from exc
        try:
            _check_schema(conn)
            # Replay is resolved before the generation check, so a retry of an
            # already applied request succeeds even though generations moved.
            prior = store.get_request(conn, request_id)
            if prior is not None:
                if prior["fingerprint"] != request_fingerprint:
                    raise IdempotentConflict(
                        "request_id reused with different arguments: "
                        + request_id
                    )
                response = json.loads(prior["response_json"])
                conn.rollback()
                return response

            current = store.get_generation(conn)
            if expected != current:
                raise GenerationConflict(
                    f"expected_generation {expected} != current generation "
                    f"{current}"
                )

            events_history, links_history = _load_revisions(conn)
            _check_event_revisions(normalized["events"], events_history)
            _check_link_revisions(normalized["links"], links_history)

            next_generation = current + 1
            for revision in normalized["events"]:
                conn.execute(
                    "INSERT INTO events(event_id,revision,op,account_id,"
                    "event_time,amount_minor,request_id,generation) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        revision["event_id"], revision["revision"],
                        revision["op"], revision["account_id"],
                        revision["event_time"], revision["amount_minor"],
                        request_id, next_generation,
                    ),
                )
            for revision in normalized["links"]:
                conn.execute(
                    "INSERT INTO links(link_id,revision,op,left_account,"
                    "right_account,valid_from,valid_to,request_id,"
                    "generation) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        revision["link_id"], revision["revision"],
                        revision["op"], revision["left_account"],
                        revision["right_account"], revision["valid_from"],
                        revision["valid_to"], request_id, next_generation,
                    ),
                )
            store.set_generation(conn, next_generation)
            response = {
                "status": "applied",
                "request_id": request_id,
                "generation_before": current,
                "generation": next_generation,
                "events_applied": len(normalized["events"]),
                "links_applied": len(normalized["links"]),
            }
            conn.execute(
                "INSERT INTO requests(request_id,fingerprint,response_json,"
                "generation_after) VALUES(?,?,?,?)",
                (
                    request_id, request_fingerprint,
                    json.dumps(response, sort_keys=True, ensure_ascii=False,
                               allow_nan=False),
                    next_generation,
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
# close
# --------------------------------------------------------------------------


def _serialize_rows(rows):
    return json.loads(canonical_json(rows).decode("utf-8"))


def close_period(db_path, request):
    """Freeze ``as_booked`` for ``[start, end)`` at the current generation."""
    normalized = parse_close_request(request)
    close_id = normalized["close_id"]
    request_fingerprint = fingerprint(normalized)

    _require_existing(db_path)
    try:
        conn = store.connect(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(
            f"activity database is not readable: {exc}"
        ) from exc
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise LedgerStateError(
                f"activity database is not readable: {exc}"
            ) from exc
        try:
            _check_schema(conn)
            prior = store.get_close_request(conn, close_id)
            if prior is not None:
                if prior["fingerprint"] != request_fingerprint:
                    raise IdempotentConflict(
                        "close_id reused with different arguments: " + close_id
                    )
                response = json.loads(prior["response_json"])
                conn.rollback()
                return response

            generation = store.get_generation(conn)
            if normalized["expected_generation"] != generation:
                raise GenerationConflict(
                    f"expected_generation {normalized['expected_generation']}"
                    f" != current generation {generation}"
                )

            events_history, links_history = _load_revisions(conn)
            grouped = projection.project(
                events_history, links_history, generation,
                normalized["start"], normalized["end"],
            )
            as_booked_rows = projection.summarize(grouped)
            totals = {
                "event_count": sum(
                    row["event_count"] for row in as_booked_rows
                ),
                "amount_minor": sum(
                    row["amount_minor"] for row in as_booked_rows
                ),
            }
            response = {
                "status": "closed",
                "close_id": close_id,
                "start": normalized["start"],
                "end": normalized["end"],
                "close_generation": generation,
                "as_booked": as_booked_rows,
                "totals": totals,
            }
            conn.execute(
                "INSERT INTO closes(close_id,start,end,close_generation,"
                "as_booked_json) VALUES(?,?,?,?,?)",
                (
                    close_id, normalized["start"], normalized["end"],
                    generation, canonical_json(as_booked_rows).decode("utf-8"),
                ),
            )
            conn.execute(
                "INSERT INTO close_requests(close_id,fingerprint,"
                "response_json,close_generation) VALUES(?,?,?,?)",
                (
                    close_id, request_fingerprint,
                    json.dumps(response, sort_keys=True, ensure_ascii=False,
                               allow_nan=False),
                    generation,
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


def _totals(rows):
    return {
        "event_count": sum(row["event_count"] for row in rows),
        "amount_minor": sum(row["amount_minor"] for row in rows),
    }


def build_report(db_path, close_id):
    """Publish the frozen ``as_booked`` and the current-generation restatement.

    Everything is read in one read-only SQLite snapshot, so the close row, the
    saved ``as_booked`` bytes, the current generation and the full event/link
    history come from the same consistent view.
    """
    close_id = ident(close_id)
    _require_existing(db_path)
    try:
        conn = store.connect_reader(db_path)
    except sqlite3.DatabaseError as exc:
        raise LedgerStateError(
            f"activity database is not readable: {exc}"
        ) from exc
    try:
        try:
            conn.execute("BEGIN")
            _check_schema(conn)
            close_row = store.get_close(conn, close_id)
            if close_row is None:
                raise UnknownCloseError("unknown close_id: " + close_id)
            generation = store.get_generation(conn)
            events_history, links_history = _load_revisions(conn)
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError as exc:
            raise LedgerStateError(
                f"activity database is not readable: {exc}"
            ) from exc
    finally:
        conn.close()

    as_booked_rows = json.loads(close_row["as_booked_json"])
    grouped = projection.project(
        events_history, links_history, generation,
        close_row["start"], close_row["end"],
    )
    restated_rows = projection.summarize(grouped)
    changes = projection.diff(as_booked_rows, restated_rows)
    return {
        "schema_version": store.SCHEMA_VERSION,
        "close_id": close_row["close_id"],
        "start": close_row["start"],
        "end": close_row["end"],
        "as_booked_generation": close_row["close_generation"],
        "restated_generation": generation,
        "as_booked": _serialize_rows(as_booked_rows),
        "as_booked_totals": _totals(as_booked_rows),
        "restated": restated_rows,
        "restated_totals": _totals(restated_rows),
        "changes": changes,
        "changes_totals": {
            "delta_event_count": sum(
                row["delta_event_count"] for row in changes
            ),
            "delta_amount_minor": sum(
                row["delta_amount_minor"] for row in changes
            ),
        },
    }

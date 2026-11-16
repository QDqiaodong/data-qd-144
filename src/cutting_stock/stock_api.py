"""Testable Python API for the offline bounded cutting-plan warehouse.

All inputs go through the same strict validation as the CLI: duplicate JSON
keys and non-finite numbers are rejected by :mod:`cutting_stock.common`,
unknown fields, boolean integers and out-of-range integers are rejected here,
and identifiers use the shared :func:`ident` rule.
"""
import hashlib
import json
import os

from . import stock_planner, stock_store
from .common import ident

MAX_OPEN_ORDERS = 8
MAX_PENDING_PIECES = 18
MAX_LIVE_BARS = 12
MAX_BATCH_OPERATIONS = 64
EXCLUSIVE_OPS = frozenset({"start", "finish"})
OP_KINDS = frozenset(
    {"receive", "order", "amend", "withdraw", "start", "finish"}
)


class StockError(ValueError):
    """Base class for contract violations carrying a stable error code."""

    code = "invalid_request"


class ConflictError(StockError):
    code = "conflict"


class RevisionError(StockError):
    code = "revision_conflict"


class IdempotentConflict(StockError):
    code = "idempotent_conflict"


def _strict_int(value, minimum, maximum, field):
    if type(value) is not int or isinstance(value, bool):
        raise StockError(f"{field} must be an integer in [{minimum},{maximum}]")
    if not minimum <= value <= maximum:
        raise StockError(f"{field} outside allowed bounds [{minimum},{maximum}]")
    return value


def _require_fields(obj, fields, context):
    if not isinstance(obj, dict):
        raise StockError(f"{context} must be an object")
    if set(obj) != set(fields):
        raise StockError(
            f"{context} expects exactly fields {sorted(fields)}, "
            f"got {sorted(obj)}"
        )


def _request_bytes(request):
    text = json.dumps(request, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))
    return text.encode("utf-8")


def _validate_init(data):
    _require_fields(data, "kerf reuse_min bars".split(), "init source")
    kerf = _strict_int(data["kerf"], 0, 1_000_000, "kerf")
    reuse_min = _strict_int(data["reuse_min"], 1, 1_000_000, "reuse_min")
    bars = data["bars"]
    if not isinstance(bars, list):
        raise StockError("bars must be an array")
    seen = set()
    clean_bars = []
    for raw in bars:
        _require_fields(raw, "id length grade".split(), "bar")
        bar_id = ident(raw["id"])
        if bar_id in seen:
            raise StockError("duplicate bar id: " + bar_id)
        seen.add(bar_id)
        length = _strict_int(raw["length"], 1, 1_000_000, "bar length")
        grade = _strict_int(raw["grade"], 0, 9, "bar grade")
        clean_bars.append({"id": bar_id, "length": length, "grade": grade})
    if len(clean_bars) > MAX_LIVE_BARS:
        raise StockError(f"at most {MAX_LIVE_BARS} live bars per warehouse")
    return kerf, reuse_min, clean_bars


def _validate_piece(raw, seen_piece_ids):
    _require_fields(raw, "id length min_grade".split(), "piece")
    piece_id = ident(raw["id"])
    if piece_id in seen_piece_ids:
        raise StockError("duplicate piece id: " + piece_id)
    seen_piece_ids.add(piece_id)
    length = _strict_int(raw["length"], 1, 1_000_000, "piece length")
    min_grade = _strict_int(raw["min_grade"], 0, 9, "piece min_grade")
    return {"id": piece_id, "length": length, "min_grade": min_grade}


def _validate_order_body(raw, seen_piece_ids, existing_piece_ids, field_name):
    _require_fields(raw, "id priority pieces".split(), field_name)
    order_id = ident(raw["id"])
    priority = _strict_int(raw["priority"], 1, 100, "priority")
    pieces_raw = raw["pieces"]
    if not isinstance(pieces_raw, list):
        raise StockError("pieces must be an array")
    local_seen = set()
    pieces = []
    for piece_raw in pieces_raw:
        piece = _validate_piece(piece_raw, local_seen)
        if piece["id"] in existing_piece_ids or piece["id"] in seen_piece_ids:
            raise StockError("piece id already used: " + piece["id"])
        pieces.append(piece)
    if not pieces:
        raise StockError("work order must contain at least one piece")
    if len(pieces) > MAX_PENDING_PIECES:
        raise StockError(
            f"at most {MAX_PENDING_PIECES} pending pieces per batch"
        )
    return {"id": order_id, "priority": priority, "pieces": pieces}


def initialize(db_path, data, source_sha256=None):
    """Create a new warehouse database and persist a copy of the input."""
    kerf, reuse_min, bars = _validate_init(data)
    if source_sha256 is None:
        source_sha256 = hashlib.sha256(_request_bytes(data)).hexdigest()
    if os.path.exists(db_path):
        raise stock_store.DatabaseExistsError(
            "database already exists; init only creates new databases"
        )
    conn = stock_store.connect(db_path)
    try:
        stock_store.initialize_schema(
            conn, source_sha256, kerf, reuse_min, bars, batch_id="init"
        )
    finally:
        conn.close()
    return {"status": "initialized", "bars": len(bars), "revision": 0,
            "source_sha256": source_sha256}


# --- operation validation --------------------------------------------------


def _validate_request(request):
    _require_fields(
        request, "batch_id expected_revision operations".split(), "request"
    )
    batch_id = ident(request["batch_id"])
    expected = _strict_int(
        request["expected_revision"], 0, 10**9, "expected_revision"
    )
    operations = request["operations"]
    if not isinstance(operations, list) or not operations:
        raise StockError("operations must be a non-empty array")
    if len(operations) > MAX_BATCH_OPERATIONS:
        raise StockError("too many operations in one batch")
    kinds = []
    touched_orders, touched_bars = set(), set()
    for raw in operations:
        if not isinstance(raw, dict) or "op" not in raw:
            raise StockError("each operation needs an op field")
        op = raw["op"]
        if op not in OP_KINDS:
            raise StockError("unknown operation: " + str(op))
        kinds.append(op)
        if op == "receive":
            _require_fields(raw, "op bar".split(), "receive")
            _require_fields(raw["bar"], "id length grade".split(), "bar")
            bar_id = ident(raw["bar"]["id"])
            if bar_id in touched_bars:
                raise StockError("bar appears twice in batch: " + bar_id)
            touched_bars.add(bar_id)
            _strict_int(raw["bar"]["length"], 1, 1_000_000, "bar length")
            _strict_int(raw["bar"]["grade"], 0, 9, "bar grade")
        elif op in ("order", "amend"):
            if op == "order":
                _require_fields(raw, "op id priority pieces".split(), "order")
            else:
                _require_fields(raw, "op id priority pieces".split(), "amend")
            order_id = ident(raw["id"])
            if order_id in touched_orders:
                raise StockError("work order appears twice in batch: " + order_id)
            touched_orders.add(order_id)
            _strict_int(raw["priority"], 1, 100, "priority")
            if not isinstance(raw["pieces"], list) or not raw["pieces"]:
                raise StockError("work order must contain at least one piece")
            local = set()
            for piece_raw in raw["pieces"]:
                _require_fields(piece_raw, "id length min_grade".split(), "piece")
                pid = ident(piece_raw["id"])
                if pid in local:
                    raise StockError("duplicate piece id: " + pid)
                local.add(pid)
                _strict_int(piece_raw["length"], 1, 1_000_000, "piece length")
                _strict_int(piece_raw["min_grade"], 0, 9, "piece min_grade")
        elif op == "withdraw":
            _require_fields(raw, "op id".split(), "withdraw")
            order_id = ident(raw["id"])
            if order_id in touched_orders:
                raise StockError("work order appears twice in batch: " + order_id)
            touched_orders.add(order_id)
        elif op in EXCLUSIVE_OPS:
            _require_fields(raw, "op".split(), op)
    exclusive = [k for k in kinds if k in EXCLUSIVE_OPS]
    if exclusive and (len(operations) != 1):
        raise StockError(
            "start and finish are exclusive operations; they cannot be "
            "mixed with other operations"
        )
    return batch_id, expected, operations


def _existing_piece_ids(conn):
    return {
        row["id"]
        for row in conn.execute("SELECT id FROM pieces").fetchall()
    } | {
        row["id"]
        for row in conn.execute("SELECT id FROM piece_tombstones").fetchall()
    }


def _clean_ops_for_ids(operations):
    """Re-parse op bodies with the shared validators against live id sets."""
    cleaned = []
    seen_pieces = set()
    for raw in operations:
        op = raw["op"]
        if op == "receive":
            cleaned.append(
                {
                    "op": "receive",
                    "bar": {
                        "id": ident(raw["bar"]["id"]),
                        "length": _strict_int(
                            raw["bar"]["length"], 1, 1_000_000, "bar length"
                        ),
                        "grade": _strict_int(
                            raw["bar"]["grade"], 0, 9, "bar grade"
                        ),
                    },
                }
            )
        elif op in ("order", "amend"):
            local = set()
            pieces = []
            for piece_raw in raw["pieces"]:
                pieces.append(_validate_piece(piece_raw, local))
            cleaned.append(
                {
                    "op": op,
                    "id": ident(raw["id"]),
                    "priority": _strict_int(
                        raw["priority"], 1, 100, "priority"
                    ),
                    "pieces": pieces,
                }
            )
        elif op == "withdraw":
            cleaned.append({"op": "withdraw", "id": ident(raw["id"])})
        else:
            cleaned.append({"op": op})
    return cleaned


# --- fact mutation ---------------------------------------------------------


def _apply_receive(conn, op, batch_id):
    bar = op["bar"]
    row = conn.execute(
        "SELECT id FROM bar_tombstones WHERE id=?", (bar["id"],)
    ).fetchone()
    if row is not None:
        raise StockError("bar id was already consumed and cannot be reused: "
                         + bar["id"])
    row = conn.execute("SELECT id,status FROM bars WHERE id=?",
                       (bar["id"],)).fetchone()
    if row is not None:
        raise StockError("bar id already exists: " + bar["id"])
    counts = stock_store.bar_status_counts(conn)
    if counts["available"] + counts["occupied"] + 1 > MAX_LIVE_BARS:
        raise StockError(
            f"warehouse holds at most {MAX_LIVE_BARS} non-consumed bars"
        )
    conn.execute(
        "INSERT INTO bars(id,length,grade,status,origin,parent_bar_id,"
        "finished_plan_id,created_batch) VALUES(?,?,?, 'available','received',"
        "NULL,NULL,?)",
        (bar["id"], bar["length"], bar["grade"], batch_id),
    )


def _apply_order(conn, op, batch_id):
    if conn.execute(
        "SELECT 1 FROM work_orders WHERE id=?", (op["id"],)
    ).fetchone() or conn.execute(
        "SELECT 1 FROM order_tombstones WHERE id=?", (op["id"],)
    ).fetchone():
        raise StockError("work order id already used: " + op["id"])
    used = _existing_piece_ids(conn)
    for piece in op["pieces"]:
        if piece["id"] in used:
            raise StockError("piece id already used: " + piece["id"])
    open_count = conn.execute(
        "SELECT COUNT(*) AS n FROM work_orders WHERE status='open'"
    ).fetchone()["n"]
    if open_count + 1 > MAX_OPEN_ORDERS:
        raise StockError(f"at most {MAX_OPEN_ORDERS} open work orders")
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM pieces WHERE status='pending'"
    ).fetchone()["n"]
    if pending + len(op["pieces"]) > MAX_PENDING_PIECES:
        raise StockError(
            f"at most {MAX_PENDING_PIECES} pending pieces across open orders"
        )
    conn.execute(
        "INSERT INTO work_orders(id,priority,status,created_batch,"
        "updated_batch) VALUES(?,?,'open',?,?)",
        (op["id"], op["priority"], batch_id, batch_id),
    )
    for piece in op["pieces"]:
        conn.execute(
            "INSERT INTO pieces(id,work_order_id,length,min_grade,status) "
            "VALUES(?,?,?,?,'pending')",
            (piece["id"], op["id"], piece["length"], piece["min_grade"]),
        )


def _apply_withdraw(conn, op, batch_id):
    row = conn.execute(
        "SELECT status FROM work_orders WHERE id=?", (op["id"],)
    ).fetchone()
    if row is None:
        raise StockError("unknown work order: " + op["id"])
    if row["status"] != "open":
        raise StockError(
            "only open work orders can be withdrawn; status=" + row["status"]
        )
    conn.execute(
        "UPDATE work_orders SET status='withdrawn', updated_batch=? WHERE id=?",
        (batch_id, op["id"]),
    )
    conn.execute(
        "UPDATE pieces SET status='withdrawn' WHERE work_order_id=?",
        (op["id"],),
    )
    conn.execute(
        "INSERT OR IGNORE INTO order_tombstones(id) VALUES(?)", (op["id"],)
    )
    conn.execute(
        "INSERT OR IGNORE INTO piece_tombstones(id) "
        "SELECT id FROM pieces WHERE work_order_id=?", (op["id"],)
    )


# --- planning --------------------------------------------------------------


def _available_bars(conn):
    return [
        {"id": row["id"], "length": row["length"], "grade": row["grade"]}
        for row in conn.execute(
            "SELECT id,length,grade FROM bars WHERE status='available' "
            "ORDER BY id"
        )
    ]


def _replan_open(conn, revision, plan_seq, kerf, reuse_min):
    orders = stock_store.open_orders_with_pieces(conn)
    bars = _available_bars(conn)
    plan = stock_planner.plan_open_orders(orders, bars, kerf, reuse_min)
    plan_id = f"plan-r{revision}-{plan_seq}"
    stock_store.put_snapshot(conn, "open", plan_id, revision, plan)
    stock_store.set_meta(conn, "open_plan_json",
                         json.dumps(plan, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False))
    return plan_id, plan


def _persist_allocations(conn, plan_id, plan):
    conn.execute("DELETE FROM allocations WHERE plan_id=?", (plan_id,))
    rows = []
    for layout in plan["layouts"]:
        for piece in layout["pieces"]:
            rows.append(
                (plan_id, piece["work_order"], piece["id"],
                 layout["bar_id"], piece["seq"])
            )
    conn.executemany(
        "INSERT INTO allocations(plan_id,work_order_id,piece_id,bar_id,seq) "
        "VALUES(?,?,?,?,?)",
        rows,
    )


def _apply_start(conn, op, batch_id, revision, kerf, reuse_min):
    snap = stock_store.get_snapshot(conn, "open")
    if stock_store.get_snapshot(conn, "active") is not None:
        raise StockError("a started plan already exists; partial start refused")
    if snap is None:
        raise StockError("no open plan exists")
    plan = snap["plan"]
    if not plan["accepted"]:
        raise StockError("cannot start a plan with no accepted work orders")
    # The open snapshot must describe the revision the client is acting on.
    if snap["revision"] != revision:
        raise StockError("stale plan snapshot revision; refresh expected_revision")
    plan_id = snap["plan_id"]
    _persist_allocations(conn, plan_id, plan)
    bar_ids = [layout["bar_id"] for layout in plan["layouts"]]
    conn.executemany(
        "UPDATE bars SET status='occupied' WHERE id=?",
        [(bar_id,) for bar_id in bar_ids],
    )
    conn.executemany(
        "UPDATE work_orders SET status='started', updated_batch=? "
        "WHERE id=?",
        [(batch_id, order_id) for order_id in plan["accepted"]],
    )
    placeholders = ",".join("?" for _ in plan["accepted"])
    conn.execute(
        f"UPDATE pieces SET status='cut' WHERE work_order_id IN ({placeholders})",
        plan["accepted"],
    )
    stock_store.put_snapshot(conn, "active", plan_id, revision, plan)
    stock_store.set_meta(conn, "active_plan_json",
                         json.dumps(plan, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False))
    # The open snapshot continues to describe the still-open work orders, but
    # is recomputed against bars that remain available; occupied bars are
    # invisible to this replan.
    remaining_orders = stock_store.open_orders_with_pieces(conn)
    remaining_bars = _available_bars(conn)
    open_plan = stock_planner.plan_open_orders(
        remaining_orders, remaining_bars, kerf, reuse_min
    )
    stock_store.put_snapshot(conn, "open", plan_id + "-open", revision,
                             open_plan)
    stock_store.set_meta(conn, "open_plan_json",
                         json.dumps(open_plan, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False))
    return plan_id


def _unique_remainder_id(conn, plan_id, seq):
    base = f"rem-{plan_id}-{seq}"
    candidate = base
    counter = 1
    while True:
        exists = conn.execute(
            "SELECT 1 FROM bars WHERE id=? UNION ALL "
            "SELECT 1 FROM bar_tombstones WHERE id=?",
            (candidate, candidate),
        ).fetchone()
        if exists is None:
            return candidate
        counter += 1
        candidate = f"{base}-{counter}"


def _apply_finish(conn, batch_id, current_revision, kerf, reuse_min):
    active = stock_store.get_snapshot(conn, "active")
    if active is None:
        raise StockError("no started plan to finish")
    plan = active["plan"]
    plan_id = active["plan_id"]
    accepted = list(plan["accepted"])
    finished_bars, returned_remainders, waste_rows = [], [], []
    rem_seq = 0
    for layout in plan["layouts"]:
        bar_id = layout["bar_id"]
        row = conn.execute(
            "SELECT length,grade,status FROM bars WHERE id=?", (bar_id,)
        ).fetchone()
        if row is None or row["status"] != "occupied":
            raise StockError("plan bar is not occupied: " + bar_id)
        if layout["remainder"] >= reuse_min:
            rem_seq += 1
            new_id = _unique_remainder_id(conn, plan_id, rem_seq)
            conn.execute(
                "INSERT INTO bars(id,length,grade,status,origin,"
                "parent_bar_id,finished_plan_id,created_batch) VALUES(?,?,?,"
                "'available','remainder',?,?,?)",
                (new_id, layout["remainder"], row["grade"], bar_id,
                 plan_id, batch_id),
            )
            returned_remainders.append(
                {
                    "new_bar_id": new_id,
                    "source_bar_id": bar_id,
                    "length": layout["remainder"],
                    "grade": row["grade"],
                }
            )
        else:
            conn.execute(
                "INSERT INTO waste(plan_id,source_bar_id,length,grade,"
                "finished_batch) VALUES(?,?,?,?,?)",
                (plan_id, bar_id, layout["remainder"], row["grade"], batch_id),
            )
            waste_rows.append(
                {
                    "source_bar_id": bar_id,
                    "length": layout["remainder"],
                    "grade": row["grade"],
                }
            )
        conn.execute(
            "UPDATE bars SET status='consumed', finished_plan_id=? WHERE id=?",
            (plan_id, bar_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO bar_tombstones(id) VALUES(?)", (bar_id,)
        )
        finished_bars.append(bar_id)
    conn.executemany(
        "UPDATE work_orders SET status='finished', updated_batch=? WHERE id=?",
        [(batch_id, order_id) for order_id in accepted],
    )
    counts = stock_store.bar_status_counts(conn)
    if counts["available"] + counts["occupied"] > MAX_LIVE_BARS:
        raise StockError(
            f"finish would exceed {MAX_LIVE_BARS} live bars"
        )
    finish_record = {
        "plan_id": plan_id,
        "consumed_bars": sorted(finished_bars, key=lambda s: s.encode("utf-8")),
        "returned_remainders": returned_remainders,
        "waste": waste_rows,
    }
    conn.execute(
        "INSERT INTO plan_history(plan_id,started_revision,finished_revision,"
        "plan_json,finish_json) VALUES(?,?,?,?,?)",
        (
            plan_id,
            active["revision"],
            current_revision + 1,
            json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False),
            json.dumps(finish_record, sort_keys=True, ensure_ascii=False,
                       allow_nan=False),
        ),
    )
    stock_store.clear_snapshot(conn, "active")
    stock_store.set_meta(conn, "active_plan_json", "")
    return plan_id, finish_record


# --- public batch entry point ---------------------------------------------


def _enforce_invariants(conn):
    counts = stock_store.bar_status_counts(conn)
    if counts["available"] + counts["occupied"] > MAX_LIVE_BARS:
        raise StockError(
            f"warehouse holds at most {MAX_LIVE_BARS} non-consumed bars"
        )
    open_count = conn.execute(
        "SELECT COUNT(*) AS n FROM work_orders WHERE status='open'"
    ).fetchone()["n"]
    if open_count > MAX_OPEN_ORDERS:
        raise StockError(f"at most {MAX_OPEN_ORDERS} open work orders")
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM pieces WHERE status='pending'"
    ).fetchone()["n"]
    if pending > MAX_PENDING_PIECES:
        raise StockError(
            f"at most {MAX_PENDING_PIECES} pending pieces across open orders"
        )
    active = stock_store.get_snapshot(conn, "active")
    if active is not None:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE scope='active'"
        ).fetchone()["n"]
        if n != 1:
            raise StockError("at most one started plan is allowed")


def apply_request(db_path, request):
    """Validate and atomically apply one batch; idempotent on batch_id."""
    batch_id, expected, operations = _validate_request(request)
    request_text = json.dumps(
        request, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request_digest = hashlib.sha256(request_text).hexdigest()
    conn = stock_store.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT request_sha256,response_json,revision_after FROM batches "
            "WHERE batch_id=?", (batch_id,),
        ).fetchone()
        current_revision = stock_store.get_int_meta(conn, "revision")
        if prior is not None:
            if prior["request_sha256"] != request_digest:
                raise IdempotentConflict(
                    "batch_id reused with a different request payload"
                )
            conn.rollback()
            return json.loads(prior["response_json"])
        if expected != current_revision:
            raise RevisionError(
                f"expected_revision {expected} != current revision "
                f"{current_revision}"
            )
        kerf = stock_store.get_int_meta(conn, "kerf")
        reuse_min = stock_store.get_int_meta(conn, "reuse_min")
        cleaned = _clean_ops_for_ids(operations)
        kind = cleaned[0]["op"]
        finish_record = None
        started_plan_id = None
        if kind in EXCLUSIVE_OPS and len(cleaned) != 1:
            raise StockError("exclusive operation cannot share a batch")
        if kind == "start":
            started_plan_id = _apply_start(
                conn, cleaned[0], batch_id, current_revision,
                kerf, reuse_min,
            )
        elif kind == "finish":
            started_plan_id, finish_record = _apply_finish(
                conn, batch_id, current_revision, kerf, reuse_min
            )
            # A finished plan frees no accepted order: replan the remaining
            # open orders (including previously deferred ones) immediately at
            # the new revision, using only bars that are still available.
            plan_seq = conn.execute(
                "SELECT COUNT(*) AS n FROM plan_history"
            ).fetchone()["n"] + 1
            _replan_open(
                conn, current_revision + 1, plan_seq, kerf, reuse_min
            )
        else:
            for op in cleaned:
                if op["op"] == "receive":
                    _apply_receive(conn, op, batch_id)
                elif op["op"] == "order":
                    _apply_order(conn, op, batch_id)
                elif op["op"] == "amend":
                    _amend_fact(conn, op, batch_id)
                elif op["op"] == "withdraw":
                    _apply_withdraw(conn, op, batch_id)
                else:  # pragma: no cover - guarded by validation
                    raise StockError("unexpected operation: " + op["op"])
            # Replan open work orders against bars that are still available.
            plan_seq = conn.execute(
                "SELECT COUNT(*) AS n FROM plan_history"
            ).fetchone()["n"] + 1
            _replan_open(
                conn, current_revision + 1, plan_seq, kerf, reuse_min
            )
        _enforce_invariants(conn)
        new_revision = current_revision + 1
        stock_store.set_meta(conn, "revision", new_revision)
        response = {
            "status": "ok",
            "batch_id": batch_id,
            "revision": new_revision,
            "operation": kind,
        }
        if kind == "start":
            response["started_plan_id"] = started_plan_id
        if kind == "finish":
            response["finished_plan_id"] = started_plan_id
            response["finish"] = finish_record
        conn.execute(
            "INSERT INTO batches(batch_id,request_sha256,request_json,"
            "response_json,revision_after) VALUES(?,?,?,?,?)",
            (
                batch_id,
                request_digest,
                request_text.decode("utf-8"),
                json.dumps(response, sort_keys=True, ensure_ascii=False,
                           allow_nan=False),
                new_revision,
            ),
        )
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _amend_fact(conn, op, batch_id):
    """Amend an open order; replaced piece ids become permanently unusable."""
    row = conn.execute(
        "SELECT status FROM work_orders WHERE id=?", (op["id"],)
    ).fetchone()
    if row is None:
        if conn.execute(
            "SELECT 1 FROM order_tombstones WHERE id=?", (op["id"],)
        ).fetchone():
            raise StockError("cannot amend withdrawn work order: " + op["id"])
        raise StockError("unknown work order: " + op["id"])
    if row["status"] != "open":
        raise StockError(
            "only open work orders can be amended; status=" + row["status"]
        )
    other_pending = conn.execute(
        "SELECT COUNT(*) AS n FROM pieces WHERE status='pending' "
        "AND work_order_id<>?", (op["id"],)
    ).fetchone()["n"]
    if other_pending + len(op["pieces"]) > MAX_PENDING_PIECES:
        raise StockError(
            f"at most {MAX_PENDING_PIECES} pending pieces across open orders"
        )
    old_rows = conn.execute(
        "SELECT id FROM pieces WHERE work_order_id=? AND status='pending'",
        (op["id"],),
    ).fetchall()
    used = _existing_piece_ids(conn)
    local = set()
    for piece in op["pieces"]:
        if piece["id"] in local:
            raise StockError("duplicate piece id: " + piece["id"])
        local.add(piece["id"])
        if piece["id"] in used:
            raise StockError("piece id already used: " + piece["id"])
    for old in old_rows:
        conn.execute(
            "INSERT OR IGNORE INTO piece_tombstones(id) VALUES(?)",
            (old["id"],),
        )
    conn.execute(
        "DELETE FROM pieces WHERE work_order_id=? AND status='pending'",
        (op["id"],),
    )
    conn.execute(
        "UPDATE work_orders SET priority=?, updated_batch=? WHERE id=?",
        (op["priority"], batch_id, op["id"]),
    )
    for piece in op["pieces"]:
        conn.execute(
            "INSERT INTO pieces(id,work_order_id,length,min_grade,status) "
            "VALUES(?,?,?,?,'pending')",
            (piece["id"], op["id"], piece["length"], piece["min_grade"]),
        )


# --- reporting -------------------------------------------------------------


def _byte_sorted(values, key=lambda item: item):
    return sorted(values, key=lambda item: key(item).encode("utf-8"))


def build_report(db_path):
    """Read a single revision snapshot and emit the deterministic report."""
    conn = stock_store.connect(db_path)
    try:
        revision = stock_store.get_int_meta(conn, "revision")
        kerf = stock_store.get_int_meta(conn, "kerf")
        reuse_min = stock_store.get_int_meta(conn, "reuse_min")
        open_snap = stock_store.get_snapshot(conn, "open")
        active_snap = stock_store.get_snapshot(conn, "active")
        bars = [dict(row) for row in conn.execute(
            "SELECT id,length,grade,status,origin,parent_bar_id,"
            "finished_plan_id,created_batch FROM bars"
        ).fetchall()]
        orders = [dict(row) for row in conn.execute(
            "SELECT id,priority,status,created_batch,updated_batch "
            "FROM work_orders"
        ).fetchall()]
        waste = [dict(row) for row in conn.execute(
            "SELECT plan_id,source_bar_id,length,grade,finished_batch "
            "FROM waste"
        ).fetchall()]
        history = [dict(row) for row in conn.execute(
            "SELECT plan_id,started_revision,finished_revision,finish_json "
            "FROM plan_history"
        ).fetchall()]
    finally:
        conn.close()

    report_orders = []
    plan = open_snap["plan"] if open_snap else None
    active_plan = active_snap["plan"] if active_snap else None
    accepted_set = set(plan["accepted"]) if plan else set()
    deferred_map = {d["id"]: d["reasons"] for d in plan["deferred"]} if plan else {}
    for order in _byte_sorted(orders, key=lambda o: o["id"]):
        if order["status"] == "open":
            state = "accepted" if order["id"] in accepted_set else "deferred"
            reasons = [] if state == "accepted" else deferred_map.get(order["id"], [])
        else:
            state = order["status"]
            reasons = []
        report_orders.append(
            {
                "id": order["id"],
                "priority": order["priority"],
                "state": state,
                "deferral_reasons": reasons,
            }
        )

    layout_by_bar = {}
    source_plan = active_plan if active_plan else plan
    if source_plan:
        for layout in source_plan["layouts"]:
            layout_by_bar[layout["bar_id"]] = layout
    report_bars = []
    for bar in _byte_sorted(bars, key=lambda b: b["id"]):
        layout = layout_by_bar.get(bar["id"])
        entry = {
            "id": bar["id"],
            "length": bar["length"],
            "grade": bar["grade"],
            "status": bar["status"],
            "origin": bar["origin"],
            "parent_bar_id": bar["parent_bar_id"],
            "allocation": None,
        }
        if layout is not None:
            entry["allocation"] = {
                "pieces": [
                    {
                        "seq": piece["seq"],
                        "piece_id": piece["id"],
                        "work_order": piece["work_order"],
                        "length": piece["length"],
                        "min_grade": piece["min_grade"],
                    }
                    for piece in layout["pieces"]
                ],
                "used": layout["used"],
                "kerf_loss": layout["kerf_loss"],
                "planned_remainder": layout["remainder"],
                "planned_reusable": layout["planned_reusable"],
            }
        report_bars.append(entry)

    returned = []
    for row in history:
        finish = json.loads(row["finish_json"])
        for rem in finish["returned_remainders"]:
            returned.append(
                {
                    "plan_id": row["plan_id"],
                    "new_bar_id": rem["new_bar_id"],
                    "source_bar_id": rem["source_bar_id"],
                    "length": rem["length"],
                    "grade": rem["grade"],
                }
            )
    report = {
        "revision": revision,
        "kerf": kerf,
        "reuse_min": reuse_min,
        "open_plan": _report_plan(plan),
        "active_plan": _report_plan(active_plan),
        "work_orders": report_orders,
        "bars": report_bars,
        "returned_remainders": _byte_sorted(
            returned, key=lambda item: item["new_bar_id"]
        ),
        "waste": _byte_sorted(
            [
                {
                    "plan_id": row["plan_id"],
                    "source_bar_id": row["source_bar_id"],
                    "length": row["length"],
                    "grade": row["grade"],
                }
                for row in waste
            ],
            key=lambda item: item["source_bar_id"],
        ),
        "totals": _report_totals(plan, active_plan, bars),
    }
    return report


def _report_plan(plan):
    if plan is None:
        return None
    return {
        "accepted": list(plan["accepted"]),
        "deferred": [
            {"id": d["id"], "reasons": list(d["reasons"])}
            for d in _byte_sorted(plan["deferred"], key=lambda d: d["id"])
        ],
        "layouts": [
            {
                "bar_id": layout["bar_id"],
                "pieces": [
                    {
                        "seq": piece["seq"],
                        "piece_id": piece["id"],
                        "work_order": piece["work_order"],
                    }
                    for piece in layout["pieces"]
                ],
                "used": layout["used"],
                "kerf_loss": layout["kerf_loss"],
                "planned_remainder": layout["remainder"],
                "planned_reusable": layout["planned_reusable"],
            }
            for layout in _byte_sorted(
                plan["layouts"], key=lambda layout: layout["bar_id"]
            )
        ],
        "totals": dict(plan["totals"]),
    }


def _report_totals(plan, active_plan, bars):
    counts = {"available": 0, "occupied": 0, "consumed": 0}
    for bar in bars:
        counts[bar["status"]] += 1
    current = active_plan if active_plan else plan
    return {
        "priority_sum": current["totals"]["priority_sum"] if current else 0,
        "bars_available": counts["available"],
        "bars_occupied": counts["occupied"],
        "bars_consumed": counts["consumed"],
    }

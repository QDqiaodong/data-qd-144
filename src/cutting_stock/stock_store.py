"""SQLite persistence and transaction/snapshot boundaries for the warehouse.

Every state-changing command runs in one immediate transaction.  All fact
tables, plan allocations, the batch idempotency log and the revision bump are
written in that single transaction; a failure before COMMIT rolls back every
change so facts, allocations, remainders and the revision stay consistent.
"""
import json
import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE meta (
    key TEXT PRIMARY KEY CHECK (key IN
        ('schema_version','revision','kerf','reuse_min',
         'open_plan_json','active_plan_json','source_sha256')),
    value TEXT NOT NULL
);
CREATE TABLE bars (
    id TEXT PRIMARY KEY,
    length INTEGER NOT NULL CHECK (length BETWEEN 1 AND 1000000),
    grade INTEGER NOT NULL CHECK (grade BETWEEN 0 AND 9),
    status TEXT NOT NULL CHECK (status IN ('available','occupied','consumed')),
    origin TEXT NOT NULL CHECK (origin IN ('initial','received','remainder')),
    parent_bar_id TEXT,
    finished_plan_id TEXT,
    created_batch TEXT NOT NULL
);
CREATE TABLE work_orders (
    id TEXT PRIMARY KEY,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 100),
    status TEXT NOT NULL CHECK (status IN ('open','started','finished','withdrawn')),
    created_batch TEXT NOT NULL,
    updated_batch TEXT NOT NULL
);
CREATE TABLE pieces (
    id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
    length INTEGER NOT NULL CHECK (length BETWEEN 1 AND 1000000),
    min_grade INTEGER NOT NULL CHECK (min_grade BETWEEN 0 AND 9),
    status TEXT NOT NULL CHECK (status IN ('pending','cut','withdrawn'))
);
CREATE TABLE bar_tombstones (id TEXT PRIMARY KEY);
CREATE TABLE order_tombstones (id TEXT PRIMARY KEY);
CREATE TABLE piece_tombstones (id TEXT PRIMARY KEY);
CREATE TABLE allocations (
    plan_id TEXT NOT NULL,
    work_order_id TEXT NOT NULL,
    piece_id TEXT NOT NULL,
    bar_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    PRIMARY KEY (plan_id, piece_id)
);
CREATE TABLE snapshots (
    scope TEXT PRIMARY KEY CHECK (scope IN ('open','active')),
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL
);
CREATE TABLE plan_history (
    plan_id TEXT PRIMARY KEY,
    started_revision INTEGER NOT NULL,
    finished_revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    finish_json TEXT NOT NULL
);
CREATE TABLE waste (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    source_bar_id TEXT NOT NULL,
    length INTEGER NOT NULL,
    grade INTEGER NOT NULL,
    finished_batch TEXT NOT NULL
);
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    revision_after INTEGER NOT NULL
);
"""


def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_schema(conn, source_sha256, kerf, reuse_min, bars, batch_id):
    """Create a brand-new database; fails if any table already exists."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if existing:
            raise DatabaseExistsError("database already exists; init only creates new databases")
        conn.executescript(SCHEMA)
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "revision": "0",
            "kerf": str(kerf),
            "reuse_min": str(reuse_min),
            "source_sha256": source_sha256,
        }
        conn.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?)", meta.items()
        )
        conn.executemany(
            "INSERT INTO bars(id,length,grade,status,origin,parent_bar_id,"
            "finished_plan_id,created_batch) VALUES(?,?,?, 'available',"
            "'initial', NULL, NULL, ?)",
            [(b["id"], b["length"], b["grade"], batch_id) for b in bars],
        )
        empty_plan = {
            "accepted": [],
            "deferred": [],
            "layouts": [],
            "totals": {
                "priority_sum": 0,
                "accepted_count": 0,
                "bars_used": 0,
                "total_remainder": 0,
            },
        }
        conn.execute(
            "INSERT INTO snapshots(scope,plan_id,revision,plan_json) "
            "VALUES('open','plan-r0-0',0,?)",
            (json.dumps(empty_plan, sort_keys=True, ensure_ascii=False,
                        allow_nan=False),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class DatabaseExistsError(ValueError):
    pass


# --- small typed accessors -------------------------------------------------


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_int_meta(conn, key):
    return int(get_meta(conn, key))


def get_snapshot(conn, scope):
    row = conn.execute(
        "SELECT plan_id,revision,plan_json FROM snapshots WHERE scope=?",
        (scope,),
    ).fetchone()
    if row is None:
        return None
    return {
        "plan_id": row["plan_id"],
        "revision": row["revision"],
        "plan": json.loads(row["plan_json"]),
    }


def put_snapshot(conn, scope, plan_id, revision, plan):
    conn.execute("DELETE FROM snapshots WHERE scope=?", (scope,))
    conn.execute(
        "INSERT INTO snapshots(scope,plan_id,revision,plan_json) "
        "VALUES(?,?,?,?)",
        (scope, plan_id, revision, json.dumps(plan, sort_keys=True,
                                              ensure_ascii=False,
                                              allow_nan=False)),
    )


def clear_snapshot(conn, scope):
    conn.execute("DELETE FROM snapshots WHERE scope=?", (scope,))


def active_bars(conn):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id,length,grade,status,origin,parent_bar_id,created_batch "
            "FROM bars ORDER BY id"
        )
    ]


def open_orders_with_pieces(conn):
    orders = [
        dict(row)
        for row in conn.execute(
            "SELECT id,priority FROM work_orders WHERE status='open' "
            "ORDER BY id"
        )
    ]
    for order in orders:
        order["pieces"] = [
            dict(row)
            for row in conn.execute(
                "SELECT id,length,min_grade FROM pieces "
                "WHERE work_order_id=? AND status='pending' ORDER BY id",
                (order["id"],),
            )
        ]
    return orders


def bar_status_counts(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM bars GROUP BY status"
    ).fetchall()
    counts = {"available": 0, "occupied": 0, "consumed": 0}
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts

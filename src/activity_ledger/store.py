"""SQLite persistence, schema and transaction boundaries for the activity ledger.

Everything lives in one database created by ``activity-init``.  The DDL is
issued as individual statements inside one ``BEGIN IMMEDIATE`` transaction
(never ``executescript``, which would implicitly COMMIT and could publish a
half-built schema).

Tables
------
``meta``            key/value: schema_version, generation.
``events``          append-only event revisions (put and delete both kept).
``links``           append-only link revisions; delete is a knowledge-time
                    retraction, never an erasure of earlier revisions.
``requests``        successful apply receipts for idempotent replay.
``closes``          frozen close results: period, close_generation and the
                    canonical-JSON ``as_booked`` bytes.
``close_requests``  close idempotency receipts keyed by ``close_id``.

Generation
----------
``generation`` starts at 0 and is incremented by exactly one per successful
apply.  A close never advances it: closes are read-only observations of a
generation and freeze their result permanently.
"""
import os
import sqlite3
from urllib.parse import quote

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS = (
    """CREATE TABLE meta (
    key TEXT PRIMARY KEY CHECK (key IN ('schema_version','generation')),
    value TEXT NOT NULL
)""",
    """CREATE TABLE events (
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    op TEXT NOT NULL CHECK (op IN ('put','delete')),
    account_id TEXT,
    event_time INTEGER,
    amount_minor INTEGER,
    request_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    PRIMARY KEY (event_id, revision),
    CHECK (
        (op='put' AND account_id IS NOT NULL AND event_time IS NOT NULL
                 AND amount_minor IS NOT NULL)
        OR
        (op='delete' AND account_id IS NULL AND event_time IS NULL
                    AND amount_minor IS NULL)
    )
)""",
    """CREATE TABLE links (
    link_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    op TEXT NOT NULL CHECK (op IN ('put','delete')),
    left_account TEXT,
    right_account TEXT,
    valid_from INTEGER,
    valid_to INTEGER,
    request_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    PRIMARY KEY (link_id, revision),
    CHECK (
        (op='put' AND left_account IS NOT NULL AND right_account IS NOT NULL
                 AND valid_from IS NOT NULL AND valid_to IS NOT NULL
                 AND left_account <> right_account
                 AND valid_from < valid_to)
        OR
        (op='delete' AND left_account IS NULL AND right_account IS NULL
                    AND valid_from IS NULL AND valid_to IS NULL)
    )
)""",
    """CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    generation_after INTEGER NOT NULL
)""",
    """CREATE TABLE closes (
    close_id TEXT PRIMARY KEY,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    close_generation INTEGER NOT NULL,
    as_booked_json TEXT NOT NULL,
    CHECK (start < end)
)""",
    """CREATE TABLE close_requests (
    close_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    close_generation INTEGER NOT NULL
)""",
)


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect_reader(db_path):
    """Open strictly read-only; never creates the database file."""
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def database_exists(db_path):
    return os.path.exists(db_path)


def has_schema(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def initialize_schema(conn):
    """Create the whole schema inside an already-open immediate transaction."""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute("INSERT INTO meta(key,value) VALUES('generation','0')")


def get_generation(conn):
    row = conn.execute(
        "SELECT value FROM meta WHERE key='generation'"
    ).fetchone()
    return 0 if row is None else int(row["value"])


def set_generation(conn, value):
    conn.execute(
        "UPDATE meta SET value=? WHERE key='generation'", (str(value),)
    )


def get_request(conn, request_id):
    return conn.execute(
        "SELECT fingerprint,response_json,generation_after "
        "FROM requests WHERE request_id=?",
        (request_id,),
    ).fetchone()


def get_close_request(conn, close_id):
    return conn.execute(
        "SELECT fingerprint,response_json,close_generation "
        "FROM close_requests WHERE close_id=?",
        (close_id,),
    ).fetchone()


def get_close(conn, close_id):
    return conn.execute(
        "SELECT close_id,start,end,close_generation,as_booked_json "
        "FROM closes WHERE close_id=?",
        (close_id,),
    ).fetchone()


# --- revision loading --------------------------------------------------------


def load_event_revisions(conn):
    """All event revisions keyed by event_id, ordered by revision."""
    events = {}
    for row in conn.execute(
        "SELECT event_id,revision,op,account_id,event_time,amount_minor,"
        "generation FROM events ORDER BY event_id,revision"
    ).fetchall():
        events.setdefault(row["event_id"], []).append(
            {
                "revision": row["revision"],
                "op": row["op"],
                "account_id": row["account_id"],
                "event_time": row["event_time"],
                "amount_minor": row["amount_minor"],
                "generation": row["generation"],
            }
        )
    return events


def load_link_revisions(conn):
    """All link revisions keyed by link_id, ordered by revision."""
    links = {}
    for row in conn.execute(
        "SELECT link_id,revision,op,left_account,right_account,"
        "valid_from,valid_to,generation FROM links ORDER BY link_id,revision"
    ).fetchall():
        links.setdefault(row["link_id"], []).append(
            {
                "revision": row["revision"],
                "op": row["op"],
                "left_account": row["left_account"],
                "right_account": row["right_account"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "generation": row["generation"],
            }
        )
    return links

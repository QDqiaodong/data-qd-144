"""SQLite persistence, schema and transaction boundaries for the AP ledger.

The ledger is created lazily and atomically by the first successful ``post``.
A ``check`` against a missing database opens it read-only (``mode=ro``) and
therefore never creates the file.  Every state-changing operation runs in one
``BEGIN IMMEDIATE`` transaction; a failure before COMMIT rolls back the schema
creation, postings, journal entries, receipt records and idempotency log as a
single unit, so no half-written occupation can survive a crash.

History is append-only.  Reversals never delete or rewrite evidence: they add
negated journal entries and a reversed marker, and the effective occupation
view subtracts reversed allocations when computing quantities available to the
next check.  Frozen invoice content and the frozen unit-price evidence live on
the posting rows, so later source corrections can never recompute or pollute
historical amounts.
"""
import json
import os
import sqlite3

SCHEMA_VERSION = 1

# Individual DDL statements (never ``executescript``: that helper issues an
# implicit COMMIT, which would publish the schema even when the surrounding
# transaction later rolls back).  Default rollback-journal mode keeps the
# whole schema creation inside the caller's ``BEGIN IMMEDIATE`` transaction.
SCHEMA_STATEMENTS = (
    """CREATE TABLE meta (
    key TEXT PRIMARY KEY CHECK (key IN ('schema_version','sequence')),
    value TEXT NOT NULL
)""",
    """CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('post','reverse')),
    fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    sequence_after INTEGER NOT NULL
)""",
    """CREATE TABLE receipt_records (
    receipt_id TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    po_line TEXT NOT NULL,
    sku TEXT NOT NULL,
    accepted_qty INTEGER NOT NULL
)""",
    """CREATE TABLE postings (
    posting_id INTEGER PRIMARY KEY,
    sequence INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    supplier TEXT NOT NULL,
    invoice_json TEXT NOT NULL,
    reversed_by INTEGER,
    reversed_request_id TEXT
)""",
    """CREATE TABLE posting_lines (
    posting_id INTEGER NOT NULL REFERENCES postings(posting_id),
    line_id TEXT NOT NULL,
    po_line TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL,
    net_cents INTEGER NOT NULL,
    tax_bps INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    net_fraction TEXT NOT NULL,
    PRIMARY KEY (posting_id, line_id)
)""",
    """CREATE TABLE posting_allocations (
    posting_id INTEGER NOT NULL REFERENCES postings(posting_id),
    line_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price_num INTEGER NOT NULL,
    price_den INTEGER NOT NULL,
    PRIMARY KEY (posting_id, line_id, receipt_id)
)""",
    """CREATE TABLE journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence INTEGER NOT NULL,
    posting_id INTEGER NOT NULL,
    sign INTEGER NOT NULL CHECK (sign IN (1,-1)),
    account TEXT NOT NULL CHECK (account IN
        ('purchase_expense','input_tax','accounts_payable')),
    direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    amount_cents INTEGER NOT NULL
)""",
)

JOURNAL_ACCOUNTS = ("purchase_expense", "input_tax", "accounts_payable")


def connect_writer(db_path):
    """Open a read/write connection; the file is created on first write."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect_reader(db_path):
    """Open strictly read-only; never creates the database file."""
    # The URI path must be percent-escaped so path characters stay safe.
    from urllib.parse import quote

    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def ledger_exists(db_path):
    return os.path.exists(db_path)


def is_empty_database_file(db_path):
    """True for a zero-length or table-less SQLite file left by a rollback."""
    if not os.path.exists(db_path):
        return False
    if os.path.getsize(db_path) == 0:
        return True
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            return row[0] == 0
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def has_schema(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def initialize_schema(conn):
    """Create the schema inside an already-open immediate transaction.

    Uses individual statements so the DDL participates in the transaction
    instead of being auto-committed by ``executescript``.
    """
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute("INSERT INTO meta(key,value) VALUES('sequence','0')")


def get_sequence(conn):
    row = conn.execute(
        "SELECT value FROM meta WHERE key='sequence'"
    ).fetchone()
    return 0 if row is None else int(row["value"])


def set_sequence(conn, value):
    conn.execute(
        "UPDATE meta SET value=? WHERE key='sequence'", (str(value),)
    )


def get_request(conn, request_id):
    return conn.execute(
        "SELECT operation,fingerprint,response_json,sequence_after "
        "FROM requests WHERE request_id=?",
        (request_id,),
    ).fetchone()


def active_usage_by_receipt(conn):
    """Sum live (non-reversed) allocated units keyed by receipt id."""
    rows = conn.execute(
        "SELECT a.receipt_id AS receipt_id, COALESCE(SUM(a.qty),0) AS used "
        "FROM posting_allocations a "
        "JOIN postings p ON p.posting_id = a.posting_id "
        "WHERE p.reversed_by IS NULL "
        "GROUP BY a.receipt_id"
    ).fetchall()
    return {row["receipt_id"]: row["used"] for row in rows}


def active_invoice_ids(conn):
    rows = conn.execute(
        "SELECT DISTINCT invoice_id FROM postings WHERE reversed_by IS NULL"
    ).fetchall()
    return {row["invoice_id"] for row in rows}


def load_receipt_records(conn):
    return {
        row["receipt_id"]: dict(row)
        for row in conn.execute(
            "SELECT receipt_id,supplier,po_line,sku,accepted_qty "
            "FROM receipt_records"
        ).fetchall()
    }


def load_live_postings(conn):
    """Full live posting evidence (frozen invoices, lines, allocations)."""
    postings = []
    for prow in conn.execute(
        "SELECT posting_id,sequence,request_id,source_sha256,invoice_id,"
        "supplier,invoice_json FROM postings WHERE reversed_by IS NULL "
        "ORDER BY posting_id"
    ).fetchall():
        posting = dict(prow)
        posting["invoice"] = json.loads(prow["invoice_json"])
        posting["lines"] = [
            dict(row)
            for row in conn.execute(
                "SELECT line_id,po_line,sku,qty,net_cents,tax_bps,tax_cents,"
                "net_fraction FROM posting_lines WHERE posting_id=? "
                "ORDER BY line_id",
                (prow["posting_id"],),
            ).fetchall()
        ]
        posting["allocations"] = [
            dict(row)
            for row in conn.execute(
                "SELECT line_id,receipt_id,qty,price_num,price_den "
                "FROM posting_allocations WHERE posting_id=? "
                "ORDER BY line_id,receipt_id",
                (prow["posting_id"],),
            ).fetchall()
        ]
        postings.append(posting)
    return postings


def load_all_postings(conn):
    """Every posting including reversed ones, for the report."""
    postings = []
    for prow in conn.execute(
        "SELECT posting_id,sequence,request_id,source_sha256,invoice_id,"
        "supplier,invoice_json,reversed_by,reversed_request_id "
        "FROM postings ORDER BY posting_id"
    ).fetchall():
        posting = dict(prow)
        posting["invoice"] = json.loads(prow["invoice_json"])
        posting["lines"] = [
            dict(row)
            for row in conn.execute(
                "SELECT line_id,po_line,sku,qty,net_cents,tax_bps,tax_cents,"
                "net_fraction FROM posting_lines WHERE posting_id=? "
                "ORDER BY line_id",
                (prow["posting_id"],),
            ).fetchall()
        ]
        posting["allocations"] = [
            dict(row)
            for row in conn.execute(
                "SELECT line_id,receipt_id,qty,price_num,price_den "
                "FROM posting_allocations WHERE posting_id=? "
                "ORDER BY line_id,receipt_id",
                (prow["posting_id"],),
            ).fetchall()
        ]
        postings.append(posting)
    return postings


def load_journal(conn):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT sequence,posting_id,sign,account,direction,amount_cents "
            "FROM journal_entries ORDER BY id"
        ).fetchall()
    ]

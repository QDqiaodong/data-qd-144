"""Strict validation for activity-apply and activity-close request documents.

Apply request shape (exactly four fields)::

    {
      "request_id": "REQ-1",
      "expected_generation": 0,
      "events": [
        {"event_id","revision","op","account_id","event_time","amount_minor"}
      ],
      "links": [
        {"link_id","revision","op","left_account","right_account",
         "valid_from","valid_to"}
      ]
    }

Close request shape (exactly four fields)::

    {"close_id": "C-1", "expected_generation": 3, "start": 0, "end": 100}

Validation is purely structural: it never opens a database.  Identifiers use
the shared :func:`cutting_stock.common.ident` rule, integers are always strict
(booleans are rejected even though ``bool`` subclasses ``int``), unknown fields
and duplicate identifiers are rejected, and ``delete`` revisions must carry
``null`` in every business field.  Semantic checks that depend on the stored
history (consecutive revisions, prior undeleted revision, generation equality)
live in :mod:`activity_ledger.api`.
"""
import hashlib
import json

from cutting_stock.common import ident

MAX_BATCH_ENTRIES = 5000
MAX_TIME = 10**12
MAX_AMOUNT = 10**12
MAX_GENERATION = 10**12
MAX_REVISION = 10**12

EVENT_FIELDS = (
    "event_id", "revision", "op", "account_id", "event_time", "amount_minor"
)
LINK_FIELDS = (
    "link_id", "revision", "op", "left_account", "right_account",
    "valid_from", "valid_to",
)


class ActivityError(ValueError):
    """Contract violation carrying a stable error code for CLI exit 2."""

    code = "invalid_request"


class GenerationConflict(ActivityError):
    """``expected_generation`` no longer matches the database generation."""

    code = "generation_conflict"


class IdempotentConflict(ActivityError):
    """A request/close identifier was reused with different arguments."""

    code = "idempotent_conflict"


class LedgerStateError(ActivityError):
    """The database is missing, unreadable or has no activity schema."""

    code = "ledger_state"


class UnknownCloseError(ActivityError):
    """``activity-report`` referenced a close that was never recorded."""

    code = "unknown_close"


def strict_int(value, minimum, maximum, field):
    if type(value) is not int or isinstance(value, bool):
        raise ActivityError(
            f"{field} must be a strict integer in [{minimum},{maximum}]"
        )
    if not minimum <= value <= maximum:
        raise ActivityError(
            f"{field} outside allowed bounds [{minimum},{maximum}]"
        )
    return value


def _require_fields(obj, fields, context):
    if not isinstance(obj, dict):
        raise ActivityError(f"{context} must be an object")
    if set(obj) != set(fields):
        raise ActivityError(
            f"{context} expects exactly fields {sorted(fields)}, "
            f"got {sorted(obj)}"
        )


def _ident(value, field):
    try:
        return ident(value)
    except ValueError as exc:
        raise ActivityError(f"{field}: {exc}") from exc


def _array(value, context, limit=MAX_BATCH_ENTRIES):
    if not isinstance(value, list):
        raise ActivityError(f"{context} must be an array")
    if len(value) > limit:
        raise ActivityError(f"{context} allows at most {limit} entries")
    return value


def _op(value, context):
    if value not in ("put", "delete") or not isinstance(value, str):
        raise ActivityError(f"{context} op must be 'put' or 'delete'")
    return value


def _null_business_fields(raw, fields, context):
    for field in fields:
        if raw[field] is not None:
            raise ActivityError(
                f"{context} delete revision must set {field} to null"
            )


def _parse_event(raw, seen_ids):
    _require_fields(raw, EVENT_FIELDS, "event revision")
    event_id = _ident(raw["event_id"], "event_id")
    if event_id in seen_ids:
        raise ActivityError(
            "event_id appears more than once in one batch: " + event_id
        )
    seen_ids.add(event_id)
    revision = strict_int(raw["revision"], 1, MAX_REVISION, "event revision")
    op = _op(raw["op"], "event")
    if op == "delete":
        _null_business_fields(
            raw, ("account_id", "event_time", "amount_minor"), "event"
        )
        return {
            "event_id": event_id,
            "revision": revision,
            "op": op,
            "account_id": None,
            "event_time": None,
            "amount_minor": None,
        }
    return {
        "event_id": event_id,
        "revision": revision,
        "op": op,
        "account_id": _ident(raw["account_id"], "event account_id"),
        "event_time": strict_int(
            raw["event_time"], 0, MAX_TIME, "event_time"
        ),
        "amount_minor": strict_int(
            raw["amount_minor"], 0, MAX_AMOUNT, "amount_minor"
        ),
    }


def _parse_link(raw, seen_ids):
    _require_fields(raw, LINK_FIELDS, "link revision")
    link_id = _ident(raw["link_id"], "link_id")
    if link_id in seen_ids:
        raise ActivityError(
            "link_id appears more than once in one batch: " + link_id
        )
    seen_ids.add(link_id)
    revision = strict_int(raw["revision"], 1, MAX_REVISION, "link revision")
    op = _op(raw["op"], "link")
    if op == "delete":
        _null_business_fields(
            raw,
            ("left_account", "right_account", "valid_from", "valid_to"),
            "link",
        )
        return {
            "link_id": link_id,
            "revision": revision,
            "op": op,
            "left_account": None,
            "right_account": None,
            "valid_from": None,
            "valid_to": None,
        }
    left = _ident(raw["left_account"], "link left_account")
    right = _ident(raw["right_account"], "link right_account")
    if left == right:
        raise ActivityError(
            "link " + link_id + " must relate two distinct accounts"
        )
    valid_from = strict_int(raw["valid_from"], 0, MAX_TIME, "valid_from")
    valid_to = strict_int(raw["valid_to"], 0, MAX_TIME, "valid_to")
    if not valid_from < valid_to:
        raise ActivityError(
            "link " + link_id + " requires valid_from < valid_to"
        )
    return {
        "link_id": link_id,
        "revision": revision,
        "op": op,
        "left_account": left,
        "right_account": right,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def parse_apply_request(data):
    """Validate and normalize an apply request; array order never matters."""
    _require_fields(
        data, "request_id expected_generation events links".split(),
        "apply request",
    )
    request_id = _ident(data["request_id"], "request_id")
    expected = strict_int(
        data["expected_generation"], 0, MAX_GENERATION, "expected_generation"
    )
    events_raw = _array(data["events"], "events")
    links_raw = _array(data["links"], "links")
    if len(events_raw) + len(links_raw) == 0:
        raise ActivityError(
            "apply request requires at least one event or link revision"
        )
    if len(events_raw) + len(links_raw) > MAX_BATCH_ENTRIES:
        raise ActivityError(
            f"apply batch allows at most {MAX_BATCH_ENTRIES} entries total"
        )

    event_ids = set()
    events = [_parse_event(raw, event_ids) for raw in events_raw]
    link_ids = set()
    links = [_parse_link(raw, link_ids) for raw in links_raw]
    events.sort(key=lambda row: (row["event_id"].encode("utf-8"),
                                 row["revision"]))
    links.sort(key=lambda row: (row["link_id"].encode("utf-8"),
                                row["revision"]))
    return {
        "request_id": request_id,
        "expected_generation": expected,
        "events": events,
        "links": links,
    }


def parse_close_request(data):
    _require_fields(
        data, "close_id expected_generation start end".split(), "close request"
    )
    close_id = _ident(data["close_id"], "close_id")
    expected = strict_int(
        data["expected_generation"], 0, MAX_GENERATION, "expected_generation"
    )
    start = strict_int(data["start"], 0, MAX_TIME, "start")
    end = strict_int(data["end"], 0, MAX_TIME, "end")
    if not start < end:
        raise ActivityError("close period requires start < end")
    return {
        "close_id": close_id,
        "expected_generation": expected,
        "start": start,
        "end": end,
    }


def canonical_json(obj):
    """Semantic comparison bytes: sorted keys, compact separators, no NaN."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(normalized):
    """Stable request fingerprint over the normalized (order-independent)
    structure: a retry with reordered arrays is still the same request."""
    body = canonical_json(normalized)
    return hashlib.sha256(body).hexdigest()

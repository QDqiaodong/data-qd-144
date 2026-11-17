"""Command line interface for the offline energy audit.

Command::

    energy-audit --warehouse DB --source JSON --output JSON

The audit is read-only against the warehouse: it never creates a missing
database, never writes any table and never changes the schema.  Strict JSON
parsing (duplicate keys / NaN rejected) and the atomic, symlink-rejecting
report publication rule are reused from :mod:`cutting_stock.common`.  Any
failure prints ``<code>: <message>`` to stderr, exits with status 2 and leaves
a previously published report untouched.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from cutting_stock.common import save, strict_json_bytes

from . import api
from .validate import AuditError

DESCRIPTION = (
    "Offline finished-work-order energy attribution and "
    "metering-uncertainty audit (read-only against the warehouse)"
)


def _reject_same_path(left, right):
    """Reject identical input/output paths when both exist."""
    try:
        if left.resolve() == right.resolve():
            raise AuditError("input and output paths must differ")
    except FileNotFoundError:
        pass


def _cmd_audit(args):
    _reject_same_path(args.source, args.output)
    _reject_same_path(args.warehouse, args.output)
    raw = Path(args.source).read_bytes()
    data = strict_json_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    report = api.audit(str(args.warehouse), data, digest)
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="energy-audit",
                                     description=DESCRIPTION)
    parser.add_argument("--warehouse", type=Path, required=True,
                        help="existing stock warehouse database (read-only)")
    parser.add_argument("--source", type=Path, required=True,
                        help="meter/run/tariff/budget source JSON")
    parser.add_argument("--output", type=Path, required=True,
                        help="report JSON output path")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _cmd_audit(args)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        code = getattr(error, "code", "invalid_request")
        print(f"{code}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

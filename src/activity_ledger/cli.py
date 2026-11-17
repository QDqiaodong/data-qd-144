"""Command line interface for the offline customer-activity ledger.

Subcommands::

    activity-init   --db DB
    activity-apply  --db DB --request JSON
    activity-close  --db DB --request JSON
    activity-report --db DB --close-id ID --output JSON

Strict JSON (duplicate keys / NaN rejected), the shared identifier rule and the
atomic, symlink-rejecting publication rule are reused from
:mod:`cutting_stock.common`.  Contract violations print ``<code>: <message>``
to stderr and exit with status 2, leaving any previously published report
untouched.  The existing ``cutting-stock``, ``stock-*``, ``ap-*`` and
``energy-audit`` entry points are unchanged.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from cutting_stock.common import save, strict_json_bytes

from . import api
from .validate import ActivityError

DESCRIPTION = (
    "Offline auditable customer-activity ledger with bitemporal identity"
)


def _load_json(path):
    raw = Path(path).read_bytes()
    return strict_json_bytes(raw)


def _reject_same_path(left, right):
    """Reject identical input/output paths when both exist."""
    try:
        if Path(left).resolve() == Path(right).resolve():
            raise ActivityError("input and output paths must differ")
    except FileNotFoundError:
        pass


def _cmd_init(args):
    result = api.initialize(str(args.db))
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_apply(args):
    request = _load_json(args.request)
    result = api.apply_request(str(args.db), request)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_close(args):
    request = _load_json(args.request)
    result = api.close_period(str(args.db), request)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_report(args):
    _reject_same_path(args.db, args.output)
    report = api.build_report(str(args.db), args.close_id)
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("activity-init", help="create a new activity database")
    init.add_argument("--db", type=Path, required=True)
    init.set_defaults(func=_cmd_init)

    apply_p = sub.add_parser("activity-apply", help="apply one idempotent batch")
    apply_p.add_argument("--db", type=Path, required=True)
    apply_p.add_argument("--request", type=Path, required=True)
    apply_p.set_defaults(func=_cmd_apply)

    close_p = sub.add_parser("activity-close", help="freeze a period close")
    close_p.add_argument("--db", type=Path, required=True)
    close_p.add_argument("--request", type=Path, required=True)
    close_p.set_defaults(func=_cmd_close)

    report = sub.add_parser("activity-report", help="publish a report")
    report.add_argument("--db", type=Path, required=True)
    report.add_argument("--close-id", required=True)
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ActivityError, ValueError, TypeError, KeyError, OSError,
            OverflowError, RecursionError, sqlite3.Error) as error:
        code = getattr(error, "code", "invalid_request")
        print(f"{code}: {error}", file=sys.stderr)
        return 2


# Console-script entry points (hyphenated names in pyproject).
def init_main(argv=None):
    if argv is None:
        return main(["activity-init", *sys.argv[1:]])
    return main(argv)


def apply_main(argv=None):
    if argv is None:
        return main(["activity-apply", *sys.argv[1:]])
    return main(argv)


def close_main(argv=None):
    if argv is None:
        return main(["activity-close", *sys.argv[1:]])
    return main(argv)


def report_main(argv=None):
    if argv is None:
        return main(["activity-report", *sys.argv[1:]])
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

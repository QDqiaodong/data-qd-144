"""Command line interface for the offline AP reconciliation ledger.

Subcommands::

    ap-check   --source JSON --ledger DB --output JSON
    ap-post    --source JSON --ledger DB --proposal JSON --request-id ID
    ap-reverse --ledger DB --posting-id ID --request-id ID
    ap-report  --ledger DB --output JSON

Strict JSON, identifier rules and the atomic symlink-rejecting publication rule
are reused from :mod:`cutting_stock.common`.  Contract violations print
``<code>: <message>`` to stderr and exit with status 2, matching the existing
warehouse CLI.  ``ap-check`` opens a missing ledger read-only, so it never
creates the database file.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from cutting_stock.common import save, strict_json_bytes

from . import api
from .validate import APError

DESCRIPTION = (
    "Offline receipt-voucher / supplier-invoice reconciliation and posting"
)


def _load_json(path):
    raw = Path(path).read_bytes()
    return strict_json_bytes(raw), hashlib.sha256(raw).hexdigest()


def _reject_same_path(left, right):
    """Reject identical source/output or ledger/output paths when both exist."""
    try:
        if left.resolve() == right.resolve():
            raise APError("input and output paths must differ")
    except FileNotFoundError:
        pass


def _cmd_check(args):
    _reject_same_path(args.source, args.output)
    _reject_same_path(args.ledger, args.output)
    data, digest = _load_json(args.source)
    report = api.check(str(args.ledger), data, digest)
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_post(args):
    data, digest = _load_json(args.source)
    proposal, _ = _load_json(args.proposal)
    result = api.post(
        str(args.ledger), data, digest, proposal, args.request_id
    )
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_reverse(args):
    result = api.reverse(str(args.ledger), args.posting_id, args.request_id)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_report(args):
    _reject_same_path(args.ledger, args.output)
    report = api.build_report(str(args.ledger))
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("ap-check", help="match receipts against invoices")
    check.add_argument("--source", type=Path, required=True)
    check.add_argument("--ledger", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.set_defaults(func=_cmd_check)

    post = sub.add_parser("ap-post", help="atomically post a unique proposal")
    post.add_argument("--source", type=Path, required=True)
    post.add_argument("--ledger", type=Path, required=True)
    post.add_argument("--proposal", type=Path, required=True)
    post.add_argument("--request-id", required=True)
    post.set_defaults(func=_cmd_post)

    reverse = sub.add_parser("ap-reverse", help="reverse one full posting")
    reverse.add_argument("--ledger", type=Path, required=True)
    reverse.add_argument("--posting-id", type=int, required=True)
    reverse.add_argument("--request-id", required=True)
    reverse.set_defaults(func=_cmd_reverse)

    report = sub.add_parser("ap-report", help="publish deterministic report")
    report.add_argument("--ledger", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        code = getattr(error, "code", "invalid_request")
        print(f"{code}: {error}", file=sys.stderr)
        return 2


# Console-script style entry points (hyphenated names in pyproject).
def check_main(argv=None):
    return main(["ap-check", *sys.argv[1:]]) if argv is None else main(argv)


def post_main(argv=None):
    return main(["ap-post", *sys.argv[1:]]) if argv is None else main(argv)


def reverse_main(argv=None):
    return main(["ap-reverse", *sys.argv[1:]]) if argv is None else main(argv)


def report_main(argv=None):
    return main(["ap-report", *sys.argv[1:]]) if argv is None else main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

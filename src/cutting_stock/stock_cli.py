"""CLI for the offline bounded cutting-plan warehouse.

Subcommands::

    stock-init   --db DB --source JSON
    stock-apply  --db DB --request JSON
    stock-report --db DB --output JSON

The original single-shot ``cutting-stock --source/--output`` FFD contract in
:mod:`cutting_stock.cli` is unchanged.  Report publication reuses the shared
atomic, symlink-rejecting :func:`cutting_stock.common.save` rule, and all
inputs use the same strict JSON parser (duplicate keys / NaN rejected).
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import stock_api
from .common import save, strict_json_bytes

DESCRIPTION = "Offline bounded cutting-plan warehouse (exact optimizer)"


def _load_json(path):
    raw = Path(path).read_bytes()
    return strict_json_bytes(raw), hashlib.sha256(raw).hexdigest()


def _cmd_init(args):
    data, digest = _load_json(args.source)
    result = stock_api.initialize(str(args.db), data, source_sha256=digest)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_apply(args):
    request, _ = _load_json(args.request)
    result = stock_api.apply_request(str(args.db), request)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_report(args):
    report = stock_api.build_report(str(args.db))
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("stock-init", help="create a new warehouse database")
    init.add_argument("--db", type=Path, required=True)
    init.add_argument("--source", type=Path, required=True)
    init.set_defaults(func=_cmd_init)

    apply_p = sub.add_parser("stock-apply", help="apply one idempotent batch")
    apply_p.add_argument("--db", type=Path, required=True)
    apply_p.add_argument("--request", type=Path, required=True)
    apply_p.set_defaults(func=_cmd_apply)

    report = sub.add_parser("stock-report", help="publish deterministic report")
    report.add_argument("--db", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "stock-report":
        try:
            if args.db.resolve() == args.output.resolve():
                raise ValueError("db and output must differ")
        except FileNotFoundError:
            pass
    try:
        return args.func(args)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        code = getattr(error, "code", "invalid_request")
        print(f"{code}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

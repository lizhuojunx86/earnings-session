"""Command line: classify, batch, audit."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from earnings_session import __version__
from earnings_session.audit import audit_rows, render_text
from earnings_session.batch import (
    InputError,
    classify_events,
    read_events,
    read_rows,
    stderr_progress,
    write_rows,
)
from earnings_session.core import Classifier
from earnings_session.edgar import (
    DEFAULT_MAX_RPS,
    USER_AGENT_ENV,
    EdgarAccessError,
    UserAgentError,
    default_cache_dir,
)

EXIT_USAGE = 2
EXIT_EDGAR = 3


def _strict(value: Any) -> Any:
    """NaN -> None, so the JSON output is strict."""
    if isinstance(value, dict):
        return {k: _strict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value


def _edgar_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("SEC access")
    g.add_argument(
        "--user-agent",
        help=f'"Your Name you@your-domain.com" (default: ${USER_AGENT_ENV}); SEC requires one',
    )
    g.add_argument(
        "--cache-dir",
        type=Path,
        help=f"response cache (default: {default_cache_dir()})",
    )
    g.add_argument("--no-cache", action="store_true", help="do not read or write the cache")
    g.add_argument(
        "--max-rps",
        type=float,
        default=DEFAULT_MAX_RPS,
        help=f"requests per second, at most 10 (default: {DEFAULT_MAX_RPS:g})",
    )


def _classifier(args: argparse.Namespace) -> Classifier:
    return Classifier(
        args.user_agent,
        cache=not args.no_cache,
        cache_dir=args.cache_dir,
        max_rps=args.max_rps,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="earnings-session",
        description="When did an earnings release go out? Pre-open, intraday or post-close, "
        "from the SEC EDGAR acceptance time of its 8-K.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("classify", help="classify one event, JSON to stdout")
    c.add_argument("--ticker", required=True)
    c.add_argument("--date", required=True, help="announcement date, YYYY-MM-DD")
    c.add_argument("--entry-date", help="backtest entry date (entry at that day's open)")
    c.add_argument("--label", choices=("bmo", "amc", "dmh"), help="the vendor's timing label")
    _edgar_options(c)

    b = sub.add_parser("batch", help="classify every row of a CSV")
    b.add_argument("input", type=Path, help="CSV: ticker, event_date[, entry_date, vendor_label]")
    b.add_argument("output", type=Path, help="CSV to write")
    _edgar_options(b)

    a = sub.add_parser("audit", help="vendor labels vs EDGAR, with Wilson 95%% intervals")
    a.add_argument(
        "input",
        type=Path,
        help="batch output CSV (a plain input CSV is classified first)",
    )
    a.add_argument("--json", action="store_true", help="print the report as JSON")
    _edgar_options(a)
    return p


def _run(args: argparse.Namespace) -> int:
    if args.command == "classify":
        with _classifier(args) as clf:
            res = clf.classify(args.ticker, args.date, args.entry_date, args.label)
        print(json.dumps(res.to_dict(), indent=2))
        return 0

    if args.command == "batch":
        header, events = read_events(args.input)
        with _classifier(args) as clf, args.output.open("w", newline="", encoding="utf-8") as out:
            pairs = classify_events(clf, events, stderr_progress())
            write_rows(out, header, pairs)
            print(
                f"wrote {len(events)} rows to {args.output} "
                f"(requests {clf.client.requests}, cache hits {clf.client.cache_hits}, "
                f"failures {clf.client.failures})",
                file=sys.stderr,
            )
        return 0

    rows = read_rows(args.input)
    if rows and "verdict" not in rows[0]:
        header, events = read_events(args.input)
        print(
            f"{args.input} has no verdict column: classifying {len(events)} events first "
            "(run batch to keep the rows)",
            file=sys.stderr,
        )
        with _classifier(args) as clf:
            rows = write_rows(
                io.StringIO(), header, classify_events(clf, events, stderr_progress())
            )
    report = audit_rows(rows)
    if args.json:
        print(json.dumps(_strict(report), indent=2))
    else:
        print(render_text(report))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except UserAgentError as exc:
        print(f"earnings-session: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (InputError, ValueError) as exc:
        print(f"earnings-session: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except EdgarAccessError as exc:
        print(f"earnings-session: {exc}", file=sys.stderr)
        return EXIT_EDGAR


if __name__ == "__main__":
    sys.exit(main())

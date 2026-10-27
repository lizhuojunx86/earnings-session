"""Command line: classify, batch, audit, bars."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from earnings_session import __version__
from earnings_session import bars as bars_mod
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

EXIT_FLAGGED = 1
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
        "from the SEC EDGAR acceptance time of its 8-K. The bars command checks, offline, that "
        "a backtest's price bars sit on the days the backtest says they do.",
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

    s = sub.add_parser(
        "bars",
        help="check that a backtest's price bars sit on their target days (offline)",
    )
    s.add_argument(
        "input",
        type=Path,
        help="CSV: ticker, entry_date[, exit_date, return, entry_bar_date, exit_bar_date]",
    )
    s.add_argument(
        "--prices",
        type=Path,
        help="the price table the backtest used: ticker, date (other columns ignored); "
        "without it the trade file's entry_bar_date / exit_bar_date are checked",
    )
    s.add_argument("-o", "--output", type=Path, help="write the checked rows to this CSV")
    s.add_argument(
        "--entry-side",
        choices=bars_mod.SIDES,
        default="after",
        help="entry bar: first on or after entry_date (default) or last on or before it",
    )
    s.add_argument(
        "--exit-side",
        choices=bars_mod.SIDES,
        default="before",
        help="exit bar: last on or before exit_date (default) or first on or after it",
    )
    s.add_argument(
        "--max-gap",
        type=int,
        default=bars_mod.DEFAULT_MAX_GAP,
        metavar="N",
        help=f"trading days a bar may sit from its target (default: {bars_mod.DEFAULT_MAX_GAP})",
    )
    s.add_argument(
        "--break-days",
        type=int,
        default=bars_mod.DEFAULT_BREAK_DAYS,
        metavar="N",
        help="trading days beyond which a bar counts as another stretch of history "
        f"(default: {bars_mod.DEFAULT_BREAK_DAYS})",
    )
    s.add_argument("--json", action="store_true", help="print the report as JSON")
    s.add_argument(
        "--fail-on-flag",
        action="store_true",
        help=f"exit {EXIT_FLAGGED} when any row is flagged (for CI)",
    )
    return p


def _run_bars(args: argparse.Namespace) -> int:
    entry_side = bars_mod.as_side(args.entry_side)
    exit_side = bars_mod.as_side(args.exit_side)
    if args.max_gap < 0 or args.break_days < 0:
        raise InputError("--max-gap and --break-days must be 0 or more")
    header, trades = bars_mod.read_trades(args.input)
    lower = {h.lower() for h in header}
    prices = bars_mod.read_prices(args.prices) if args.prices else None
    if prices is None and "entry_bar_date" not in lower:
        raise InputError(
            f"{args.input}: no entry_bar_date column; pass --prices with the price table "
            "the backtest used"
        )
    checks = bars_mod.check_trades(
        trades,
        prices,
        entry_side=entry_side,
        exit_side=exit_side,
        max_gap=args.max_gap,
        break_days=args.break_days,
    )
    if args.output is not None:
        with args.output.open("w", newline="", encoding="utf-8") as out:
            bars_mod.write_rows(out, header, checks)
    report = bars_mod.summarize(
        checks,
        prices=prices,
        has_returns="return" in lower,
        entry_side=entry_side,
        exit_side=exit_side,
        max_gap=args.max_gap,
        break_days=args.break_days,
    )
    if args.json:
        print(json.dumps(_strict(report), indent=2))
    else:
        print(bars_mod.render_text(report))
    if args.fail_on_flag and any(c.flag != bars_mod.OK for c in checks):
        return EXIT_FLAGGED
    return 0


def _run(args: argparse.Namespace) -> int:
    if args.command == "bars":
        return _run_bars(args)

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

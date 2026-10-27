"""Check that a backtest's price bars sit on the days the backtest says they do.

A return built from "the first bar on or after the entry date" with no limit on
the distance takes whatever bar comes next. For a ticker with no price history
near the target that can be months or years later, and the trade is booked in
the wrong period without any error. This module measures the distance for every
trade in NYSE trading days and flags the rows a reviewer should look at.

Offline: it reads two CSV files and never goes on the network.
"""

from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Any, Literal, TextIO

from earnings_session.audit import share
from earnings_session.batch import InputError
from earnings_session.calendar import (
    calendar_bounds,
    in_range,
    next_trading_day,
    prev_trading_day,
    trading_days,
)

Side = Literal["after", "before"]
SIDES: tuple[Side, ...] = ("after", "before")

EARLY_BAR = "early_bar"
HISTORY_BREAK = "history_break"
NO_BAR = "no_bar"
ENTRY_GAP = "entry_gap"
EXIT_GAP = "exit_gap"
OK = "ok"
FLAGS = (EARLY_BAR, HISTORY_BREAK, NO_BAR, ENTRY_GAP, EXIT_GAP, OK)  # most serious first

DEFAULT_MAX_GAP = 0
DEFAULT_BREAK_DAYS = 250

BAR_COLUMNS = (
    "entry_target", "entry_bar_date", "entry_gap_td", "entry_gap_cd",
    "exit_target", "exit_bar_date", "exit_gap_td", "exit_gap_cd",
    "flag", "notes",
)  # fmt: skip

# |gap| in trading days -> bucket label; the last bucket is open-ended
GAP_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0", 0, 0),
    ("1", 1, 1),
    ("2-5", 2, 5),
    ("6-20", 6, 20),
    ("21-250", 21, 250),
    (">250", 251, None),
)


def as_side(value: str) -> Side:
    if value == "after":
        return "after"
    if value == "before":
        return "before"
    raise ValueError(f"side must be 'after' or 'before', not {value!r}")


def trading_day_gap(target: date, bar: date, side: Side) -> int:
    """Signed distance, in NYSE trading days, between the bar a rule should find and ``bar``.

    The bar a rule should find is the first trading day on or after ``target`` (side
    ``"after"``) or the last one on or before it (``"before"``). 0 means ``bar`` is that day;
    positive means later, negative earlier. A trading day with no bar counts as a skipped
    day: a Friday target priced from a Monday bar is 1 day late.
    """
    days = trading_days()
    if side == "after":
        if bar >= target:
            return bisect_left(days, bar) - bisect_left(days, target)
        return -(bisect_left(days, target) - bisect_left(days, bar))
    if bar <= target:
        return -(bisect_right(days, target) - bisect_right(days, bar))
    return bisect_right(days, bar) - bisect_right(days, target)


def find_bar(
    dates: Sequence[date],
    target: date,
    side: Side = "after",
    max_gap_td: int | None = None,
) -> date | None:
    """The bar a backtest would take for ``target`` from ``dates`` (sorted ascending).

    Side ``"after"``: the first date on or after ``target``. Side ``"before"``: the last date
    on or before it. With ``max_gap_td``, a bar more than that many trading days from where
    it should be is not returned: None, so the caller has to decide what to do.
    """
    if side == "after":
        i = bisect_left(dates, target)
        if i == len(dates):
            return None
        bar = dates[i]
    elif side == "before":
        i = bisect_right(dates, target)
        if i == 0:
            return None
        bar = dates[i - 1]
    else:
        raise ValueError(f"side must be 'after' or 'before', not {side!r}")
    if max_gap_td is not None and abs(trading_day_gap(target, bar, side)) > max_gap_td:
        return None
    return bar


def expected_day(target: date, side: Side) -> date:
    """The trading day a rule with this side should price ``target`` on."""
    days = trading_days()
    if side == "after":
        i = bisect_left(days, target)
        if i == len(days):
            return next_trading_day(target)  # raises: outside the calendar
        return days[i]
    i = bisect_right(days, target)
    if i == 0:
        return prev_trading_day(target)  # raises: outside the calendar
    return days[i - 1]


def parse_date(value: str) -> date:
    """YYYY-MM-DD, also when a time follows it ("2024-01-02 00:00:00", "2024-01-02T16:00")."""
    text = value.strip()
    for sep in ("T", " "):
        if sep in text:
            text = text.split(sep, 1)[0]
    return date.fromisoformat(text)


@dataclass(frozen=True)
class PriceTable:
    dates: dict[str, list[date]]  # ticker -> sorted, de-duplicated bar dates
    rows: int
    duplicates: int
    out_of_range: int
    source: str = ""

    @property
    def tickers(self) -> int:
        return len(self.dates)


def read_prices(path: Path) -> PriceTable:
    """Bar dates per ticker from a price table with ``ticker`` and ``date`` columns."""
    lo, hi = calendar_bounds()
    seen: dict[str, set[date]] = {}
    rows = duplicates = out_of_range = 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = [h.strip().lower() for h in next(reader, [])]
        missing = [c for c in ("ticker", "date") if c not in header]
        if missing:
            raise InputError(f"{path}: missing column(s) {', '.join(missing)}")
        ti, di = header.index("ticker"), header.index("date")
        for line, rec in enumerate(reader, start=2):
            if not any(cell.strip() for cell in rec):
                continue
            if len(rec) <= max(ti, di):
                raise InputError(f"{path}:{line}: short row")
            ticker = rec[ti].strip().upper()
            if not ticker:
                raise InputError(f"{path}:{line}: empty ticker")
            try:
                d = parse_date(rec[di])
            except ValueError:
                raise InputError(f"{path}:{line}: bad date {rec[di]!r}; use YYYY-MM-DD") from None
            rows += 1
            if not lo <= d <= hi:
                out_of_range += 1
                continue
            bucket = seen.setdefault(ticker, set())
            if d in bucket:
                duplicates += 1
            else:
                bucket.add(d)
    return PriceTable(
        {t: sorted(s) for t, s in seen.items()}, rows, duplicates, out_of_range, str(path)
    )


@dataclass(frozen=True)
class TradeInput:
    line: int  # 1-based line in the file, header = 1
    ticker: str
    entry_date: date
    exit_date: date | None = None
    entry_bar_date: date | None = None
    exit_bar_date: date | None = None
    ret: float | None = None
    ret_given: bool = False  # the row had a non-empty return value
    raw: dict[str, str] = field(default_factory=dict)


def _target(path: Path, line: int, name: str, text: str) -> date | None:
    if not text:
        return None
    try:
        d = parse_date(text)
    except ValueError:
        raise InputError(f"{path}:{line}: bad {name} {text!r}; use YYYY-MM-DD") from None
    if not in_range(d):
        lo, hi = calendar_bounds()
        raise InputError(f"{path}:{line}: {name} {d} is outside the calendar ({lo} to {hi})")
    return d


def _bar_date(path: Path, line: int, name: str, text: str) -> date | None:
    if not text:
        return None
    try:
        return parse_date(text)
    except ValueError:
        raise InputError(f"{path}:{line}: bad {name} {text!r}; use YYYY-MM-DD") from None


def _return(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if isfinite(value) else None


def read_trades(path: Path) -> tuple[list[str], list[TradeInput]]:
    """(header, trades). Column names match case-insensitively; every row is checked."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = [(h or "").strip() for h in reader.fieldnames or []]
        cols = {h.lower(): h for h in header}
        missing = [c for c in ("ticker", "entry_date") if c not in cols]
        if missing:
            raise InputError(f"{path}: missing column(s) {', '.join(missing)}")
        trades: list[TradeInput] = []
        for line, rec in enumerate(reader, start=2):
            row = {(k or "").strip(): (v or "").strip() for k, v in rec.items()}

            def get(name: str, row: dict[str, str] = row) -> str:
                return row.get(cols.get(name, ""), "")

            if not any(row.values()):
                continue
            ticker = get("ticker").upper()
            if not ticker:
                raise InputError(f"{path}:{line}: empty ticker")
            entry = _target(path, line, "entry_date", get("entry_date"))
            if entry is None:
                raise InputError(f"{path}:{line}: empty entry_date")
            exit_ = _target(path, line, "exit_date", get("exit_date"))
            if exit_ is not None and exit_ < entry:
                raise InputError(f"{path}:{line}: exit_date {exit_} is before entry_date {entry}")
            ret_text = get("return")
            trades.append(
                TradeInput(
                    line=line,
                    ticker=ticker,
                    entry_date=entry,
                    exit_date=exit_,
                    entry_bar_date=_bar_date(path, line, "entry_bar_date", get("entry_bar_date")),
                    exit_bar_date=_bar_date(path, line, "exit_bar_date", get("exit_bar_date")),
                    ret=_return(ret_text) if ret_text else None,
                    ret_given=bool(ret_text),
                    raw=row,
                )
            )
    return header, trades


@dataclass
class BarCheck:
    trade: TradeInput
    entry_target: date
    entry_bar: date | None = None
    entry_gap_td: int | None = None
    entry_gap_cd: int | None = None
    exit_target: date | None = None
    exit_bar: date | None = None
    exit_gap_td: int | None = None
    exit_gap_cd: int | None = None
    flag: str = OK
    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, str]:
        def text(v: object) -> str:
            return "" if v is None else str(v)

        return {
            "entry_target": self.entry_target.isoformat(),
            "entry_bar_date": text(self.entry_bar),
            "entry_gap_td": text(self.entry_gap_td),
            "entry_gap_cd": text(self.entry_gap_cd),
            "exit_target": text(self.exit_target),
            "exit_bar_date": text(self.exit_bar),
            "exit_gap_td": text(self.exit_gap_td),
            "exit_gap_cd": text(self.exit_gap_cd),
            "flag": self.flag,
            "notes": "; ".join(self.notes),
        }


def _leg(
    leg: str,
    target: date,
    side: Side,
    dates: Sequence[date] | None,
    used: date | None,
    from_prices: bool,
    notes: list[str],
) -> tuple[date, date | None, int | None, int | None]:
    """(expected day, bar, gap_td, gap_cd) for the entry or the exit."""
    expected = expected_day(target, side)
    if expected != target:
        notes.append(f"{leg}_date {target} is not a trading day; target {expected}")
    if from_prices:
        if dates is None:
            return expected, None, None, None
        bar = find_bar(dates, target, side)
        if bar is None:
            word = "on or after" if side == "after" else "on or before"
            notes.append(f"no bar {word} {leg}_date")
            return expected, None, None, None
    else:
        if used is None:
            notes.append(f"no {leg}_bar_date")
            return expected, None, None, None
        bar = used
    return expected, bar, trading_day_gap(target, bar, side), (bar - target).days


def check_trades(
    trades: Iterable[TradeInput],
    prices: PriceTable | None = None,
    *,
    entry_side: Side = "after",
    exit_side: Side = "before",
    max_gap: int = DEFAULT_MAX_GAP,
    break_days: int = DEFAULT_BREAK_DAYS,
) -> list[BarCheck]:
    """One check per trade. With ``prices`` the bars are looked up there; without, the
    trade's own ``entry_bar_date`` / ``exit_bar_date`` (what the backtest recorded) are used.
    """
    if max_gap < 0 or break_days < 0:
        raise ValueError("max_gap and break_days must be >= 0")
    from_prices = prices is not None
    out: list[BarCheck] = []
    for t in trades:
        notes: list[str] = []
        dates = prices.dates.get(t.ticker) if prices is not None else None
        if from_prices and dates is None:
            notes.append("ticker not in the price table")
        e_target, e_bar, e_td, e_cd = _leg(
            "entry", t.entry_date, entry_side, dates, t.entry_bar_date, from_prices, notes
        )
        check = BarCheck(t, e_target, e_bar, e_td, e_cd, notes=notes)
        if t.exit_date is not None:
            if from_prices or t.exit_bar_date is not None:
                check.exit_target, check.exit_bar, check.exit_gap_td, check.exit_gap_cd = _leg(
                    "exit", t.exit_date, exit_side, dates, t.exit_bar_date, from_prices, notes
                )
            else:
                notes.append("exit not checked: no exit_bar_date")
        elif t.exit_bar_date is not None:
            notes.append("exit not checked: exit_bar_date without exit_date")
        if (
            check.entry_bar is not None
            and check.exit_bar is not None
            and check.exit_bar < check.entry_bar
        ):
            notes.append("exit bar is before the entry bar")

        hits: list[str] = []
        if not from_prices and entry_side == "after" and e_bar is not None and e_bar < t.entry_date:
            hits.append(EARLY_BAR)
        gaps = [g for g in (check.entry_gap_td, check.exit_gap_td) if g is not None]
        if any(abs(g) > break_days for g in gaps):
            hits.append(HISTORY_BREAK)
        if e_bar is None or (check.exit_target is not None and check.exit_bar is None):
            hits.append(NO_BAR)
        if check.entry_gap_td is not None and abs(check.entry_gap_td) > max_gap:
            hits.append(ENTRY_GAP)
        if check.exit_gap_td is not None and abs(check.exit_gap_td) > max_gap:
            hits.append(EXIT_GAP)
        if hits:
            hits.sort(key=FLAGS.index)
            check.flag = hits[0]
            if len(hits) > 1:
                notes.append("also " + ", ".join(hits[1:]))
        out.append(check)
    return out


def _buckets(gaps: Sequence[int | None]) -> dict[str, int]:
    counts = {label: 0 for label, _, _ in GAP_BUCKETS}
    counts["no bar"] = 0
    for g in gaps:
        if g is None:
            counts["no bar"] += 1
            continue
        a = abs(g)
        for label, lo, hi in GAP_BUCKETS:
            if a >= lo and (hi is None or a <= hi):
                counts[label] += 1
                break
    return counts


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _returns(checks: Sequence[BarCheck]) -> dict[str, Any]:
    flagged = [c.trade.ret for c in checks if c.flag != OK and c.trade.ret is not None]
    clean = [c.trade.ret for c in checks if c.flag == OK and c.trade.ret is not None]
    invalid = sum(c.trade.ret_given and c.trade.ret is None for c in checks)
    total = sum(flagged) + sum(clean)
    return {
        "n_valid": len(flagged) + len(clean),
        "n_invalid": invalid,
        "flagged": {"n": len(flagged), "mean": _mean(flagged), "sum": sum(flagged)},
        "ok": {"n": len(clean), "mean": _mean(clean), "sum": sum(clean)},
        "flagged_share_of_total": sum(flagged) / total if total > 0 else None,
    }


def summarize(
    checks: Sequence[BarCheck],
    *,
    prices: PriceTable | None = None,
    has_returns: bool = False,
    entry_side: Side = "after",
    exit_side: Side = "before",
    max_gap: int = DEFAULT_MAX_GAP,
    break_days: int = DEFAULT_BREAK_DAYS,
) -> dict[str, Any]:
    n = len(checks)
    counts = Counter(c.flag for c in checks)
    exits = [c for c in checks if c.exit_target is not None]
    report: dict[str, Any] = {
        "n_trades": n,
        "bars_from": "prices" if prices is not None else "trades",
        "rules": {
            "entry_side": entry_side,
            "exit_side": exit_side,
            "max_gap": max_gap,
            "break_days": break_days,
        },
        "flags": {f: share(counts[f], n) for f in FLAGS},
        "entry_gap_td": _buckets([c.entry_gap_td for c in checks]),
        "exit_gap_td": _buckets([c.exit_gap_td for c in exits]) if exits else None,
        "returns": _returns(checks) if has_returns else None,
        "prices": None,
    }
    if prices is not None:
        report["prices"] = {
            "source": prices.source,
            "rows": prices.rows,
            "tickers": prices.tickers,
            "duplicates": prices.duplicates,
            "out_of_range": prices.out_of_range,
        }
    return report


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.4g}"


def render_text(report: dict[str, Any]) -> str:
    rules = report["rules"]
    entry_rule = (
        "first bar on or after" if rules["entry_side"] == "after" else "last bar on or before"
    )
    exit_rule = (
        "last bar on or before" if rules["exit_side"] == "before" else "first bar on or after"
    )
    lines = [f"trades: {report['n_trades']}"]
    p = report["prices"]
    if p is not None:
        extra = []
        if p["duplicates"]:
            extra.append(f"{p['duplicates']} duplicate rows dropped")
        if p["out_of_range"]:
            extra.append(f"{p['out_of_range']} rows outside the calendar ignored")
        tail = f"; {', '.join(extra)}" if extra else ""
        lines.append(f"bars from {p['source']}: {p['rows']} rows, {p['tickers']} tickers{tail}")
    else:
        lines.append("bars from the trade file (entry_bar_date, exit_bar_date)")
    lines.append(
        f"entry = {entry_rule} entry_date; exit = {exit_rule} exit_date; "
        f"max gap {rules['max_gap']} trading days; history break > {rules['break_days']}"
    )
    if not report["n_trades"]:
        return "\n".join([*lines, "", "no trades"])
    lines += ["", "flags (Wilson 95%)"]
    for f, s in report["flags"].items():
        if not s["k"] and f != OK:
            continue
        lo, hi = s["wilson95"]
        lines.append(f"  {f:<14}{s['k']}/{s['n']} = {_pct(s['share'])} ({_pct(lo)}-{_pct(hi)})")
    lines += ["", "distance from target, trading days"]
    for key, title in (("entry_gap_td", "entry"), ("exit_gap_td", "exit")):
        buckets = report[key]
        if buckets is None:
            continue
        cells = ", ".join(f"{label}: {k}" for label, k in buckets.items())
        lines.append(f"  {title:<6}{cells}")
    r = report["returns"]
    if r is not None:
        lines += ["", "returns (the 'return' column)"]
        for key in ("flagged", "ok"):
            g = r[key]
            lines.append(f"  {key:<8} n {g['n']}, mean {_num(g['mean'])}, sum {_num(g['sum'])}")
        if r["flagged_share_of_total"] is not None:
            lines.append(f"  flagged rows carry {_pct(r['flagged_share_of_total'])} of the total")
        if r["n_invalid"]:
            lines.append(f"  {r['n_invalid']} rows with a non-numeric return were left out")
    return "\n".join(lines)


def output_header(input_header: Sequence[str]) -> list[str]:
    return [h for h in input_header if h not in BAR_COLUMNS] + list(BAR_COLUMNS)


def write_rows(out: TextIO, input_header: Sequence[str], checks: Iterable[BarCheck]) -> None:
    writer = csv.DictWriter(out, fieldnames=output_header(input_header), extrasaction="ignore")
    writer.writeheader()
    for c in checks:
        writer.writerow({**c.trade.raw, **c.to_row()})

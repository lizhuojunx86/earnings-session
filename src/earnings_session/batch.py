"""CSV in, CSV out: one classified row per input event."""

from __future__ import annotations

import csv
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

from earnings_session.core import CSV_COLUMNS, Classifier, EventResult, as_date

REQUIRED = ("ticker", "event_date")
OPTIONAL = ("entry_date", "vendor_label")


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class EventInput:
    line: int  # 1-based line in the file, header = 1
    ticker: str
    event_date: date
    entry_date: date | None
    vendor_label: str | None
    raw: dict[str, str]


def read_events(path: Path) -> tuple[list[str], list[EventInput]]:
    """(header, events). Every row is checked before anything is fetched."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in reader.fieldnames or []]
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            raise InputError(f"{path}: missing column(s) {', '.join(missing)}")
        events: list[EventInput] = []
        for i, raw in enumerate(reader, start=2):
            row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            ticker = row.get("ticker", "")
            if not ticker:
                raise InputError(f"{path}:{i}: empty ticker")
            try:
                ev = as_date(row["event_date"])
                en = as_date(row["entry_date"]) if row.get("entry_date") else None
            except ValueError as exc:
                raise InputError(f"{path}:{i}: bad date ({exc}); use YYYY-MM-DD") from None
            label = row.get("vendor_label") or None
            events.append(EventInput(i, ticker, ev, en, label, row))
    return header, events


def classify_events(
    classifier: Classifier,
    events: list[EventInput],
    progress: Callable[[int, int], None] | None = None,
) -> Iterator[tuple[EventInput, EventResult]]:
    for k, e in enumerate(events, start=1):
        yield e, classifier.classify(e.ticker, e.event_date, e.entry_date, e.vendor_label)
        if progress is not None:
            progress(k, len(events))


def output_header(input_header: list[str]) -> list[str]:
    return [h for h in input_header if h not in CSV_COLUMNS] + list(CSV_COLUMNS)


def write_rows(
    out: TextIO, input_header: list[str], pairs: Iterator[tuple[EventInput, EventResult]]
) -> list[dict[str, str]]:
    """Write the classified rows as they come in; returns them as well."""
    header = output_header(input_header)
    writer = csv.DictWriter(out, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    rows = []
    for e, r in pairs:
        row = {**e.raw, **r.to_row()}
        writer.writerow(row)
        out.flush()
        rows.append(row)
    return rows


def stderr_progress(every: int = 25) -> Callable[[int, int], None]:
    def report(k: int, n: int) -> None:
        if k % every == 0 or k == n:
            print(f"  classified {k}/{n}", file=sys.stderr, flush=True)

    return report


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [
            {(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)
        ]

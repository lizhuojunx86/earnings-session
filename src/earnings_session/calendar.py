"""NYSE trading calendar (exchange_calendars XNYS).

Columbus Day and Veterans Day are ordinary NYSE trading days; any calendar
that treats them as holidays will misplace acceptances on those dates.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals

CALENDAR_START = "1995-01-01"

# Unscheduled full-day closures (national days of mourning). XNYS lists both;
# they are kept here as well so a calendar release that drops one is caught by
# the test suite instead of silently moving acceptances.
SPECIAL_CLOSURES = frozenset({date(2018, 12, 5), date(2025, 1, 9)})


@lru_cache(maxsize=1)
def _xnys() -> Any:
    return xcals.get_calendar("XNYS", start=CALENDAR_START)


def calendar_bounds() -> tuple[date, date]:
    cal = _xnys()
    return cal.first_session.date(), cal.last_session.date()


def in_range(d: date) -> bool:
    """True when ``d`` can be looked up (one week of slack inside the calendar's ends)."""
    lo, hi = calendar_bounds()
    # one week of slack each side so prev/next lookups at the edges still work
    return lo + timedelta(days=7) <= d <= hi - timedelta(days=7)


def _check(d: date) -> None:
    if not in_range(d):
        lo, hi = calendar_bounds()
        raise ValueError(f"{d} is outside the XNYS calendar range ({lo} to {hi})")


@lru_cache(maxsize=1)
def trading_days() -> tuple[date, ...]:
    """Every NYSE trading day in the calendar range, ascending."""
    sessions = (ts.date() for ts in _xnys().sessions)
    return tuple(d for d in sessions if d not in SPECIAL_CLOSURES)


def is_trading_day(d: date) -> bool:
    _check(d)
    return bool(_xnys().is_session(d.isoformat())) and d not in SPECIAL_CLOSURES


def next_trading_day(d: date) -> date:
    """First trading day >= d."""
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    """Last trading day <= d."""
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def session_open(d: date) -> datetime:
    """Open of trading day ``d`` as an aware UTC datetime."""
    if not is_trading_day(d):
        raise ValueError(f"{d} is not an NYSE trading day")
    ts: datetime = _xnys().session_open(d.isoformat()).to_pydatetime()
    return ts


def session_close(d: date) -> datetime:
    """Close of trading day ``d`` (16:00 ET, 13:00 ET on early-close days), aware UTC."""
    if not is_trading_day(d):
        raise ValueError(f"{d} is not an NYSE trading day")
    ts: datetime = _xnys().session_close(d.isoformat()).to_pydatetime()
    return ts


def is_early_close(d: date) -> bool:
    if not is_trading_day(d):
        return False
    return d in _early_close_dates()


@lru_cache(maxsize=1)
def _early_close_dates() -> frozenset[date]:
    return frozenset(ts.date() for ts in _xnys().early_closes)

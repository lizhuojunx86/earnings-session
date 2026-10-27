"""Session classification and filing selection. Pure functions, no I/O.

Timing model
------------
An earnings 8-K (Item 2.02) is furnished together with, or after, the press
release, so its EDGAR acceptance time is an UPPER bound on when the release
became public. The ``acceptanceDateTime`` of the submissions API cannot be
trusted for the zone: every value carries a "Z", but for some filings it is
UTC (Apple's earnings 8-Ks, 16:30 ET, read 20:30Z / 21:30Z) and for others the
US/Eastern wall clock (ARNA 2016-05-09 "16:18:17Z" is "Accepted 16:18:17" on
its index page). The filing index page's ET "Accepted" line is the reference;
``zone_convention`` records which reading the API value matches. The
acceptance ET *date* is right under either reading, because EDGAR only
accepts filings between 06:00 and 22:00 ET.

``classify_acceptance`` labels an acceptance by NYSE session: pre_open
(before 09:30 ET), intraday (09:30 to the close), post_close (at or after the
close, 16:00 ET or 13:00 ET on an early-close day). An acceptance on a weekend
or market holiday rolls back to post_close of the previous trading day.

``entry_verdict`` compares that session with a backtest entry at the open of
``entry_date``:

  before_entry   accepted before the entry-day open: the release was public
                 at entry, whatever the filing lag.
  entry_session  accepted during the entry-day session. Ambiguous: a pre-open
                 release with a late 8-K, or a genuine intraday release.
  after_entry    accepted at or after the entry-day close, or on a later day:
                 look-ahead, unless the release preceded its 8-K by a whole
                 session.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from earnings_session.calendar import (
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    session_close,
    session_open,
)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

PRE_OPEN = "pre_open"
INTRADAY = "intraday"
POST_CLOSE = "post_close"
SESSIONS = (PRE_OPEN, INTRADAY, POST_CLOSE)

BEFORE_ENTRY = "before_entry"
ENTRY_SESSION = "entry_session"
AFTER_ENTRY = "after_entry"
VERDICTS = (BEFORE_ENTRY, ENTRY_SESSION, AFTER_ENTRY)

# Vendor labels and the session each one claims on the event date.
LABEL_SESSION = {"bmo": PRE_OPEN, "dmh": INTRADAY, "amc": POST_CLOSE}
SESSION_LABEL = {v: k for k, v in LABEL_SESSION.items()}
PRIOR_DAY = "prior_day"
LATER_DAY = "later_day"
EDGAR_LABELS = ("bmo", "dmh", "amc", PRIOR_DAY, LATER_DAY)

FORM_8K = "8-K"
FORM_6K = "6-K"
PERIODIC_FORMS = ("10-Q", "10-K", "10-KT")


# ---------------------------------------------------------------------------
# Acceptance times
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    label: str  # PRE_OPEN | INTRADAY | POST_CLOSE
    session_date: date  # trading day the label refers to
    rolled: bool = False  # accepted on a non-trading day, rolled back


def parse_edgar_acceptance(value: str) -> datetime:
    """``acceptanceDateTime`` ("2024-10-31T20:30:33.000Z") -> aware UTC datetime, as labelled."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"acceptanceDateTime without a zone: {value!r}")
    return dt.astimezone(UTC)


def et_wall_clock(value: datetime) -> datetime:
    """Aware UTC datetime for a naive US/Eastern wall-clock time (index-page format)."""
    if value.tzinfo is not None:
        raise ValueError("expected a naive US/Eastern wall-clock time")
    return value.replace(tzinfo=ET).astimezone(UTC)


def zone_convention(api: datetime, page_et: datetime) -> str:
    """How the API's "Z" value relates to the index page's ET "Accepted" time.

    ``utc``: the API value is real UTC. ``et_as_utc``: the API value is the ET
    wall clock labelled Z. ``mismatch``: neither (a different filing time).
    """
    if api.astimezone(ET).replace(tzinfo=None) == page_et:
        return "utc"
    if api.astimezone(UTC).replace(tzinfo=None) == page_et:
        return "et_as_utc"
    return "mismatch"


def classify_acceptance(ts: datetime) -> Session:
    """NYSE session of an acceptance timestamp."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime: acceptance timestamps must be timezone-aware")
    d = ts.astimezone(ET).date()
    if not is_trading_day(d):
        return Session(POST_CLOSE, prev_trading_day(d), rolled=True)
    if ts < session_open(d):
        return Session(PRE_OPEN, d)
    if ts < session_close(d):
        return Session(INTRADAY, d)
    return Session(POST_CLOSE, d)


def entry_verdict(session: Session, entry_date: date) -> str:
    """Where the acceptance falls relative to an entry at ``entry_date``'s open."""
    if session.session_date < entry_date:
        return BEFORE_ENTRY
    if session.session_date > entry_date:
        return AFTER_ENTRY
    return {PRE_OPEN: BEFORE_ENTRY, INTRADAY: ENTRY_SESSION, POST_CLOSE: AFTER_ENTRY}[session.label]


def reaction_date(session: Session) -> date:
    """First trading day whose open comes after the acceptance."""
    if session.label == PRE_OPEN:
        return session.session_date
    return next_trading_day(session.session_date + timedelta(days=1))


# ---------------------------------------------------------------------------
# Vendor labels
# ---------------------------------------------------------------------------


def normalize_label(value: str | None) -> str | None:
    """ "BMO" / " amc " -> "bmo" / "amc"; anything outside bmo/amc/dmh -> None."""
    v = (value or "").strip().lower()
    return v if v in LABEL_SESSION else None


def edgar_label(session: Session, event_date: date) -> str:
    """The label the acceptance itself supports, relative to the event date.

    bmo / dmh / amc when the acceptance falls in that session of the event's
    trading day (the event date, or the next trading day if the event date is
    not one); ``prior_day`` / ``later_day`` when it falls on an earlier or
    later trading day.
    """
    ref = next_trading_day(event_date)
    if session.session_date < ref:
        return PRIOR_DAY
    if session.session_date > ref:
        return LATER_DAY
    return SESSION_LABEL[session.label]


def implied_entry(event_date: date, label: str | None) -> date:
    """The open a backtest trading on the label would enter at.

    amc: the first trading day after the event date. bmo, dmh or no label:
    the event's trading day (no label = the BMO assumption).
    """
    if label == "amc":
        return next_trading_day(event_date + timedelta(days=1))
    return next_trading_day(event_date)


# ---------------------------------------------------------------------------
# Filing selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    items: str
    acceptance: datetime  # aware UTC, as the API labels it
    filing_date: date
    acceptance_raw: str = ""  # the API's acceptanceDateTime string

    @property
    def acceptance_et_date(self) -> date:
        return self.acceptance.astimezone(ET).date()


def has_item_202(items: str | None) -> bool:
    return "2.02" in {x.strip() for x in str(items or "").split(",")}


def filing_window(event_date: date, entry_date: date | None = None) -> tuple[date, date]:
    """Acceptance ET dates searched for an event's filing (inclusive).

    From the trading day before the event date to the trading day after the
    later of event and entry date, weekends in between included: data vendors
    date an after-close release on either side of midnight.
    """
    later = event_date if entry_date is None else max(event_date, entry_date)
    start = prev_trading_day(event_date - timedelta(days=1))
    end = next_trading_day(later + timedelta(days=1))
    return start, end


def _nearest(cands: list[Filing], event_date: date) -> Filing:
    return min(cands, key=lambda f: (abs((f.acceptance_et_date - event_date).days), f.acceptance))


def _in_window(filings: Iterable[Filing], start: date, end: date) -> list[Filing]:
    return [f for f in filings if start <= f.acceptance_et_date <= end]


def pick_earnings_filing(
    filings: Iterable[Filing], event_date: date, entry_date: date | None = None
) -> tuple[Filing | None, int]:
    """Original 8-K with Item 2.02 accepted in the window, nearest the event date.

    Matches on the acceptance ET date, never on filingDate (EDGAR moves the
    filing date of an acceptance after 17:30 ET to the next business day).
    Amendments (8-K/A) are skipped. Ties go to the earliest acceptance.
    Returns (filing, candidates in window).
    """
    start, end = filing_window(event_date, entry_date)
    cands = [
        f for f in _in_window(filings, start, end) if f.form == FORM_8K and has_item_202(f.items)
    ]
    if not cands:
        return None, 0
    return _nearest(cands, event_date), len(cands)


def pick_6k_proxy(
    filings: Iterable[Filing], event_date: date, entry_date: date | None = None
) -> tuple[Filing | None, int]:
    """Foreign private issuer fallback: the original 6-K nearest the event date.

    A 6-K carries no item code, so this may be a different furnishing than the
    earnings release: a low-confidence read. Same window and tie rule as
    ``pick_earnings_filing``.
    """
    start, end = filing_window(event_date, entry_date)
    cands = [f for f in _in_window(filings, start, end) if f.form == FORM_6K]
    if not cands:
        return None, 0
    return _nearest(cands, event_date), len(cands)


def pick_fallback_filing(
    filings: Iterable[Filing], event_date: date, entry_date: date | None = None
) -> tuple[Filing | None, int]:
    """No Item 2.02: the nearest original 8-K under any item, else the nearest 10-Q / 10-K.

    Small issuers often announce results under Item 7.01 / 8.01 or only in the
    periodic report. Same window and tie rule as ``pick_earnings_filing``.
    """
    start, end = filing_window(event_date, entry_date)
    in_window = _in_window(filings, start, end)
    for forms in ((FORM_8K,), PERIODIC_FORMS):
        cands = [f for f in in_window if f.form in forms]
        if cands:
            return _nearest(cands, event_date), len(cands)
    return None, 0


def unresolved_reason(filings: Iterable[Filing], start: date, end: date) -> str:
    """Why no earnings 8-K matched: what the issuer did file in the window."""
    forms = {f.form for f in _in_window(filings, start, end)}
    if FORM_8K in forms:
        return "8k_without_202"
    if forms & {FORM_6K, "6-K/A"}:
        return "6k_only"
    return "no_filing_in_window"

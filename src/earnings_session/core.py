"""Find an event's filing on EDGAR and classify its acceptance time.

``Classifier.classify`` is what the CLI's ``classify`` and ``batch`` call.

Filing search, in order:

1. CIK: the current SEC ticker list, then EDGAR full-text search for the
   quoted ticker in the window's Item 2.02 8-Ks (renamed and delisted
   issuers). A full-text candidate is used only with identity evidence: its
   display name lists the ticker, or its own 8-Ks within a year of the event
   cite the ticker after an exchange name ("NASDAQ: X"). Otherwise the event
   stays unresolved (``fts_ambiguous`` / ``fts_unconfirmed``).
2. Filing: the original 8-K with Item 2.02 nearest the event date inside the
   window (``timing.filing_window``). An issuer that files only 6-Ks gets
   the nearest 6-K as a low-confidence proxy. A ticker-list issuer without
   Item 2.02 falls back to its nearest other 8-K, then 10-Q / 10-K.
3. Time: the ET "Accepted" line of the filing's index page; the API's
   ``acceptanceDateTime`` is kept with its zone convention.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from earnings_session import edgar
from earnings_session.timing import (
    AFTER_ENTRY,
    ENTRY_SESSION,
    ET,
    FORM_8K,
    PRIOR_DAY,
    Filing,
    classify_acceptance,
    edgar_label,
    entry_verdict,
    et_wall_clock,
    filing_window,
    implied_entry,
    normalize_label,
    pick_6k_proxy,
    pick_earnings_filing,
    pick_fallback_filing,
    unresolved_reason,
    zone_convention,
)

RULE_ITEM_202 = "item_2.02"
RULE_OTHER_8K = "other_8k"
RULE_PERIODIC = "periodic"
RULE_6K_PROXY = "6k_proxy"
FALLBACK_REASONS = ("no_filing_in_window", "8k_without_202")

# Spec columns first, then the extra context columns.
CSV_COLUMNS = (
    "cik", "form", "accession", "accepted_et", "api_value", "zone_convention", "session",
    "verdict", "label_agrees", "rule", "confidence", "notes",
    "edgar_label", "session_date", "entry_used", "reason", "company",
)  # fmt: skip


@dataclass
class EventResult:
    ticker: str
    event_date: date
    entry_date: date | None
    vendor_label: str | None
    entry_used: date
    entry_source: str  # given | label | bmo_assumed
    window: tuple[date, date]
    status: str = "unresolved"  # resolved | unresolved
    reason: str = ""
    rule: str = ""
    confidence: str = ""
    cik: int | None = None
    cik_source: str = ""  # ticker_map | full_text
    cik_evidence: str = ""  # ticker_map | display_name | exchange_self_reference
    company: str = ""
    form: str = ""
    items: str = ""
    accession: str = ""
    filing_date: date | None = None
    n_in_window: int = 0
    api_value: str = ""
    zone_convention: str = ""  # utc | et_as_utc | mismatch | unverified
    accepted_et: datetime | None = None
    session: str = ""
    session_date: date | None = None
    rolled: bool = False
    verdict: str = ""
    edgar_label: str = ""
    label_agrees: bool | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def iso(v: date | datetime | None) -> str | None:
            return v.isoformat() if v is not None else None

        return {
            "ticker": self.ticker,
            "event_date": iso(self.event_date),
            "entry_date": iso(self.entry_date),
            "vendor_label": self.vendor_label,
            "entry_used": iso(self.entry_used),
            "entry_source": self.entry_source,
            "window": [iso(self.window[0]), iso(self.window[1])],
            "status": self.status,
            "reason": self.reason,
            "rule": self.rule,
            "confidence": self.confidence,
            "cik": self.cik,
            "cik_source": self.cik_source,
            "cik_evidence": self.cik_evidence,
            "company": self.company,
            "form": self.form,
            "items": self.items,
            "accession": self.accession,
            "filing_date": iso(self.filing_date),
            "n_in_window": self.n_in_window,
            "accepted_et": iso(self.accepted_et),
            "api_value": self.api_value,
            "zone_convention": self.zone_convention,
            "session": self.session,
            "session_date": iso(self.session_date),
            "rolled": self.rolled,
            "verdict": self.verdict,
            "edgar_label": self.edgar_label,
            "label_agrees": self.label_agrees,
            "notes": list(self.notes),
        }

    def to_row(self) -> dict[str, str]:
        """The batch CSV columns, as strings."""
        d = self.to_dict()
        row = {k: "" if d[k] is None else str(d[k]) for k in CSV_COLUMNS if k != "notes"}
        row["notes"] = "; ".join(self.notes)
        if self.label_agrees is not None:
            row["label_agrees"] = "true" if self.label_agrees else "false"
        return {k: row[k] for k in CSV_COLUMNS}


@dataclass(frozen=True)
class _Found:
    cik: int
    source: str
    evidence: str
    filing: Filing
    n_in_window: int
    rule: str


def as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise TypeError("pass a date, not a datetime")
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


class Classifier:
    """Holds the EDGAR client, the ticker list and per-CIK filing lists across events."""

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        cache: bool = True,
        cache_dir: Path | None = None,
        max_rps: float = edgar.DEFAULT_MAX_RPS,
        client: edgar.EdgarClient | None = None,
    ) -> None:
        if client is None:
            ua = user_agent if user_agent is not None else os.environ.get(edgar.USER_AGENT_ENV)
            directory = (cache_dir or edgar.default_cache_dir()) if cache else None
            client = edgar.EdgarClient(edgar.check_user_agent(ua), directory, max_rps=max_rps)
        self.client = client
        self._subs = edgar.Submissions(client)
        self._ticker_map: dict[str, int] | None = None

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Classifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def ticker_map(self) -> dict[str, int]:
        if self._ticker_map is None:
            self._ticker_map = edgar.load_ticker_map(self.client)
        return self._ticker_map

    def classify(
        self,
        ticker: str,
        event_date: date | str,
        entry_date: date | str | None = None,
        label: str | None = None,
    ) -> EventResult:
        """Classify one earnings event. ``label``: the vendor's bmo / amc / dmh, if any."""
        ticker = ticker.strip().upper()
        ev = as_date(event_date)
        en = as_date(entry_date) if entry_date not in (None, "") else None
        lab = normalize_label(label)
        if en is not None:
            entry_used, source = en, "given"
        elif lab is not None:
            entry_used, source = implied_entry(ev, lab), "label"
        else:
            entry_used, source = implied_entry(ev, None), "bmo_assumed"
        res = EventResult(
            ticker=ticker,
            event_date=ev,
            entry_date=en,
            vendor_label=label if label not in (None, "") else None,
            entry_used=entry_used,
            entry_source=source,
            window=filing_window(ev, en),
        )
        if label not in (None, "") and lab is None:
            res.notes.append(f"label {label!r} is not bmo/amc/dmh; ignored")
        if source == "label":
            res.notes.append(f"no entry date: assumed the {entry_used} open implied by {lab}")
        elif source == "bmo_assumed":
            res.notes.append(f"no entry date or label: assumed the {entry_used} open (BMO)")

        found = self._find(res)
        if found is None:
            return res
        self._time(res, found)
        return res

    # -- search --------------------------------------------------------------

    def _find(self, res: EventResult) -> _Found | None:
        ticker, ev, en = res.ticker, res.event_date, res.entry_date
        start, end = res.window
        client = self.client
        map_cik = edgar.map_lookup(self.ticker_map, ticker)
        fts = edgar.fts_hits(client, ticker, start, end)
        hits, _ = fts if fts is not None else ([], False)
        fts_ciks, display_match, ambiguous = edgar.fts_cik_candidates(hits, ticker)
        conflict = bool(display_match and map_cik is not None and map_cik not in fts_ciks)
        if conflict:
            res.notes.append(
                f"ticker reused: the current SEC list maps {ticker} to CIK {map_cik}, but the "
                f"window's 8-Ks list {ticker} under CIK {';'.join(map(str, fts_ciks))}; "
                "those are tried first"
            )
        evidence = "display_name" if display_match else "exchange_self_reference"
        fts_order = [(c, "full_text", evidence) for c in fts_ciks if c != map_cik]
        fts_order = fts_order[: edgar.MAX_FTS_CIKS]
        map_order = [(map_cik, "ticker_map", "ticker_map")] if map_cik is not None else []
        order = fts_order + map_order if conflict else map_order + fts_order

        reasons: list[str] = []
        checked: list[str] = []
        seen: dict[int, list[Filing]] = {}
        fpi: tuple[int, str, str, list[Filing]] | None = None
        for cik, source, kind in order:
            if kind == "exchange_self_reference":
                ok = edgar.self_reference(client, ticker, cik, ev)
                checked.append(f"{cik}:{'yes' if ok else 'no' if ok is False else 'error'}")
                if not ok:
                    continue
            filings = self._subs.filings(cik, start, end)
            if filings is None:
                reasons.append("submissions_unavailable")
                continue
            seen[cik] = filings
            best, n = pick_earnings_filing(filings, ev, en)
            if best is None:
                reason = unresolved_reason(filings, start, end)
                reasons.append(reason)
                if reason == "6k_only":  # an identified foreign issuer ends the search
                    fpi = (cik, source, kind, filings)
                    break
                continue
            if source == "ticker_map" and hits and best.accession not in {h["adsh"] for h in hits}:
                res.notes.append(
                    "full-text search did not find the ticker in this 8-K; "
                    "check the issuer if the ticker may have changed hands"
                )
            return _Found(cik, source, kind, best, n, RULE_ITEM_202)
        if checked:
            res.notes.append("identity checks (CIK:result): " + ", ".join(checked))

        if fpi is not None:
            cik, source, kind, filings = fpi
            proxy, n = pick_6k_proxy(filings, ev, en)
            if proxy is not None:
                return _Found(cik, source, kind, proxy, n, RULE_6K_PROXY)
        if map_cik is not None and map_cik in seen and not conflict and reasons:
            first = reasons[0]
            if first in FALLBACK_REASONS:
                best, n = pick_fallback_filing(seen[map_cik], ev, en)
                if best is not None:
                    rule = RULE_OTHER_8K if best.form == FORM_8K else RULE_PERIODIC
                    res.notes.append(f"no Item 2.02 8-K in the window ({first})")
                    return _Found(map_cik, "ticker_map", "ticker_map", best, n, rule)

        if reasons:
            res.reason = reasons[0]
        elif ambiguous:
            res.reason = "fts_ambiguous"
        elif checked:
            res.reason = "fts_unconfirmed"
        else:
            res.reason = "no_cik"
        return None

    # -- timing --------------------------------------------------------------

    def _time(self, res: EventResult, found: _Found) -> None:
        f = found.filing
        res.status = "resolved"
        res.rule = found.rule
        res.cik, res.cik_source, res.cik_evidence = found.cik, found.source, found.evidence
        res.company = self._subs.name(found.cik)
        res.form, res.items, res.accession = f.form, f.items, f.accession
        res.filing_date, res.n_in_window = f.filing_date, found.n_in_window
        res.api_value = f.acceptance_raw
        page_et = edgar.index_acceptance(self.client, found.cik, f.accession)
        if page_et is None:
            acceptance = f.acceptance
            res.zone_convention = "unverified"
            res.notes.append(
                "index page unavailable: the API value was read as UTC; it may be "
                "US/Eastern labelled Z, i.e. 4-5 hours early"
            )
        else:
            acceptance = et_wall_clock(page_et)
            res.zone_convention = zone_convention(f.acceptance, page_et)
            if res.zone_convention == "et_as_utc":
                res.notes.append("API acceptanceDateTime is US/Eastern labelled Z")
            elif res.zone_convention == "mismatch":
                res.notes.append(
                    "API acceptanceDateTime matches neither UTC nor ET; index page used"
                )
        res.accepted_et = acceptance.astimezone(ET)

        s = classify_acceptance(acceptance)
        res.session, res.session_date, res.rolled = s.label, s.session_date, s.rolled
        if s.rolled:
            res.notes.append(
                f"accepted on a non-trading day: counted as post_close of {s.session_date}"
            )
        res.verdict = entry_verdict(s, res.entry_used)
        res.edgar_label = edgar_label(s, res.event_date)
        lab = normalize_label(res.vendor_label)
        if lab is not None:
            res.label_agrees = res.edgar_label == lab
            if lab == "bmo" and res.edgar_label == PRIOR_DAY:
                res.notes.append(
                    "accepted in an earlier session than the event date: the date may be the "
                    "reaction day; a BMO entry on it is still after the release"
                )
        if found.n_in_window > 1:
            res.notes.append(f"{found.n_in_window} candidate filings in the window; nearest taken")

        verified = page_et is not None
        if found.rule == RULE_ITEM_202:
            res.confidence = "high" if verified else "medium"
        elif found.rule == RULE_6K_PROXY:
            res.confidence = "low"
            res.notes.append("6-K has no item code: it may not be the earnings release")
        else:
            res.confidence = "medium" if verified else "low"
            res.notes.append(f"{f.form} is not an earnings-release filing: a loose upper bound")
        if res.verdict == AFTER_ENTRY:
            res.notes.append(
                "filed after the entry open: look-ahead unless the release preceded the "
                "filing by a whole session"
            )
        elif res.verdict == ENTRY_SESSION:
            res.notes.append(
                "filed during the entry session: an intraday release, or a pre-open release "
                "with a late filing"
            )


def classify(
    ticker: str,
    event_date: date | str,
    entry_date: date | str | None = None,
    label: str | None = None,
    *,
    user_agent: str | None = None,
    cache: bool = True,
    cache_dir: Path | None = None,
    max_rps: float = edgar.DEFAULT_MAX_RPS,
) -> EventResult:
    """One-off convenience wrapper around ``Classifier.classify``."""
    with Classifier(user_agent, cache=cache, cache_dir=cache_dir, max_rps=max_rps) as c:
        return c.classify(ticker, event_date, entry_date, label)

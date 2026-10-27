"""SEC EDGAR access: throttled, cached GETs and the parsers built on them.

SEC fair access (https://www.sec.gov/os/accessing-edgar-data): every request
declares a User-Agent with a contact, and a client stays under 10 requests per
second across www.sec.gov, data.sec.gov and efts.sec.gov. One throttle covers
all three hosts here; the default is 5 requests per second.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from earnings_session.timing import Filing, has_item_202, parse_edgar_acceptance

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{acc}-index.htm"

USER_AGENT_ENV = "EARNINGS_SESSION_USER_AGENT"
DEFAULT_MAX_RPS = 5.0
SEC_MAX_RPS = 10.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
MUTABLE_TTL_S = 24 * 3600.0  # ticker list, submissions, full-text search
FILING_DATE_PAD = timedelta(days=5)  # filingDate trails acceptance by <= a long weekend
IDENTITY_PAD = timedelta(days=365)  # window of the issuer's own 8-Ks for the identity check
MAX_FTS_CIKS = 3  # more undifferentiated full-text candidates = ambiguous
# Exchange names a press release puts before its own ticker ("(NASDAQ: X)",
# "(Nasdaq/NMS: X)"). Full-text search drops the punctuation, so each entry is
# a phrase query; a ticker inside a list ("Nasdaq: A, B, X") is not matched.
EXCHANGES = (
    "NASDAQ", "NASDAQ/NMS", "NASDAQ/NGS", "NASDAQ GS", "NASDAQGS", "NASDAQ GM", "NASDAQGM",
    "NYSE", "NYSE American", "NYSE MKT", "NYSE Amex", "NYSEAMERICAN", "NYSE Arca", "AMEX",
    "OTCQB", "OTCQX", "OTC",
)  # fmt: skip

_ACCEPTED_RE = re.compile(r"Accepted</div>\s*<div class=\"info\">([^<]+)</div>")
_DISPLAY_TICKERS_RE = re.compile(r"\(([^()]*)\)\s+\(CIK")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")

USER_AGENT_HELP = f"""\
SEC asks every automated client to declare who it is in the User-Agent header:

    Sample Company Name AdminContact@<sample company domain>.com

Pass it with --user-agent "Your Name you@your-domain.com" or set
{USER_AGENT_ENV}. See https://www.sec.gov/os/accessing-edgar-data."""


class UserAgentError(ValueError):
    pass


class EdgarAccessError(RuntimeError):
    """SEC refused the request (403): bad User-Agent or the client is blocked."""


def check_user_agent(value: str | None) -> str:
    """The User-Agent if it names a requester and a contact address, else UserAgentError."""
    ua = (value or "").strip()
    if not ua:
        raise UserAgentError("no User-Agent given.\n" + USER_AGENT_HELP)
    email = _EMAIL_RE.search(ua)
    if email is None or not ua.replace(email.group(0), "").strip():
        raise UserAgentError("User-Agent needs a name and a contact email.\n" + USER_AGENT_HELP)
    if re.search(r"@example\.(com|org|net)\b", ua, re.IGNORECASE):
        raise UserAgentError("User-Agent still uses an example.* address.\n" + USER_AGENT_HELP)
    return ua


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "earnings-session"


class EdgarClient:
    """Throttled, disk-cached GETs against SEC EDGAR (one throttle for every host).

    200 and 404 answers are cached; ``max_age`` (seconds) bounds how old a
    cached answer may be, None means forever. Retries 429 / 5xx / transport
    errors with exponential back-off; a 403 raises EdgarAccessError.
    """

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path | None = None,
        *,
        max_rps: float = DEFAULT_MAX_RPS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < max_rps <= SEC_MAX_RPS:
            raise ValueError(f"max_rps must be in (0, {SEC_MAX_RPS:g}]")
        self._http = httpx.Client(
            headers={
                "User-Agent": check_user_agent(user_agent),
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=30.0,
            transport=transport,
        )
        self._cache = cache_dir
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._min_interval = 1.0 / max_rps
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._last = -math.inf
        self.requests = 0
        self.cache_hits = 0
        self.failures = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _path(self, url: str, params: dict[str, str] | None) -> Path | None:
        if self._cache is None:
            return None
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        return self._cache / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def _fetch(self, url: str, params: dict[str, str] | None) -> tuple[int, str]:
        status, body = -1, ""
        for attempt in range(MAX_ATTEMPTS):
            wait = self._last + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.requests += 1
            try:
                resp = self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                status, body = -1, type(exc).__name__
            else:
                status, body = resp.status_code, resp.text
            if status != -1 and status not in RETRY_STATUS:
                return status, body
            if attempt + 1 < MAX_ATTEMPTS:
                self._sleep(2.0 * 2**attempt)
        self.failures += 1
        return status, body

    def get(
        self, url: str, params: dict[str, str] | None = None, *, max_age: float | None = None
    ) -> tuple[int, str]:
        """(status, body); -1 status after a transport error that outlived the retries."""
        path = self._path(url, params)
        if path is not None and path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            fresh = max_age is None or self._wall_clock() - float(rec["fetched_at"]) <= max_age
            if fresh:
                self.cache_hits += 1
                return int(rec["status"]), str(rec["body"])
        status, body = self._fetch(url, params)
        if status == 403:
            raise EdgarAccessError(
                "SEC answered 403 Forbidden: the User-Agent was rejected or this client is "
                "blocked. Check the User-Agent format and stay under 10 requests per second."
            )
        if path is not None and status in (200, 404):
            rec = {
                "url": url,
                "params": params,
                "status": status,
                "fetched_at": self._wall_clock(),
                "body": body,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            tmp.replace(path)
        return status, body

    def get_json(
        self, url: str, params: dict[str, str] | None = None, *, max_age: float | None = None
    ) -> Any:
        status, body = self.get(url, params, max_age=max_age)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------


def parse_filings(block: dict[str, Any]) -> list[Filing]:
    """Column-major submissions block (``recent`` or a ``files`` page) -> Filings."""
    acc = block.get("accessionNumber", [])
    forms = block.get("form", [])
    fdates = block.get("filingDate", [])
    accepts = block.get("acceptanceDateTime", [])
    items = block.get("items", [])
    out: list[Filing] = []
    for i in range(min(len(acc), len(forms), len(fdates), len(accepts))):
        if not accepts[i] or not fdates[i]:
            continue
        try:
            ts = parse_edgar_acceptance(accepts[i])
            fd = date.fromisoformat(fdates[i])
        except ValueError:
            continue
        item = items[i] if i < len(items) else ""
        out.append(Filing(acc[i], forms[i], item or "", ts, fd, str(accepts[i])))
    return out


class Submissions:
    """Per-CIK filing lists, including the older ``files`` pages a window needs."""

    def __init__(self, client: EdgarClient) -> None:
        self._client = client
        self._main: dict[int, dict[str, Any] | None] = {}
        self._pages: dict[str, list[Filing]] = {}

    def _company(self, cik: int) -> dict[str, Any] | None:
        if cik not in self._main:
            url = SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json")
            data = self._client.get_json(url, max_age=MUTABLE_TTL_S)
            self._main[cik] = data if isinstance(data, dict) else None
        return self._main[cik]

    def _page(self, name: str) -> list[Filing]:
        if name not in self._pages:
            # older pages are closed: they never change once written
            data = self._client.get_json(SUBMISSIONS_URL.format(name=name))
            self._pages[name] = parse_filings(data) if isinstance(data, dict) else []
        return self._pages[name]

    def name(self, cik: int) -> str:
        main = self._company(cik)
        return str(main.get("name") or "") if main else ""

    def filings(self, cik: int, start: date, end: date) -> list[Filing] | None:
        """Filings whose filingDate fits an acceptance in [start, end]; None = unknown CIK."""
        main = self._company(cik)
        if main is None:
            return None
        filings = main.get("filings", {})
        recent = parse_filings(filings.get("recent", {}))
        lo, hi = start - timedelta(days=1), end + FILING_DATE_PAD
        out = [f for f in recent if lo <= f.filing_date <= hi]
        oldest = min((f.filing_date for f in recent), default=None)
        if oldest is None or oldest > lo:
            for page in filings.get("files", []):
                try:
                    p_from = date.fromisoformat(page["filingFrom"])
                    p_to = date.fromisoformat(page["filingTo"])
                except (KeyError, ValueError):
                    continue
                if p_from <= hi and p_to >= lo:
                    out += [f for f in self._page(page["name"]) if lo <= f.filing_date <= hi]
        return out


def index_acceptance(client: EdgarClient, cik: int, accession: str) -> datetime | None:
    """The ET "Accepted" wall-clock time on the filing's index page (naive), or None."""
    url = INDEX_URL.format(cik=int(cik), nodash=accession.replace("-", ""), acc=accession)
    status, body = client.get(url)
    m = _ACCEPTED_RE.search(body) if status == 200 else None
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# CIK lookup
# ---------------------------------------------------------------------------


def load_ticker_map(client: EdgarClient) -> dict[str, int]:
    """Current SEC ticker list: ticker -> CIK."""
    data = client.get_json(TICKERS_URL, max_age=MUTABLE_TTL_S)
    if not isinstance(data, dict):
        raise EdgarAccessError("could not load SEC company_tickers.json")
    return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in data.values()}


def map_lookup(ticker_map: dict[str, int], ticker: str) -> int | None:
    t = ticker.upper()
    for cand in (t, t.replace(".", "-"), t.replace("-", ".")):
        if cand in ticker_map:
            return ticker_map[cand]
    return None


def fts_hits(
    client: EdgarClient, ticker: str, start: date, end: date
) -> tuple[list[dict[str, Any]], bool] | None:
    """Item 2.02 8-K documents whose text contains the quoted ticker; (hits, truncated)."""
    params = {
        "q": f'"{ticker}"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start.isoformat(),
        "enddt": (end + FILING_DATE_PAD).isoformat(),
    }
    data = client.get_json(FTS_URL, params, max_age=MUTABLE_TTL_S)
    if not isinstance(data, dict):
        return None
    block = data.get("hits", {})
    total = block.get("total", {})
    rows = block.get("hits", [])
    truncated = total.get("relation", "eq") != "eq" or int(total.get("value", 0)) > len(rows)
    keep = []
    for h in rows:
        src = h.get("_source", {})
        if not has_item_202(",".join(src.get("items") or [])):
            continue
        keep.append(
            {
                "adsh": src.get("adsh") or str(h.get("_id", "")).split(":")[0],
                "ciks": [int(c) for c in src.get("ciks", [])],
                "display_names": list(src.get("display_names", [])),
            }
        )
    return keep, truncated


def display_tickers(name: str) -> set[str]:
    """Tickers listed in a full-text display name: "2U, Inc.  (TWOUQ)  (CIK 0001459417)"."""
    m = _DISPLAY_TICKERS_RE.search(name)
    return {t.strip().upper() for t in m.group(1).split(",")} if m else set()


def fts_cik_candidates(hits: list[dict[str, Any]], ticker: str) -> tuple[list[int], bool, bool]:
    """(candidate CIKs, any display-name match, ambiguous).

    A CIK whose display name lists the ticker (or the ticker with the "Q" that
    marks a bankrupt issuer's OTC symbol) goes first and alone. Without such a
    match the candidates still need an identity check (``self_reference``),
    and more than MAX_FTS_CIKS of them is ambiguous: none is returned.
    """
    t = ticker.upper()
    ordered: list[int] = []
    matched: list[int] = []
    for h in hits:
        names = h["display_names"] or [""] * len(h["ciks"])
        for cik, name in zip(h["ciks"], names, strict=False):
            if cik not in ordered:
                ordered.append(cik)
            hit = any(x in (t, t + "Q") for x in display_tickers(name))
            if hit and cik not in matched:
                matched.append(cik)
        for cik in h["ciks"]:
            if cik not in ordered:
                ordered.append(cik)
    if matched:
        return matched, True, False
    if len(ordered) > MAX_FTS_CIKS:
        return [], False, True
    return ordered, False, False


def self_reference(client: EdgarClient, ticker: str, cik: int, event_date: date) -> bool | None:
    """Whether the CIK's own 8-Ks near the event cite the ticker after an exchange name.

    Identity check for a full-text candidate without a display-name match:
    an issuer's press releases name its own listing ("(NASDAQ: X)"), a company
    whose filing merely contains the string X does not. Any item counts and the
    window is a year either side, because the issuer's filings in the event
    window may all be Item 2.02 hits of someone else. None when the search failed.
    """
    t = ticker.upper()
    params = {
        "q": " OR ".join(f'"{x}: {t}"' for x in EXCHANGES),
        "forms": "8-K",
        "ciks": f"{int(cik):010d}",
        "dateRange": "custom",
        "startdt": (event_date - IDENTITY_PAD).isoformat(),
        "enddt": (event_date + IDENTITY_PAD).isoformat(),
    }
    data = client.get_json(FTS_URL, params, max_age=MUTABLE_TTL_S)
    if not isinstance(data, dict):
        return None
    return int(data.get("hits", {}).get("total", {}).get("value", 0)) > 0

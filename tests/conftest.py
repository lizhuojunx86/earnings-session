"""Offline stand-ins for sec.gov: httpx.MockTransport, a fake clock, payload builders."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from earnings_session.edgar import EdgarClient

UA = "earnings-session tests tests@earnings-session.invalid"
FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc

Handler = Callable[[httpx.Request], httpx.Response]


def utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def make_client(
    tmp_path: Path, handler: Handler, clock: FakeClock | None = None, **kw: Any
) -> tuple[EdgarClient, list[httpx.Request], FakeClock]:
    seen: list[httpx.Request] = []
    clock = clock or FakeClock()

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = EdgarClient(
        UA,
        tmp_path / "cache",
        transport=httpx.MockTransport(wrapped),
        sleep=clock.sleep,
        clock=clock,
        wall_clock=clock,
        **kw,
    )
    return client, seen, clock


Row = tuple[str, str, str, str, str]  # accession, form, filingDate, acceptanceDateTime, items


def block(rows: list[Row]) -> dict[str, list[str]]:
    """Rows -> the column-major block of a submissions JSON."""
    return {
        "accessionNumber": [r[0] for r in rows],
        "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows],
        "acceptanceDateTime": [r[3] for r in rows],
        "items": [r[4] for r in rows],
    }


def hit(adsh: str, ciks: list[str], names: list[str], items: list[str]) -> dict[str, Any]:
    return {
        "_id": f"{adsh}:ex99.htm",
        "_source": {"adsh": adsh, "ciks": ciks, "display_names": names, "items": items},
    }


def fts_payload(hits: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    n = len(hits) if total is None else total
    return {"hits": {"total": {"value": n, "relation": "eq"}, "hits": hits}}


def index_page(accepted: str) -> str:
    """The fragment of an EDGAR filing index page that carries the ET "Accepted" time."""
    return (
        '<div class="formGrouping"><div class="infoHead">Accepted</div>\n'
        f'<div class="info">{accepted}</div></div>'
    )


def edgar_site(
    companies: dict[int, list[Row]],
    fts: dict[str, Any] | None = None,
    pages: dict[str, str] | None = None,
    identity: dict[int, int] | None = None,
    tickers: dict[str, int] | None = None,
    names: dict[int, str] | None = None,
) -> Handler:
    """A fake sec.gov.

    ``pages``: accession -> ET "Accepted" string (missing = 404); ``identity``:
    CIK -> number of its 8-Ks citing the ticker after an exchange name.
    """
    fts = fts if fts is not None else fts_payload([])
    pages = pages or {}
    identity = identity or {}
    tickers = tickers or {}
    names = names or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "efts.sec.gov":
            if "ciks" in request.url.params:
                n = identity.get(int(request.url.params["ciks"]), 0)
                return httpx.Response(200, json=fts_payload([], total=n))
            return httpx.Response(200, json=fts)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "company_tickers.json":
            body = {
                str(i): {"cik_str": cik, "ticker": t, "title": t}
                for i, (t, cik) in enumerate(tickers.items())
            }
            return httpx.Response(200, json=body)
        if name.endswith("-index.htm"):
            accepted = pages.get(name.removesuffix("-index.htm"))
            if accepted is None:
                return httpx.Response(404)
            return httpx.Response(200, text=index_page(accepted))
        for cik, rows in companies.items():
            if name == f"CIK{cik:010d}.json":
                payload = {"cik": str(cik), "name": names.get(cik, f"Company {cik}")}
                payload["filings"] = {"recent": block(rows), "files": []}  # type: ignore[assignment]
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return handler


def recorded_site() -> Handler:
    """sec.gov answered from the recorded responses in tests/fixtures/sec/.

    ``index.json`` maps "host/path?query" keys to files; anything else is 404.
    """
    root = FIXTURES / "sec"
    index: dict[str, str] = json.loads((root / "index.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        if request.url.host == "efts.sec.gov":
            q = "&".join(f"{k}={v}" for k, v in sorted(request.url.params.items()))
            key = f"{key}?{q}"
        name = index.get(key)
        if name is None:
            return httpx.Response(404)
        return httpx.Response(200, content=(root / name).read_bytes())

    return handler


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()

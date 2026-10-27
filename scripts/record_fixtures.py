"""Record the SEC responses behind tests/test_recorded.py into tests/fixtures/sec/.

Runs the classifier on a few public events with a recording transport, then
trims each response to what the tests need: the ticker list to the tickers
used, submissions to the filings near each event, full-text search hits to
the fields the code reads. SEC EDGAR data is public domain.

Usage (makes about 30 requests):
    EARNINGS_SESSION_USER_AGENT="Your Name you@your-domain.com" \\
        python scripts/record_fixtures.py
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from earnings_session import Classifier
from earnings_session.edgar import USER_AGENT_ENV, EdgarClient

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sec"
EVENTS = [
    ("AAPL", "2024-08-01", "amc"),  # current ticker list, API in real UTC
    ("OSPN", "2019-10-29", "bmo"),  # evening release labelled bmo
    ("ARNA", "2016-05-09", None),  # delisted: full-text search + identity check
]
KEEP_DAYS = 10  # submissions rows kept on each side of an event


def key_of(request: httpx.Request) -> str:
    key = f"{request.url.host}{request.url.path}"
    if request.url.host == "efts.sec.gov":
        key += "?" + "&".join(f"{k}={v}" for k, v in sorted(request.url.params.items()))
    return key


def trim_block(block: dict[str, list[Any]], days: list[date]) -> dict[str, list[Any]]:
    dates = block.get("filingDate", [])
    keep = [
        i
        for i, d in enumerate(dates)
        if any(abs((date.fromisoformat(d) - e).days) <= KEEP_DAYS for e in days)
    ]
    return {k: [v[i] for i in keep] for k, v in block.items() if isinstance(v, list)}


def trim(key: str, body: bytes, tickers: set[str], days: list[date]) -> bytes:
    if key.endswith("company_tickers.json"):
        data = json.loads(body)
        rows = [v for v in data.values() if v["ticker"] in tickers]
        return json.dumps({str(i): v for i, v in enumerate(rows)}, indent=1).encode()
    if key.startswith("data.sec.gov/submissions/"):
        data = json.loads(body)
        if "filings" in data:
            out = {k: data[k] for k in ("cik", "name", "tickers", "exchanges") if k in data}
            f = data["filings"]
            out["filings"] = {"recent": trim_block(f["recent"], days), "files": f.get("files", [])}
        else:
            out = trim_block(data, days)
        return json.dumps(out, indent=1).encode()
    if key.startswith("efts.sec.gov"):
        data = json.loads(body)
        hits = data.get("hits", {})
        slim = [
            {
                "_id": h.get("_id"),
                "_source": {
                    k: h.get("_source", {}).get(k)
                    for k in ("adsh", "ciks", "display_names", "items", "form", "file_date")
                },
            }
            for h in hits.get("hits", [])
        ]
        return json.dumps({"hits": {"total": hits.get("total"), "hits": slim}}, indent=1).encode()
    return body


def file_name(key: str, n: int) -> str:
    if key.startswith("efts.sec.gov"):
        return f"fts_{n:02d}.json"
    last = key.split("/")[-1]
    if key.startswith("data.sec.gov/submissions/"):
        return f"submissions_{last}"
    return last


def main() -> None:
    ua = os.environ.get(USER_AGENT_ENV, "")
    recorded: dict[str, bytes] = {}
    real = httpx.HTTPTransport()

    def handler(request: httpx.Request) -> httpx.Response:
        resp = real.handle_request(request)
        resp.read()
        if resp.status_code == 200:
            recorded[key_of(request)] = resp.content
        ctype = resp.headers.get("content-type", "")  # body is already decoded
        return httpx.Response(
            resp.status_code, content=resp.content, headers={"content-type": ctype}
        )

    client = EdgarClient(ua, None, transport=httpx.MockTransport(handler))
    with Classifier(client=client) as clf:
        for ticker, day, label in EVENTS:
            r = clf.classify(ticker, day, label=label)
            print(ticker, day, r.rule, r.accepted_et, r.zone_convention, r.verdict)
    tickers = {t for t, _, _ in EVENTS}
    days = [date.fromisoformat(d) for _, d, _ in EVENTS]
    days += [d - timedelta(days=1) for d in days]
    OUT.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}
    for n, (key, body) in enumerate(sorted(recorded.items())):
        name = file_name(key, n)
        (OUT / name).write_bytes(trim(key, body, tickers, days))
        index[key] = name
    (OUT / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(index)} responses to {OUT}")


if __name__ == "__main__":
    main()

"""EDGAR client (throttle, retry, cache), User-Agent check and response parsers. No network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from conftest import UA, FakeClock, block, fts_payload, hit, make_client

from earnings_session import edgar
from earnings_session.timing import parse_edgar_acceptance


class TestUserAgent:
    @pytest.mark.parametrize(
        "ua", ["Jane Doe jane@doe-research.test", "Acme Fund LLC ops@acme-fund.test"]
    )
    def test_accepts_name_and_email(self, ua: str) -> None:
        assert edgar.check_user_agent(f"  {ua} ") == ua

    @pytest.mark.parametrize(
        "ua",
        [None, "", "   ", "jane@doe-research.test", "Jane Doe", "Jane jane@example.com"],
    )
    def test_refuses_and_explains_the_sec_format(self, ua: str | None) -> None:
        with pytest.raises(edgar.UserAgentError) as exc:
            edgar.check_user_agent(ua)
        msg = str(exc.value)
        assert "Sample Company Name AdminContact@<sample company domain>.com" in msg
        assert edgar.USER_AGENT_ENV in msg

    def test_client_refuses_a_bad_user_agent(self, tmp_path: Path) -> None:
        with pytest.raises(edgar.UserAgentError):
            edgar.EdgarClient("no contact", tmp_path)

    def test_rate_cap(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_rps"):
            edgar.EdgarClient(UA, tmp_path, max_rps=11)


class TestEdgarClient:
    def test_default_throttle_is_five_per_second(self, tmp_path: Path) -> None:
        client, seen, clock = make_client(tmp_path, lambda r: httpx.Response(200, text="{}"))
        client.get("https://data.sec.gov/a")
        client.get("https://www.sec.gov/b")
        client.get("https://efts.sec.gov/c", {"q": '"X"'})
        assert len(seen) == 3 and client.requests == 3
        # first request immediately, then one interval before each later one, across hosts
        assert clock.sleeps == [pytest.approx(0.2), pytest.approx(0.2)]
        assert all(r.headers["User-Agent"] == UA for r in seen)

    def test_custom_rate(self, tmp_path: Path) -> None:
        client, _, clock = make_client(
            tmp_path, lambda r: httpx.Response(200, text="{}"), max_rps=10
        )
        client.get("https://data.sec.gov/a")
        client.get("https://data.sec.gov/b")
        assert clock.sleeps == [pytest.approx(0.1)]

    def test_cache_hit_skips_network(self, tmp_path: Path) -> None:
        client, seen, _ = make_client(tmp_path, lambda r: httpx.Response(200, text='{"k": 1}'))
        assert client.get_json("https://data.sec.gov/a", {"q": "1"}) == {"k": 1}
        assert client.get_json("https://data.sec.gov/a", {"q": "1"}) == {"k": 1}
        assert client.get_json("https://data.sec.gov/a", {"q": "2"}) == {"k": 1}
        assert len(seen) == 2 and client.cache_hits == 1
        files = sorted((tmp_path / "cache").glob("*.json"))
        assert len(files) == 2
        assert json.loads(files[0].read_text())["status"] == 200

    def test_cache_max_age(self, tmp_path: Path) -> None:
        clock = FakeClock()
        client, seen, _ = make_client(
            tmp_path, lambda r: httpx.Response(200, text="{}"), clock=clock
        )
        client.get("https://data.sec.gov/a", max_age=100)
        clock.now += 50
        client.get("https://data.sec.gov/a", max_age=100)
        assert len(seen) == 1
        clock.now += 100
        client.get("https://data.sec.gov/a", max_age=100)
        assert len(seen) == 2
        client.get("https://data.sec.gov/a")  # no max_age: any cached copy will do
        assert len(seen) == 2

    def test_no_cache(self, tmp_path: Path) -> None:
        seen: list[httpx.Request] = []

        def handler(r: httpx.Request) -> httpx.Response:
            seen.append(r)
            return httpx.Response(200, text="{}")

        clock = FakeClock()
        client = edgar.EdgarClient(
            UA, None, transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock
        )
        client.get("https://data.sec.gov/a")
        client.get("https://data.sec.gov/a")
        assert len(seen) == 2

    def test_404_cached_as_missing(self, tmp_path: Path) -> None:
        client, seen, _ = make_client(tmp_path, lambda r: httpx.Response(404, text="nope"))
        assert client.get_json("https://data.sec.gov/x") is None
        assert client.get_json("https://data.sec.gov/x") is None
        assert len(seen) == 1

    def test_retry_after_429_then_cache(self, tmp_path: Path) -> None:
        answers = iter([httpx.Response(429), httpx.Response(200, text='{"ok": true}')])
        client, seen, clock = make_client(tmp_path, lambda r: next(answers))
        assert client.get_json("https://data.sec.gov/r") == {"ok": True}
        assert len(seen) == 2 and client.failures == 0
        assert 2.0 in clock.sleeps  # back-off before the retry

    def test_persistent_5xx_not_cached(self, tmp_path: Path) -> None:
        client, seen, _ = make_client(tmp_path, lambda r: httpx.Response(503))
        assert client.get("https://data.sec.gov/down")[0] == 503
        assert len(seen) == edgar.MAX_ATTEMPTS and client.failures == 1
        assert not list((tmp_path / "cache").glob("*.json"))

    def test_transport_error_retried_without_url_in_body(self, tmp_path: Path) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        client, seen, _ = make_client(tmp_path, boom)
        status, body = client.get("https://data.sec.gov/x?token=abc")
        assert status == -1 and body == "ConnectError"
        assert len(seen) == edgar.MAX_ATTEMPTS

    def test_403_raises(self, tmp_path: Path) -> None:
        client, _, _ = make_client(tmp_path, lambda r: httpx.Response(403, text="UA blocked"))
        with pytest.raises(edgar.EdgarAccessError, match="403"):
            client.get("https://www.sec.gov/files/company_tickers.json")


class TestSubmissions:
    def test_parse_skips_rows_without_acceptance(self) -> None:
        rows = edgar.parse_filings(
            block(
                [
                    ("a", "8-K", "2024-02-05", "2024-02-05T21:10:00.000Z", "2.02,9.01"),
                    ("b", "8-K", "2024-02-05", "", "2.02"),
                    ("c", "4", "2024-02-05", "garbage", ""),
                ]
            )
        )
        assert [f.accession for f in rows] == ["a"]
        assert rows[0].acceptance == parse_edgar_acceptance("2024-02-05T21:10:00Z")
        assert rows[0].acceptance_raw == "2024-02-05T21:10:00.000Z"

    def test_older_page_fetched_only_when_window_predates_recent(self, tmp_path: Path) -> None:
        main = {
            "name": "Forty Two Inc",
            "filings": {
                "recent": block(
                    [("r1", "8-K", "2024-02-06", "2024-02-06T02:00:00.000Z", "2.02")]
                ),
                "files": [
                    {"name": "CIK0000000042-submissions-001.json",
                     "filingFrom": "2015-01-01", "filingTo": "2019-12-31"},
                    {"name": "CIK0000000042-submissions-002.json",
                     "filingFrom": "2001-01-01", "filingTo": "2014-12-31"},
                ],
            },
        }  # fmt: skip
        page = block([("p1", "8-K", "2016-02-05", "2016-02-05T12:00:00.000Z", "2.02")])

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            if name == "CIK0000000042.json":
                return httpx.Response(200, json=main)
            if name == "CIK0000000042-submissions-001.json":
                return httpx.Response(200, json=page)
            return httpx.Response(500)  # page 002 must never be requested

        client, seen, _ = make_client(tmp_path, handler)
        subs = edgar.Submissions(client)
        recent = subs.filings(42, date(2024, 2, 2), date(2024, 2, 6))
        assert recent is not None and [f.accession for f in recent] == ["r1"]
        assert len(seen) == 1
        old = subs.filings(42, date(2016, 2, 4), date(2016, 2, 8))
        assert old is not None and [f.accession for f in old] == ["p1"]
        names = [r.url.path.rsplit("/", 1)[-1] for r in seen]
        assert names == ["CIK0000000042.json", "CIK0000000042-submissions-001.json"]
        assert subs.name(42) == "Forty Two Inc"

    def test_missing_company(self, tmp_path: Path) -> None:
        client, _, _ = make_client(tmp_path, lambda r: httpx.Response(404))
        assert edgar.Submissions(client).filings(7, date(2024, 1, 1), date(2024, 1, 2)) is None


class TestFullText:
    def test_display_names(self) -> None:
        assert edgar.display_tickers("2U, Inc.  (TWOUQ)  (CIK 0001459417)") == {"TWOUQ"}
        assert edgar.display_tickers("Foo Corp  (FOO, FOOW)  (CIK 0000000001)") == {"FOO", "FOOW"}
        assert edgar.display_tickers("GrubHub Inc.  (CIK 0001594109)") == set()

    def test_display_match_goes_first_and_alone(self) -> None:
        hits = [
            {"adsh": "1", "ciks": [5], "display_names": ["Other Co  (OTH)  (CIK 0000000005)"]},
            {"adsh": "2", "ciks": [9], "display_names": ["2U, Inc.  (TWOUQ)  (CIK 0000000009)"]},
        ]
        assert edgar.fts_cik_candidates(hits, "TWOU") == ([9], True, False)

    def test_suffix_match_is_only_the_bankruptcy_q(self) -> None:
        hits = [
            {"adsh": "1", "ciks": [5], "display_names": ["Mueller  (MLI)  (CIK 0000000005)"]},
            {"adsh": "2", "ciks": [6], "display_names": ["Martin M.  (MLM)  (CIK 0000000006)"]},
        ]
        assert edgar.fts_cik_candidates(hits, "ML") == ([5, 6], False, False)
        hits.append(
            {"adsh": "3", "ciks": [7], "display_names": ["MoneyLion  (MLQ)  (CIK 0000000007)"]}
        )
        assert edgar.fts_cik_candidates(hits, "ML") == ([7], True, False)

    def test_many_undifferentiated_candidates_are_ambiguous(self) -> None:
        hits = [
            {"adsh": str(i), "ciks": [i], "display_names": [f"Co{i}  (CIK 000000000{i})"]}
            for i in range(1, 5)
        ]
        assert edgar.fts_cik_candidates(hits, "X") == ([], False, True)
        assert edgar.fts_cik_candidates(hits[:2], "X") == ([1, 2], False, False)

    def test_fts_hits_keep_only_item_202(self, tmp_path: Path) -> None:
        payload = fts_payload(
            [
                hit("0001-16-1", ["0000000001"], ["A  (AAA)  (CIK 0000000001)"], ["2.02", "9.01"]),
                hit("0002-16-1", ["0000000002"], ["B  (CIK 0000000002)"], ["8.01"]),
            ],
            total=150,
        )
        client, seen, _ = make_client(tmp_path, lambda r: httpx.Response(200, json=payload))
        got = edgar.fts_hits(client, "AAA", date(2016, 2, 4), date(2016, 2, 8))
        assert got is not None
        hits, truncated = got
        assert [h["adsh"] for h in hits] == ["0001-16-1"] and hits[0]["ciks"] == [1]
        assert truncated is True
        params = dict(seen[0].url.params)
        assert params["q"] == '"AAA"' and params["forms"] == "8-K"
        assert params["startdt"] == "2016-02-04" and params["enddt"] == "2016-02-13"

    def test_map_lookup_variants(self) -> None:
        m = {"BRK-B": 1, "ABC": 2}
        assert edgar.map_lookup(m, "brk.b") == 1
        assert edgar.map_lookup(m, "abc") == 2
        assert edgar.map_lookup(m, "XYZ") is None

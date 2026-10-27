"""Live checks against sec.gov. Skipped by default; run with ``pytest -m network``.

Needs EARNINGS_SESSION_USER_AGENT. Uses the default response cache, so a
second run makes no requests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from earnings_session import Classifier
from earnings_session.edgar import USER_AGENT_ENV

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def clf() -> Iterator[Classifier]:
    if not os.environ.get(USER_AGENT_ENV):
        pytest.skip(f"{USER_AGENT_ENV} is not set")
    with Classifier() as c:
        yield c


@pytest.mark.parametrize(
    ("event_date", "api_hour"),
    [
        ("2016-01-26", "21:30"),
        ("2019-07-30", "20:30"),
        ("2024-02-01", "21:30"),
        ("2024-08-01", "20:30"),
    ],
)
def test_apple_releases_at_1630_et_and_the_api_is_utc(
    clf: Classifier, event_date: str, api_hour: str
) -> None:
    # the release goes out at 16:30 ET; the 8-K is accepted within a few minutes
    r = clf.classify("AAPL", event_date, label="amc")
    assert (r.rule, r.confidence, r.cik) == ("item_2.02", "high", 320193)
    assert r.accepted_et is not None and "16:30" <= r.accepted_et.strftime("%H:%M") < "16:35"
    assert r.api_value[11:15] == api_hour[:4]
    assert (r.zone_convention, r.session, r.label_agrees) == ("utc", "post_close", True)


@pytest.mark.parametrize(
    ("ticker", "event_date", "accepted"),
    [("OSPN", "2019-10-29", "20:27"), ("PARR", "2020-02-25", "19:48")],
)
def test_evening_releases_labelled_bmo_are_look_ahead(
    clf: Classifier, ticker: str, event_date: str, accepted: str
) -> None:
    r = clf.classify(ticker, event_date, label="bmo")
    assert r.rule == "item_2.02"
    assert r.accepted_et is not None and r.accepted_et.strftime("%Y-%m-%d %H:%M") == (
        f"{event_date} {accepted}"
    )
    assert (r.session, r.verdict, r.label_agrees) == ("post_close", "after_entry", False)


def test_arna_found_by_full_text_and_timed_by_index_page(clf: Classifier) -> None:
    # Arena Pharmaceuticals left the SEC ticker list after its 2022 acquisition.
    # In September 2026 the API gave this filing as "16:18:17Z" (ET labelled Z);
    # by late October 2026 it gave real UTC. The index page has said 16:18:17 ET
    # throughout, which is why the classification reads the page.
    r = clf.classify("ARNA", "2016-05-09")
    assert (r.cik, r.cik_source) == (1080709, "full_text")
    assert r.accession == "0001628280-16-015931"
    assert r.zone_convention in ("utc", "et_as_utc")
    assert r.accepted_et is not None and r.accepted_et.isoformat() == "2016-05-09T16:18:17-04:00"
    assert (r.session, r.verdict) == ("post_close", "after_entry")

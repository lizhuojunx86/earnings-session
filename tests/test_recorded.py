"""End to end on SEC responses recorded 2026-10-27 by scripts/record_fixtures.py."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import make_client, recorded_site

from earnings_session import Classifier, cli


@pytest.fixture
def clf(tmp_path: Path) -> Iterator[Classifier]:
    client, _, _ = make_client(tmp_path, recorded_site())
    with Classifier(client=client) as c:
        yield c


def test_apple_after_the_close_in_utc(clf: Classifier) -> None:
    r = clf.classify("AAPL", "2024-08-01", label="amc")
    assert (r.cik, r.cik_source, r.company) == (320193, "ticker_map", "Apple Inc.")
    assert (r.rule, r.form, r.accession) == ("item_2.02", "8-K", "0000320193-24-000080")
    assert r.api_value == "2024-08-01T20:30:26.000Z"
    assert r.accepted_et is not None and r.accepted_et.isoformat() == "2024-08-01T16:30:26-04:00"
    assert (r.zone_convention, r.session, r.edgar_label) == ("utc", "post_close", "amc")
    assert (r.label_agrees, r.entry_used.isoformat(), r.verdict) == (
        True,
        "2024-08-02",
        "before_entry",
    )
    assert r.confidence == "high"


def test_evening_release_labelled_bmo(clf: Classifier) -> None:
    r = clf.classify("OSPN", "2019-10-29", label="bmo")
    assert (
        r.accepted_et is not None and r.accepted_et.strftime("%Y-%m-%d %H:%M") == "2019-10-29 20:27"
    )
    assert (r.session, r.verdict, r.label_agrees) == ("post_close", "after_entry", False)


def test_delisted_issuer_through_full_text_and_identity_check(clf: Classifier) -> None:
    r = clf.classify("ARNA", "2016-05-09")
    assert (r.cik, r.cik_source, r.cik_evidence) == (
        1080709,
        "full_text",
        "exchange_self_reference",
    )
    assert r.accession == "0001628280-16-015931"
    assert r.accepted_et is not None and r.accepted_et.isoformat() == "2016-05-09T16:18:17-04:00"
    assert (r.session, r.verdict) == ("post_close", "after_entry")
    assert any(n.startswith("identity checks") and ":yes" in n for n in r.notes)


def test_cli_classify_on_recorded_responses(
    clf: Classifier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def factory(*args: Any, **kwargs: Any) -> Classifier:
        return clf

    monkeypatch.setattr(cli, "Classifier", factory)
    assert cli.main(["classify", "--ticker", "AAPL", "--date", "2024-08-01", "--label", "amc"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["session"], out["zone_convention"], out["verdict"]) == (
        "post_close", "utc", "before_entry",
    )  # fmt: skip

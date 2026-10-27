"""CLI commands and the audit report, against a fake sec.gov."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import pytest
from conftest import Row, edgar_site, make_client

from earnings_session import Classifier, cli, wilson
from earnings_session.audit import audit_rows
from earnings_session.core import CSV_COLUMNS
from earnings_session.edgar import USER_AGENT_ENV

# ACME: 8-K 2024-02-06 16:05 ET; BETA: 8-K 2024-02-06 07:00 ET; FPI: 6-K only; GONE: no CIK
ACME: Row = ("0000000011-24-000001", "8-K", "2024-02-06", "2024-02-06T21:05:00.000Z", "2.02")
BETA: Row = ("0000000012-24-000001", "8-K", "2024-02-06", "2024-02-06T12:00:00.000Z", "2.02")
FPI: Row = ("0000000013-24-000001", "6-K", "2024-02-06", "2024-02-06T11:30:00.000Z", "")
PAGES = {
    ACME[0]: "2024-02-06 16:05:00",
    BETA[0]: "2024-02-06 07:00:00",
    FPI[0]: "2024-02-06 06:30:00",
}


@pytest.fixture
def fake_sec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = edgar_site(
        companies={11: [ACME], 12: [BETA], 13: [FPI]},
        pages=PAGES,
        tickers={"ACME": 11, "BETA": 12, "FPI": 13},
    )
    client, _, _ = make_client(tmp_path, site)

    def factory(*args: Any, **kwargs: Any) -> Classifier:
        return Classifier(client=client)

    monkeypatch.setattr(cli, "Classifier", factory)


def write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


EVENTS = [
    {"ticker": "ACME", "event_date": "2024-02-06", "entry_date": "", "vendor_label": "bmo"},
    {"ticker": "BETA", "event_date": "2024-02-06", "entry_date": "", "vendor_label": "bmo"},
    {"ticker": "FPI", "event_date": "2024-02-06", "entry_date": "", "vendor_label": "amc"},
    {"ticker": "GONE", "event_date": "2024-02-06", "entry_date": "2024-02-07", "vendor_label": ""},
]


@pytest.mark.usefixtures("fake_sec")
class TestCommands:
    def test_classify_prints_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.main(["classify", "--ticker", "ACME", "--date", "2024-02-06", "--label", "bmo"])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["session"] == "post_close" and out["verdict"] == "after_entry"
        assert out["label_agrees"] is False and out["accepted_et"] == "2024-02-06T16:05:00-05:00"
        assert out["zone_convention"] == "utc" and out["rule"] == "item_2.02"

    def test_batch_writes_input_plus_result_columns(self, tmp_path: Path) -> None:
        src = write_csv(tmp_path / "in.csv", EVENTS)
        dst = tmp_path / "out.csv"
        assert cli.main(["batch", str(src), str(dst)]) == 0
        with dst.open() as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == [*EVENTS[0], *CSV_COLUMNS]
            rows = list(reader)
        assert [r["verdict"] for r in rows] == ["after_entry", "before_entry", "before_entry", ""]
        assert [r["confidence"] for r in rows] == ["high", "high", "low", ""]
        assert rows[3]["reason"] == "no_cik" and rows[3]["entry_used"] == "2024-02-07"

    def test_audit_from_batch_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = write_csv(tmp_path / "in.csv", EVENTS)
        dst = tmp_path / "out.csv"
        cli.main(["batch", str(src), str(dst)])
        capsys.readouterr()
        assert cli.main(["audit", str(dst), "--json"]) == 0
        rep = json.loads(capsys.readouterr().out)
        main = rep["groups"]["8-K Item 2.02"]
        assert main["n"] == 2
        assert main["confusion"]["bmo"]["amc"] == 1 and main["confusion"]["bmo"]["bmo"] == 1
        assert main["by_vendor_label"]["bmo"]["after_entry"]["k"] == 1
        assert rep["groups"]["6-K proxy (low confidence)"]["n"] == 1
        assert rep["unresolved"] == {
            "n": 1,
            "reasons": {"no_cik": 1},
            "by_vendor_label": {"(none)": 1},
        }
        assert cli.main(["audit", str(dst)]) == 0
        text = capsys.readouterr().out
        assert "== 8-K Item 2.02: n = 2 ==" in text and "after_entry" in text

    def test_audit_classifies_a_plain_input_first(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = write_csv(tmp_path / "in.csv", EVENTS)
        assert cli.main(["audit", str(src), "--json"]) == 0
        captured = capsys.readouterr()
        assert "classifying 4 events first" in captured.err
        assert json.loads(captured.out)["n_events"] == 4

    def test_bad_date_is_reported_with_its_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rows = [*EVENTS[:2], {**EVENTS[2], "event_date": "06/02/2024"}]
        src = write_csv(tmp_path / "in.csv", rows)
        assert cli.main(["batch", str(src), str(tmp_path / "out.csv")]) == cli.EXIT_USAGE
        assert "in.csv:4: bad date" in capsys.readouterr().err

    def test_missing_column(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src = write_csv(tmp_path / "in.csv", [{"symbol": "ACME", "event_date": "2024-02-06"}])
        assert cli.main(["batch", str(src), str(tmp_path / "out.csv")]) == cli.EXIT_USAGE
        assert "missing column(s) ticker" in capsys.readouterr().err


def test_missing_user_agent_exits_with_the_sec_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(USER_AGENT_ENV, raising=False)
    assert cli.main(["classify", "--ticker", "AAPL", "--date", "2024-08-01"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "Sample Company Name AdminContact@<sample company domain>.com" in err
    assert "--user-agent" in err and USER_AGENT_ENV in err


class TestAudit:
    def test_wilson_reference_values(self) -> None:
        lo, hi = wilson(5, 10)
        assert lo == pytest.approx(0.2366, abs=1e-4)
        assert hi == pytest.approx(0.7634, abs=1e-4)
        lo, hi = wilson(0, 10)
        assert lo == 0.0
        assert hi == pytest.approx(0.2775, abs=1e-4)
        # the memo's 56 of 95 after_entry: 58.9% (48.9-68.3%)
        lo, hi = wilson(56, 95)
        assert (round(lo, 3), round(hi, 3)) == (0.489, 0.683)
        assert all(math.isnan(x) for x in wilson(0, 0))

    def test_refuses_rows_that_are_not_batch_output(self) -> None:
        with pytest.raises(ValueError, match="not batch output"):
            audit_rows([{"ticker": "A", "event_date": "2024-01-02"}])

    def test_empty(self) -> None:
        assert audit_rows([])["n_events"] == 0

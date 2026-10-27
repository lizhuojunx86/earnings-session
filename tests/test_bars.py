"""bars: do a backtest's price bars sit on their target days? All offline."""

from __future__ import annotations

import csv
import json
import socket
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from earnings_session import cli
from earnings_session.bars import (
    BAR_COLUMNS,
    EARLY_BAR,
    ENTRY_GAP,
    EXIT_GAP,
    HISTORY_BREAK,
    NO_BAR,
    OK,
    BarCheck,
    PriceTable,
    TradeInput,
    check_trades,
    find_bar,
    read_prices,
    read_trades,
    render_text,
    summarize,
    trading_day_gap,
)
from earnings_session.batch import InputError
from earnings_session.calendar import trading_days
from earnings_session.edgar import USER_AGENT_ENV

D = date.fromisoformat


def sessions(start: str, end: str) -> list[date]:
    lo, hi = D(start), D(end)
    return [d for d in trading_days() if lo <= d <= hi]


def table(**series: list[date]) -> PriceTable:
    return PriceTable(
        {t: sorted(set(ds)) for t, ds in series.items()},
        rows=sum(len(ds) for ds in series.values()),
        duplicates=0,
        out_of_range=0,
    )


def trade(ticker: str, entry: str, exit_: str | None = None, **kw: Any) -> TradeInput:
    return TradeInput(
        line=2,
        ticker=ticker,
        entry_date=D(entry),
        exit_date=D(exit_) if exit_ else None,
        **kw,
    )


def write_csv(
    path: Path,
    header: list[str],
    rows: list[list[str]],
    *,
    bom: bool = False,
    crlf: bool = False,
) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n" if crlf else "\n")
        w.writerow(header)
        w.writerows(rows)
    return path


# ACME: every session 2019-2025. LATE: bars only from 2025-12-08 (history starts late).
# HALT: no bars 2024-05-20 to 2024-05-24 (Memorial Day is 2024-05-27).
ACME = sessions("2019-01-02", "2025-12-31")
LATE = sessions("2025-12-08", "2025-12-31")
HALT = [d for d in ACME if not D("2024-05-20") <= d <= D("2024-05-24")]
PRICES = table(ACME=ACME, LATE=LATE, HALT=HALT)


class TestTradingDayGap:
    def test_on_target(self) -> None:
        assert trading_day_gap(D("2024-02-06"), D("2024-02-06"), "after") == 0

    def test_next_session_is_one_day_late(self) -> None:
        assert trading_day_gap(D("2024-02-06"), D("2024-02-07"), "after") == 1

    def test_weekend_counts_trading_days_only(self) -> None:
        # Friday target, no bar until Tuesday: Friday and Monday were skipped
        assert trading_day_gap(D("2024-05-17"), D("2024-05-21"), "after") == 2
        # Memorial Day Monday is not a trading day, so only Friday was skipped
        assert trading_day_gap(D("2024-05-24"), D("2024-05-28"), "after") == 1

    def test_weekend_target_priced_on_monday_is_on_time(self) -> None:
        assert trading_day_gap(D("2024-03-30"), D("2024-04-01"), "after") == 0
        # Good Friday is a holiday
        assert trading_day_gap(D("2024-03-29"), D("2024-04-01"), "after") == 0

    def test_bar_before_target_is_negative(self) -> None:
        assert trading_day_gap(D("2024-02-06"), D("2024-02-05"), "after") == -1

    def test_before_side(self) -> None:
        assert trading_day_gap(D("2024-02-09"), D("2024-02-09"), "before") == 0
        assert trading_day_gap(D("2024-02-09"), D("2024-02-08"), "before") == -1
        # Saturday target: Friday is the bar a "last on or before" rule should find
        assert trading_day_gap(D("2024-02-10"), D("2024-02-09"), "before") == 0
        assert trading_day_gap(D("2024-02-10"), D("2024-02-12"), "before") == 1


class TestFindBar:
    def test_unbounded_search_takes_any_later_bar(self) -> None:
        assert find_bar(LATE, D("2019-06-28"), "after") == D("2025-12-08")

    def test_max_gap_refuses_a_distant_bar(self) -> None:
        assert find_bar(LATE, D("2019-06-28"), "after", max_gap_td=5) is None
        assert find_bar(HALT, D("2024-05-20"), "after", max_gap_td=4) is None
        assert find_bar(HALT, D("2024-05-20"), "after", max_gap_td=5) == D("2024-05-28")

    def test_before_side(self) -> None:
        assert find_bar(ACME, D("2024-02-10"), "before") == D("2024-02-09")
        assert find_bar(LATE, D("2025-12-01"), "before") is None

    def test_edges(self) -> None:
        assert find_bar([], D("2024-02-06")) is None
        assert find_bar(ACME, D("2026-03-02"), "after") is None
        with pytest.raises(ValueError, match="side"):
            find_bar(ACME, D("2024-02-06"), "sideways")  # type: ignore[arg-type]


def one(t: TradeInput, prices: PriceTable | None = PRICES, **kw: Any) -> BarCheck:
    (check,) = check_trades([t], prices, **kw)
    return check


class TestCheckTrades:
    def test_clean_trade(self) -> None:
        c = one(trade("ACME", "2024-02-06", "2024-02-13"))
        assert c.flag == OK and c.notes == []
        assert (c.entry_gap_td, c.entry_gap_cd, c.exit_gap_td) == (0, 0, 0)

    @pytest.mark.parametrize(
        ("entry", "target"),
        [
            ("2024-03-30", "2024-04-01"),  # Saturday
            ("2024-03-29", "2024-04-01"),  # Good Friday
            ("2024-11-28", "2024-11-29"),  # Thanksgiving; the next day is an early close
        ],
    )
    def test_non_trading_target_moves_without_a_flag(self, entry: str, target: str) -> None:
        c = one(trade("ACME", entry))
        assert c.flag == OK and c.entry_target == D(target) and c.entry_gap_td == 0
        assert c.notes == [f"entry_date {entry} is not a trading day; target {target}"]

    def test_late_history_is_a_history_break(self) -> None:
        c = one(trade("LATE", "2019-06-28"))
        assert c.flag == HISTORY_BREAK and c.entry_bar == D("2025-12-08")
        assert c.entry_gap_td is not None and c.entry_gap_td > 1500
        assert c.notes == ["also entry_gap"]

    def test_missing_ticker_is_no_bar(self) -> None:
        c = one(trade("GONE", "2024-02-06", "2024-02-13"))
        assert c.flag == NO_BAR and c.entry_bar is None and c.exit_bar is None
        assert "ticker not in the price table" in c.notes

    def test_missing_days_are_an_entry_gap(self) -> None:
        c = one(trade("HALT", "2024-05-20", "2024-05-31"))
        assert c.flag == ENTRY_GAP
        assert (c.entry_bar, c.entry_gap_td, c.entry_gap_cd) == (D("2024-05-28"), 5, 8)
        assert one(trade("HALT", "2024-05-20"), max_gap=5).flag == OK

    def test_exit_after_last_bar_is_an_exit_gap(self) -> None:
        # bars stop on Friday 2024-02-09; the exit target is the next Friday, 5 sessions on
        short = table(SHRT=sessions("2024-01-02", "2024-02-09"))
        c = one(trade("SHRT", "2024-02-06", "2024-02-16"), short)
        assert c.flag == EXIT_GAP and c.exit_bar == D("2024-02-09")
        assert (c.exit_gap_td, c.exit_gap_cd) == (-5, -7)

    def test_most_serious_flag_wins(self) -> None:
        c = one(trade("LATE", "2019-06-28", "2019-07-31"))
        assert c.flag == HISTORY_BREAK
        assert c.exit_bar is None and "also no_bar, entry_gap" in c.notes

    def test_recorded_bars_before_entry_are_early(self) -> None:
        t = trade("ACME", "2024-02-06", entry_bar_date=D("2024-02-05"))
        c = one(t, None)
        assert c.flag == EARLY_BAR and c.entry_gap_td == -1

    def test_recorded_bars_without_exit_bar_skip_the_exit(self) -> None:
        t = trade("ACME", "2024-02-06", "2024-02-13", entry_bar_date=D("2024-02-06"))
        c = one(t, None)
        assert c.flag == OK and c.exit_target is None
        assert c.notes == ["exit not checked: no exit_bar_date"]

    def test_recorded_entry_missing_is_no_bar(self) -> None:
        c = one(trade("ACME", "2024-02-06"), None)
        assert c.flag == NO_BAR and c.notes == ["no entry_bar_date"]

    def test_before_side_entry(self) -> None:
        # "last bar on or before": the bars for 05-20 and 05-21 are missing, so 05-17 is used
        c = one(trade("HALT", "2024-05-21"), entry_side="before")
        assert c.entry_bar == D("2024-05-17") and c.entry_gap_td == -2 and c.flag == ENTRY_GAP

    def test_negative_limits_are_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            check_trades([], PRICES, max_gap=-1)


class TestReadFiles:
    def test_prices_with_bom_crlf_case_and_duplicates(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "p.csv",
            ["Date", " Ticker ", "Close"],
            [
                ["2024-02-06", " acme ", "1"],
                ["2024-02-06 00:00:00", "ACME", "1"],
                ["2024-02-07", "acme", "1"],
                ["1990-01-02", "ACME", "1"],
                ["", "", ""],
            ],
            bom=True,
            crlf=True,
        )
        p = read_prices(path)
        assert p.dates == {"ACME": [D("2024-02-06"), D("2024-02-07")]}
        assert (p.rows, p.duplicates, p.out_of_range, p.tickers) == (4, 1, 1, 1)

    def test_prices_missing_column(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "p.csv", ["ticker", "close"], [["ACME", "1"]])
        with pytest.raises(InputError, match="missing column"):
            read_prices(path)

    def test_prices_bad_date_has_its_line(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "p.csv", ["ticker", "date"], [["A", "2024-02-06"], ["A", "x"]])
        with pytest.raises(InputError, match=r"p\.csv:3: bad date"):
            read_prices(path)

    def test_trades(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "t.csv",
            ["Ticker", "Entry_Date", "exit_date", "return"],
            [
                [" acme ", "2024-02-06", "2024-02-13", "0.05"],
                ["beta", "2024-02-06", "", ""],
                ["gamma", "2024-02-06", "", "n/a"],
            ],
            bom=True,
        )
        header, trades = read_trades(path)
        assert header == ["Ticker", "Entry_Date", "exit_date", "return"]
        assert [t.ticker for t in trades] == ["ACME", "BETA", "GAMMA"]
        assert trades[0].exit_date == D("2024-02-13") and trades[0].ret == 0.05
        assert (trades[1].ret, trades[1].ret_given) == (None, False)
        assert (trades[2].ret, trades[2].ret_given) == (None, True)

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            (["ACME", "06/02/2024", ""], r"t\.csv:2: bad entry_date"),
            (["ACME", "1990-01-02", ""], r"t\.csv:2: entry_date 1990-01-02 is outside"),
            (["ACME", "2024-02-06", "2024-02-01"], r"t\.csv:2: exit_date .* before entry_date"),
            (["", "2024-02-06", ""], r"t\.csv:2: empty ticker"),
        ],
    )
    def test_bad_trade_rows(self, tmp_path: Path, row: list[str], message: str) -> None:
        path = write_csv(tmp_path / "t.csv", ["ticker", "entry_date", "exit_date"], [row])
        with pytest.raises(InputError, match=message):
            read_trades(path)


class TestSummary:
    def test_returns_of_flagged_and_clean_rows(self) -> None:
        checks = check_trades(
            [
                trade("ACME", "2024-02-06", ret=0.01, ret_given=True),
                trade("LATE", "2019-06-28", ret=0.09, ret_given=True),
                trade("ACME", "2024-02-07", ret=None, ret_given=True),
            ],
            PRICES,
        )
        rep = summarize(checks, prices=PRICES, has_returns=True)
        r = rep["returns"]
        assert (r["n_valid"], r["n_invalid"]) == (2, 1)
        assert r["flagged"] == {"n": 1, "mean": 0.09, "sum": 0.09}
        assert r["ok"]["n"] == 2 - 1 and r["flagged_share_of_total"] == pytest.approx(0.9)
        assert rep["flags"][HISTORY_BREAK]["k"] == 1 and rep["flags"][OK]["k"] == 2
        assert rep["entry_gap_td"][">250"] == 1 and rep["entry_gap_td"]["0"] == 2
        assert rep["exit_gap_td"] is None
        text = render_text(rep)
        assert "flagged rows carry 90.0% of the total" in text
        assert "1 rows with a non-numeric return were left out" in text

    def test_without_a_return_column(self) -> None:
        rep = summarize(check_trades([trade("ACME", "2024-02-06")], PRICES))
        assert rep["returns"] is None and rep["prices"] is None

    def test_empty(self) -> None:
        rep = summarize([])
        assert rep["n_trades"] == 0 and render_text(rep).endswith("no trades")


def write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    prices = write_csv(
        tmp_path / "prices.csv",
        ["date", "ticker", "close"],
        [[d.isoformat(), t, "1"] for t, ds in (("ACME", ACME), ("LATE", LATE)) for d in ds],
    )
    trades = write_csv(
        tmp_path / "trades.csv",
        ["ticker", "entry_date", "exit_date", "return", "strategy"],
        [
            ["ACME", "2024-02-06", "2024-02-13", "0.012", "s1"],
            ["LATE", "2019-06-28", "2019-07-31", "0.083", "s1"],
        ],
    )
    return trades, prices


class TestCommand:
    def test_text_and_csv(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        trades, prices = write_inputs(tmp_path)
        out = tmp_path / "checked.csv"
        assert cli.main(["bars", str(trades), "--prices", str(prices), "-o", str(out)]) == 0
        text = capsys.readouterr().out
        assert "flags (Wilson 95%)" in text and "history_break 1/2" in text
        with out.open() as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == [
                "ticker",
                "entry_date",
                "exit_date",
                "return",
                "strategy",
                *BAR_COLUMNS,
            ]
            rows = list(reader)
        assert [r["flag"] for r in rows] == [OK, HISTORY_BREAK]
        assert rows[1]["entry_bar_date"] == "2025-12-08" and rows[1]["strategy"] == "s1"

    def test_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        trades, prices = write_inputs(tmp_path)
        assert cli.main(["bars", str(trades), "--prices", str(prices), "--json"]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["n_trades"] == 2 and rep["bars_from"] == "prices"
        assert rep["returns"]["flagged"]["n"] == 1

    def test_fail_on_flag(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        trades, prices = write_inputs(tmp_path)
        args = ["bars", str(trades), "--prices", str(prices), "--fail-on-flag"]
        assert cli.main(args) == cli.EXIT_FLAGGED
        clean = write_csv(
            tmp_path / "clean.csv", ["ticker", "entry_date"], [["ACME", "2024-02-06"]]
        )
        assert cli.main(["bars", str(clean), "--prices", str(prices), "--fail-on-flag"]) == 0
        capsys.readouterr()

    def test_recorded_bars_without_prices(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        trades = write_csv(
            tmp_path / "t.csv",
            ["ticker", "entry_date", "entry_bar_date"],
            [["ACME", "2024-02-06", "2024-02-05"], ["ACME", "2024-02-07", "2024-02-07"]],
        )
        assert cli.main(["bars", str(trades), "--json"]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["bars_from"] == "trades" and rep["flags"][EARLY_BAR]["k"] == 1

    def test_needs_prices_or_recorded_bars(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        trades = write_csv(tmp_path / "t.csv", ["ticker", "entry_date"], [["A", "2024-02-06"]])
        assert cli.main(["bars", str(trades)]) == cli.EXIT_USAGE
        assert "pass --prices" in capsys.readouterr().err

    def test_negative_max_gap(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        trades, prices = write_inputs(tmp_path)
        assert cli.main(["bars", str(trades), "--prices", str(prices), "--max-gap", "-1"]) == 2
        assert "must be 0 or more" in capsys.readouterr().err

    def test_runs_offline_without_a_user_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def no_network(*args: object, **kwargs: object) -> None:
            raise AssertionError("bars must not open a connection")

        monkeypatch.delenv(USER_AGENT_ENV, raising=False)
        monkeypatch.setattr(socket.socket, "connect", no_network)
        trades, prices = write_inputs(tmp_path)
        assert cli.main(["bars", str(trades), "--prices", str(prices)]) == 0
        capsys.readouterr()


def test_large_inputs_are_fast(tmp_path: Path) -> None:
    days = sessions("2016-01-04", "2025-12-31")
    tickers = [f"T{i:02d}" for i in range(40)]  # 40 x ~2,500 sessions: about 100,000 rows
    prices = write_csv(
        tmp_path / "p.csv",
        ["ticker", "date"],
        [[t, d.isoformat()] for t in tickers for d in days],
    )
    trades = write_csv(
        tmp_path / "t.csv",
        ["ticker", "entry_date", "exit_date"],
        [
            [tickers[i % 40], days[i % 2000].isoformat(), days[i % 2000 + 5].isoformat()]
            for i in range(10_000)
        ],
    )
    start = time.perf_counter()
    _, ts = read_trades(trades)
    checks = check_trades(ts, read_prices(prices))
    assert len(checks) == 10_000 and all(c.flag == OK for c in checks)
    assert time.perf_counter() - start < 5

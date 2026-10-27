"""Session classifier, entry verdict, labels and filing selection.

Timestamps are given in UTC, the zone EDGAR's acceptanceDateTime claims, so
every case also goes through the EST/EDT conversion.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from conftest import utc

from earnings_session import calendar as cal
from earnings_session import timing as t


def filing(acc: str, ts: datetime, form: str = "8-K", items: str = "2.02,9.01") -> t.Filing:
    return t.Filing(acc, form, items, ts, ts.astimezone(t.ET).date())


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class TestCalendar:
    def test_special_closures_match_xnys(self) -> None:
        # The explicit list and exchange_calendars must agree; a library update
        # that drops one fails here instead of moving acceptances silently.
        for d in cal.SPECIAL_CLOSURES:
            assert not cal._xnys().is_session(d.isoformat())

    @pytest.mark.parametrize(
        "day",
        [date(2024, 10, 14), date(2025, 10, 13), date(2024, 11, 11), date(2025, 11, 11)],
    )
    def test_columbus_and_veterans_day_trade(self, day: date) -> None:
        assert cal.is_trading_day(day)

    def test_out_of_range_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="outside the XNYS calendar"):
            cal.is_trading_day(date(1980, 1, 2))

    def test_session_times_are_utc_instants(self) -> None:
        assert cal.session_open(date(2024, 1, 10)) == utc(2024, 1, 10, 14, 30)
        assert cal.session_close(date(2024, 7, 10)) == utc(2024, 7, 10, 20, 0)
        assert cal.session_close(date(2024, 11, 29)) == utc(2024, 11, 29, 18, 0)  # 13:00 EST
        with pytest.raises(ValueError, match="not an NYSE trading day"):
            cal.session_close(date(2024, 11, 28))


# ---------------------------------------------------------------------------
# classify_acceptance
# ---------------------------------------------------------------------------


class TestSessionBoundaries:
    @pytest.mark.parametrize(
        ("ts", "label"),
        [
            # January = EST (UTC-5)
            (utc(2024, 1, 10, 14, 29, 59), t.PRE_OPEN),  # 09:29:59
            (utc(2024, 1, 10, 14, 30), t.INTRADAY),  # 09:30:00 is the open
            (utc(2024, 1, 10, 20, 59, 59), t.INTRADAY),  # 15:59:59
            (utc(2024, 1, 10, 21, 0), t.POST_CLOSE),  # 16:00:00 is the close
            (utc(2024, 1, 10, 11, 0), t.PRE_OPEN),  # 06:00, EDGAR opens
            # July = EDT (UTC-4): the same UTC hours land an hour later
            (utc(2024, 7, 10, 13, 29, 59), t.PRE_OPEN),
            (utc(2024, 7, 10, 13, 30), t.INTRADAY),
            (utc(2024, 7, 10, 19, 59, 59), t.INTRADAY),
            (utc(2024, 7, 10, 20, 0), t.POST_CLOSE),
            (utc(2024, 7, 10, 14, 29, 59), t.INTRADAY),  # 10:29:59 EDT, pre-open in EST
            (utc(2024, 7, 10, 20, 59, 59), t.POST_CLOSE),  # 16:59:59 EDT
        ],
    )
    def test_boundaries(self, ts: datetime, label: str) -> None:
        s = t.classify_acceptance(ts)
        assert s.label == label
        assert s.session_date == ts.astimezone(t.ET).date()
        assert s.rolled is False

    def test_dst_switch_week(self) -> None:
        # DST started Sunday 2024-03-10: 13:30Z is 08:30 EST on Friday, 09:30 EDT on Monday.
        assert t.classify_acceptance(utc(2024, 3, 8, 13, 30)).label == t.PRE_OPEN
        assert t.classify_acceptance(utc(2024, 3, 11, 13, 30)).label == t.INTRADAY
        # DST ended Sunday 2024-11-03: 20:30Z is 16:30 EDT on Friday, 15:30 EST on Monday.
        assert t.classify_acceptance(utc(2024, 11, 1, 20, 30)).label == t.POST_CLOSE
        assert t.classify_acceptance(utc(2024, 11, 4, 20, 30)).label == t.INTRADAY

    def test_utc_next_day_is_still_the_et_day(self) -> None:
        # 2024-02-07 02:00Z is 2024-02-06 21:00 EST: post-close on the 6th.
        s = t.classify_acceptance(utc(2024, 2, 7, 2, 0))
        assert (s.label, s.session_date) == (t.POST_CLOSE, date(2024, 2, 6))
        # Apple's 16:30 ET release in summer reads 20:30Z, in winter 21:30Z
        assert t.classify_acceptance(utc(2024, 8, 1, 20, 30, 28)).label == t.POST_CLOSE
        assert t.classify_acceptance(utc(2024, 2, 1, 21, 30, 33)).label == t.POST_CLOSE

    def test_zone_convention(self) -> None:
        apple_api = t.parse_edgar_acceptance("2026-07-30T20:30:28.000Z")
        assert t.zone_convention(apple_api, datetime(2026, 7, 30, 16, 30, 28)) == "utc"
        # ARNA 2016-05-09: API "16:18:17Z", index page "Accepted 16:18:17" (ET)
        arna_api = t.parse_edgar_acceptance("2016-05-09T16:18:17.000Z")
        assert t.zone_convention(arna_api, datetime(2016, 5, 9, 16, 18, 17)) == "et_as_utc"
        assert t.zone_convention(arna_api, datetime(2016, 5, 9, 17, 0, 0)) == "mismatch"
        jan = t.parse_edgar_acceptance("2026-01-29T21:30:33.000Z")
        assert t.zone_convention(jan, datetime(2026, 1, 29, 16, 30, 33)) == "utc"

    def test_et_labelled_z_changes_the_verdict(self) -> None:
        # Read as UTC, ARNA's "16:18:17Z" is 12:18 EDT (intraday); the ET truth is post-close.
        api = t.parse_edgar_acceptance("2016-05-09T16:18:17.000Z")
        assert t.classify_acceptance(api).label == t.INTRADAY
        page = t.et_wall_clock(datetime(2016, 5, 9, 16, 18, 17))
        assert t.classify_acceptance(page).label == t.POST_CLOSE
        assert t.entry_verdict(t.classify_acceptance(page), date(2016, 5, 9)) == t.AFTER_ENTRY

    def test_et_wall_clock(self) -> None:
        assert t.et_wall_clock(datetime(2016, 5, 9, 16, 18, 17)) == utc(2016, 5, 9, 20, 18, 17)
        assert t.et_wall_clock(datetime(2016, 1, 26, 16, 30)) == utc(2016, 1, 26, 21, 30)
        with pytest.raises(ValueError, match="naive"):
            t.et_wall_clock(utc(2016, 1, 26, 16, 30))

    def test_naive_timestamp_refused(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            t.classify_acceptance(datetime(2024, 1, 10, 15, 0))

    def test_zoneless_acceptance_string_refused(self) -> None:
        with pytest.raises(ValueError, match="zone"):
            t.parse_edgar_acceptance("2024-01-10T15:00:00")


class TestNonTradingDays:
    def test_saturday_rolls_to_friday_post_close(self) -> None:
        s = t.classify_acceptance(utc(2024, 2, 3, 15, 0))  # Sat 10:00 ET
        assert s == t.Session(t.POST_CLOSE, date(2024, 2, 2), rolled=True)

    def test_sunday_evening_rolls_to_friday(self) -> None:
        s = t.classify_acceptance(utc(2024, 2, 5, 1, 0))  # Sun 2024-02-04 20:00 ET
        assert s == t.Session(t.POST_CLOSE, date(2024, 2, 2), rolled=True)

    def test_good_friday(self) -> None:
        s = t.classify_acceptance(utc(2024, 3, 29, 11, 0))  # 07:00 EDT, NYSE closed
        assert s == t.Session(t.POST_CLOSE, date(2024, 3, 28), rolled=True)

    def test_juneteenth_from_2022(self) -> None:
        s = t.classify_acceptance(utc(2023, 6, 19, 14, 0))
        assert s == t.Session(t.POST_CLOSE, date(2023, 6, 16), rolled=True)
        s = t.classify_acceptance(utc(2022, 6, 20, 14, 0))  # first observance, a Monday
        assert s == t.Session(t.POST_CLOSE, date(2022, 6, 17), rolled=True)
        # 2021-06-18, the Friday before the first observed Juneteenth, was a trading day
        assert t.classify_acceptance(utc(2021, 6, 18, 14, 0)).label == t.INTRADAY

    @pytest.mark.parametrize("day", [date(2024, 10, 14), date(2025, 11, 11)])
    def test_columbus_and_veterans_day_are_trading_days(self, day: date) -> None:
        ts = datetime(day.year, day.month, day.day, 10, 0, tzinfo=t.ET)
        s = t.classify_acceptance(ts)
        assert (s.label, s.session_date, s.rolled) == (t.INTRADAY, day, False)

    @pytest.mark.parametrize(
        ("closed", "previous"),
        [(date(2018, 12, 5), date(2018, 12, 4)), (date(2025, 1, 9), date(2025, 1, 8))],
    )
    def test_unscheduled_closures(self, closed: date, previous: date) -> None:
        ts = datetime(closed.year, closed.month, closed.day, 8, 0, tzinfo=t.ET)
        assert t.classify_acceptance(ts) == t.Session(t.POST_CLOSE, previous, rolled=True)
        assert not cal.is_trading_day(closed)
        assert cal.next_trading_day(closed) == closed + timedelta(days=1)

    def test_christmas_observed_friday(self) -> None:
        # 2021-12-25 fell on a Saturday; NYSE closed Friday 2021-12-24.
        s = t.classify_acceptance(datetime(2021, 12, 24, 13, 30, tzinfo=t.ET))
        assert s == t.Session(t.POST_CLOSE, date(2021, 12, 23), rolled=True)


class TestEarlyClose:
    @pytest.mark.parametrize(
        "day", [date(2024, 11, 29), date(2024, 12, 24), date(2024, 7, 3), date(2019, 7, 3)]
    )
    def test_closes_at_one(self, day: date) -> None:
        assert cal.is_early_close(day)
        before = datetime(day.year, day.month, day.day, 12, 59, 59, tzinfo=t.ET)
        after = datetime(day.year, day.month, day.day, 13, 0, tzinfo=t.ET)
        assert t.classify_acceptance(before).label == t.INTRADAY
        assert t.classify_acceptance(after).label == t.POST_CLOSE

    @pytest.mark.parametrize(
        "day",
        [
            date(2024, 7, 2),  # ordinary Tuesday
            date(2024, 11, 28),  # Thanksgiving itself (closed)
            date(2020, 7, 3),  # observed Independence Day (closed)
            date(2021, 12, 24),  # observed Christmas (closed)
        ],
    )
    def test_not_early(self, day: date) -> None:
        assert not cal.is_early_close(day)

    def test_normal_close_on_ordinary_day(self) -> None:
        ts = datetime(2024, 7, 2, 14, 0, tzinfo=t.ET)
        assert t.classify_acceptance(ts).label == t.INTRADAY


# ---------------------------------------------------------------------------
# entry_verdict / reaction_date / labels
# ---------------------------------------------------------------------------


class TestEntryVerdict:
    E = date(2024, 2, 6)  # Tuesday

    @pytest.mark.parametrize(
        ("session", "verdict"),
        [
            (t.Session(t.PRE_OPEN, date(2024, 2, 6)), t.BEFORE_ENTRY),
            (t.Session(t.INTRADAY, date(2024, 2, 6)), t.ENTRY_SESSION),
            (t.Session(t.POST_CLOSE, date(2024, 2, 6)), t.AFTER_ENTRY),
            (t.Session(t.POST_CLOSE, date(2024, 2, 5)), t.BEFORE_ENTRY),
            (t.Session(t.INTRADAY, date(2024, 2, 5)), t.BEFORE_ENTRY),
            (t.Session(t.PRE_OPEN, date(2024, 2, 7)), t.AFTER_ENTRY),
            (t.Session(t.INTRADAY, date(2024, 2, 8)), t.AFTER_ENTRY),
        ],
    )
    def test_relative_to_entry_open(self, session: t.Session, verdict: str) -> None:
        assert t.entry_verdict(session, self.E) == verdict

    def test_weekend_release(self) -> None:
        s = t.classify_acceptance(utc(2024, 2, 3, 15, 0))  # Saturday
        assert t.entry_verdict(s, date(2024, 2, 5)) == t.BEFORE_ENTRY  # Monday entry
        assert t.entry_verdict(s, date(2024, 2, 2)) == t.AFTER_ENTRY  # Friday entry

    def test_reaction_date(self) -> None:
        assert t.reaction_date(t.Session(t.PRE_OPEN, date(2024, 2, 6))) == date(2024, 2, 6)
        assert t.reaction_date(t.Session(t.INTRADAY, date(2024, 2, 6))) == date(2024, 2, 7)
        # Friday post-close -> Monday; Thursday before Good Friday -> Monday
        assert t.reaction_date(t.Session(t.POST_CLOSE, date(2024, 2, 2))) == date(2024, 2, 5)
        assert t.reaction_date(t.Session(t.POST_CLOSE, date(2024, 3, 28))) == date(2024, 4, 1)


class TestLabels:
    def test_normalize(self) -> None:
        assert t.normalize_label(" BMO ") == "bmo"
        assert t.normalize_label("amc") == "amc"
        assert t.normalize_label("time-not-supplied") is None
        assert t.normalize_label(None) is None

    @pytest.mark.parametrize(
        ("session", "label"),
        [
            (t.Session(t.PRE_OPEN, date(2024, 2, 6)), "bmo"),
            (t.Session(t.INTRADAY, date(2024, 2, 6)), "dmh"),
            (t.Session(t.POST_CLOSE, date(2024, 2, 6)), "amc"),
            (t.Session(t.POST_CLOSE, date(2024, 2, 5)), t.PRIOR_DAY),
            (t.Session(t.PRE_OPEN, date(2024, 2, 7)), t.LATER_DAY),
        ],
    )
    def test_edgar_label(self, session: t.Session, label: str) -> None:
        assert t.edgar_label(session, date(2024, 2, 6)) == label

    def test_edgar_label_for_a_weekend_event_date(self) -> None:
        # event dated Saturday: its trading day is Monday
        assert t.edgar_label(t.Session(t.PRE_OPEN, date(2024, 2, 5)), date(2024, 2, 3)) == "bmo"

    def test_implied_entry(self) -> None:
        assert t.implied_entry(date(2024, 2, 6), "bmo") == date(2024, 2, 6)
        assert t.implied_entry(date(2024, 2, 6), None) == date(2024, 2, 6)
        assert t.implied_entry(date(2024, 2, 6), "dmh") == date(2024, 2, 6)
        assert t.implied_entry(date(2024, 2, 6), "amc") == date(2024, 2, 7)
        assert t.implied_entry(date(2024, 2, 2), "amc") == date(2024, 2, 5)  # Fri -> Mon
        assert t.implied_entry(date(2024, 3, 28), "amc") == date(2024, 4, 1)  # Good Friday


# ---------------------------------------------------------------------------
# Filing selection
# ---------------------------------------------------------------------------


class TestFilingSelection:
    def test_item_202_parsing(self) -> None:
        assert t.has_item_202("2.02,9.01")
        assert t.has_item_202(" 7.01, 2.02 ")
        assert not t.has_item_202("5.02,9.01")
        assert not t.has_item_202("12.02")
        assert not t.has_item_202(None)
        assert not t.has_item_202("")

    def test_window_spans_neighbouring_trading_days(self) -> None:
        assert t.filing_window(date(2024, 2, 5), date(2024, 2, 5)) == (
            date(2024, 2, 2),
            date(2024, 2, 6),
        )
        assert t.filing_window(date(2024, 2, 2)) == (date(2024, 2, 1), date(2024, 2, 5))
        # Tuesday after MLK Day: start skips the holiday
        assert t.filing_window(date(2024, 1, 16), date(2024, 1, 16))[0] == date(2024, 1, 12)
        # event on a Saturday, entry Monday: the window reaches Tuesday
        assert t.filing_window(date(2024, 2, 3), date(2024, 2, 5)) == (
            date(2024, 2, 2),
            date(2024, 2, 6),
        )

    def test_amendment_excluded(self) -> None:
        d = date(2024, 2, 5)
        files = [
            filing("prev", utc(2024, 2, 2, 22, 0)),  # Fri 17:00, one trading day early
            filing("same", utc(2024, 2, 5, 21, 10)),  # Mon 16:10
            filing("amend", utc(2024, 2, 5, 20, 0), form="8-K/A"),
            filing("other", utc(2024, 2, 5, 15, 0), items="5.02"),
            filing("far", utc(2024, 2, 9, 12, 0)),
        ]
        best, n = t.pick_earnings_filing(files, d, d)
        assert best is not None and best.accession == "same"
        assert n == 2  # prev + same; the amendment, non-2.02 and out-of-window rows are ignored
        only_amended = [filing("amend", utc(2024, 2, 5, 20, 0), form="8-K/A")]
        assert t.pick_earnings_filing(only_amended, d, d) == (None, 0)

    def test_two_202_same_window_nearest_then_earliest(self) -> None:
        d = date(2024, 2, 5)
        files = [filing("late", utc(2024, 2, 5, 22, 0)), filing("early", utc(2024, 2, 5, 12, 0))]
        best, n = t.pick_earnings_filing(files, d, d)
        assert best is not None and best.accession == "early" and n == 2
        # a 2.02 on the event date beats an earlier one the day before
        files = [filing("before", utc(2024, 2, 2, 13, 0)), filing("on", utc(2024, 2, 5, 22, 0))]
        best, _ = t.pick_earnings_filing(files, d, d)
        assert best is not None and best.accession == "on"

    def test_matches_on_acceptance_et_date_not_filing_date(self) -> None:
        # Accepted Tue 21:00 ET (Wed 02:00Z); EDGAR dates the filing Wednesday.
        ts = utc(2024, 2, 7, 2, 0)
        f = t.Filing("late-evening", "8-K", "2.02", ts, date(2024, 2, 7))
        best, n = t.pick_earnings_filing([f], date(2024, 2, 5), date(2024, 2, 5))
        assert best is f and n == 1
        # the same acceptance one ET day later falls outside the window
        g = t.Filing("outside", "8-K", "2.02", ts + timedelta(days=1), date(2024, 2, 8))
        assert t.pick_earnings_filing([g], date(2024, 2, 5), date(2024, 2, 5)) == (None, 0)

    def test_6k_proxy(self) -> None:
        d = date(2024, 2, 5)
        files = [
            filing("early", utc(2024, 2, 2, 15, 0), form="6-K", items=""),
            filing("near", utc(2024, 2, 5, 12, 0), form="6-K", items=""),
            filing("amended", utc(2024, 2, 5, 11, 0), form="6-K/A", items=""),
            filing("eightk", utc(2024, 2, 5, 11, 0), items="2.02"),
        ]
        best, n = t.pick_6k_proxy(files, d, d)
        assert best is not None and best.accession == "near" and n == 2
        assert t.pick_6k_proxy(files[2:], d, d) == (None, 0)

    def test_fallback_prefers_any_8k_then_periodic(self) -> None:
        d = date(2024, 2, 5)
        files = [
            filing("tenq", utc(2024, 2, 5, 21, 0), form="10-Q", items=""),
            filing("other", utc(2024, 2, 6, 12, 0), items="7.01,9.01"),
            filing("amend", utc(2024, 2, 5, 12, 0), form="8-K/A", items="7.01"),
        ]
        best, n = t.pick_fallback_filing(files, d, d)
        assert best is not None and best.accession == "other" and n == 1
        best, n = t.pick_fallback_filing(files[:1], d, d)
        assert best is not None and best.form == "10-Q"
        assert t.pick_fallback_filing(files[2:], d, d) == (None, 0)

    def test_unresolved_reason(self) -> None:
        start, end = date(2024, 2, 2), date(2024, 2, 6)
        in_win = utc(2024, 2, 5, 15, 0)
        assert (
            t.unresolved_reason([filing("a", in_win, items="5.02")], start, end) == "8k_without_202"
        )
        assert t.unresolved_reason([filing("b", in_win, form="6-K")], start, end) == "6k_only"
        assert (
            t.unresolved_reason([filing("c", utc(2024, 3, 1, 15, 0))], start, end)
            == "no_filing_in_window"
        )
        assert t.unresolved_reason([], start, end) == "no_filing_in_window"

"""Issuer identification, filing choice and the classified result, against a fake sec.gov."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from conftest import Row, edgar_site, fts_payload, hit, make_client

from earnings_session import Classifier
from earnings_session.core import CSV_COLUMNS

EVENT = date(2016, 2, 25)  # Thursday
RELEASE: Row = (
    "0001104659-16-099992", "8-K", "2016-02-25", "2016-02-25T21:05:00.000Z", "2.02,9.01",
)  # fmt: skip
RELEASE_PAGE = {RELEASE[0]: "2016-02-25 16:05:00"}


def classifier(tmp_path: Path, **site: object) -> tuple[Classifier, list[object]]:
    client, seen, _ = make_client(tmp_path, edgar_site(**site))  # type: ignore[arg-type]
    return Classifier(client=client), seen  # type: ignore[return-value]


class TestIssuer:
    def test_mapped_ticker(self, tmp_path: Path) -> None:
        fts = fts_payload([hit(RELEASE[0], ["0000000011"], ["Acme  (ACME)  (CIK 0000000011)"],
                               ["2.02", "9.01"])])  # fmt: skip
        c, _ = classifier(
            tmp_path, companies={11: [RELEASE]}, fts=fts, pages=RELEASE_PAGE,
            tickers={"ACME": 11}, names={11: "Acme Corp"},
        )  # fmt: skip
        r = c.classify("acme", EVENT)
        assert (r.status, r.rule, r.confidence) == ("resolved", "item_2.02", "high")
        assert (r.cik, r.cik_source, r.company) == (11, "ticker_map", "Acme Corp")
        assert (
            r.accepted_et is not None and r.accepted_et.isoformat() == "2016-02-25T16:05:00-05:00"
        )
        assert (r.zone_convention, r.api_value) == ("utc", "2016-02-25T21:05:00.000Z")
        assert (r.session, r.session_date, r.verdict) == ("post_close", EVENT, "after_entry")
        assert r.entry_source == "bmo_assumed" and r.n_in_window == 1

    def test_index_page_overrides_et_labelled_api_time(self, tmp_path: Path) -> None:
        # ARNA 0001628280-16-015931: API "16:18:17Z", index page "Accepted 16:18:17" (ET)
        arna: Row = ("0001628280-16-015931", "8-K", "2016-05-09", "2016-05-09T16:18:17.000Z",
                     "2.02")  # fmt: skip
        c, seen = classifier(
            tmp_path, companies={1080709: [arna]}, pages={arna[0]: "2016-05-09 16:18:17"},
            tickers={"ARNA": 1080709},
        )  # fmt: skip
        r = c.classify("ARNA", "2016-05-09")
        assert r.zone_convention == "et_as_utc"
        assert (
            r.accepted_et is not None and r.accepted_et.isoformat() == "2016-05-09T16:18:17-04:00"
        )
        assert (r.session, r.verdict) == ("post_close", "after_entry")  # API alone: intraday
        assert any("US/Eastern labelled Z" in n for n in r.notes)
        assert "/Archives/edgar/data/1080709/000162828016015931/" in str(seen[-1].url)  # type: ignore[attr-defined]

    def test_missing_index_page_keeps_api_time_with_lower_confidence(self, tmp_path: Path) -> None:
        c, _ = classifier(tmp_path, companies={11: [RELEASE]}, tickers={"ACME": 11})
        r = c.classify("ACME", EVENT)
        assert (r.zone_convention, r.confidence) == ("unverified", "medium")
        assert (
            r.accepted_et is not None and r.accepted_et.isoformat() == "2016-02-25T16:05:00-05:00"
        )

    def test_delisted_ticker_via_full_text(self, tmp_path: Path) -> None:
        fts = fts_payload([hit(RELEASE[0], ["0001459417"], ["2U, Inc.  (TWOUQ)  (CIK 0001459417)"],
                               ["2.02", "9.01"])])  # fmt: skip
        c, seen = classifier(tmp_path, companies={1459417: [RELEASE]}, fts=fts, pages=RELEASE_PAGE)
        r = c.classify("TWOU", EVENT)
        assert (r.status, r.cik, r.cik_source, r.cik_evidence) == (
            "resolved", 1459417, "full_text", "display_name",
        )  # fmt: skip
        assert not any("ciks" in s.url.params for s in seen)  # type: ignore[attr-defined]

    def test_reused_ticker_prefers_full_text_issuer(self, tmp_path: Path) -> None:
        # Today's "OLD" belongs to CIK 22, which also filed a 2.02 that week.
        other: Row = ("0000000022-16-000001", "8-K", "2016-02-24", "2016-02-24T12:00:00.000Z",
                      "2.02")  # fmt: skip
        fts = fts_payload([hit(RELEASE[0], ["0000000033"], ["Old Co  (OLD)  (CIK 0000000033)"],
                               ["2.02"])])  # fmt: skip
        c, _ = classifier(
            tmp_path, companies={22: [other], 33: [RELEASE]}, fts=fts, tickers={"OLD": 22}
        )
        r = c.classify("OLD", EVENT)
        assert (r.cik, r.cik_source) == (33, "full_text")
        assert any(n.startswith("ticker reused") for n in r.notes)

    def test_foreign_issuer_ignores_a_strangers_8k(self, tmp_path: Path) -> None:
        # The mapped issuer files 6-Ks; another company's 2.02 merely contains "ARCO".
        own: Row = ("0000000044-16-000002", "6-K", "2016-02-25", "2016-02-25T11:30:00.000Z", "")
        fts = fts_payload([hit(RELEASE[0], ["0000000050"], ["Refiner  (TSO)  (CIK 0000000050)"],
                               ["2.02"])])  # fmt: skip
        c, seen = classifier(
            tmp_path, companies={44: [own], 50: [RELEASE]}, fts=fts,
            pages={own[0]: "2016-02-25 06:30:00"}, identity={50: 1}, tickers={"ARCO": 44},
        )  # fmt: skip
        r = c.classify("ARCO", EVENT)
        assert (r.rule, r.cik, r.form, r.confidence) == ("6k_proxy", 44, "6-K", "low")
        assert any("6-K has no item code" in n for n in r.notes)
        assert not any("CIK0000000050" in str(s.url) for s in seen)  # type: ignore[attr-defined]

    def test_unmatched_candidate_without_identity_is_unconfirmed(self, tmp_path: Path) -> None:
        # No current map entry; a stranger's 2.02 contains "ML" (mL units).
        fts = fts_payload([hit(RELEASE[0], ["0000000060"], ["Bio Co  (PASG)  (CIK 0000000060)"],
                               ["2.02"])])  # fmt: skip
        c, seen = classifier(tmp_path, companies={60: [RELEASE]}, fts=fts)
        r = c.classify("ML", EVENT)
        assert (r.status, r.reason, r.cik) == ("unresolved", "fts_unconfirmed", None)
        ident = [s for s in seen if "ciks" in s.url.params]  # type: ignore[attr-defined]
        assert len(ident) == 1
        params = dict(ident[0].url.params)  # type: ignore[attr-defined]
        assert params["ciks"] == "0000000060" and params["forms"] == "8-K"
        assert '"NASDAQ: ML"' in params["q"] and '"NYSE: ML"' in params["q"]
        assert (params["startdt"], params["enddt"]) == ("2015-02-25", "2017-02-24")
        assert not any("CIK0000000060" in str(s.url) for s in seen)  # type: ignore[attr-defined]

    def test_mapped_issuer_miss_does_not_fall_to_a_stranger(self, tmp_path: Path) -> None:
        other: Row = ("0000000070-16-000001", "8-K", "2016-02-25", "2016-02-25T12:00:00.000Z",
                      "5.02")  # fmt: skip
        fts = fts_payload([hit(RELEASE[0], ["0000000071"], ["REIT  (GMRE)  (CIK 0000000071)"],
                               ["2.02"])])  # fmt: skip
        c, _ = classifier(
            tmp_path, companies={70: [other], 71: [RELEASE]}, fts=fts, tickers={"OSS": 70},
            pages={other[0]: "2016-02-25 07:00:00"},
        )  # fmt: skip
        r = c.classify("OSS", EVENT)
        # the mapped issuer's own non-2.02 8-K is the fallback; the REIT is never used
        assert (r.cik, r.rule, r.accession, r.confidence) == (70, "other_8k", other[0], "medium")
        assert any("71:no" in n for n in r.notes)

    def test_unmatched_candidate_with_identity_is_used(self, tmp_path: Path) -> None:
        fts = fts_payload([hit(RELEASE[0], ["0000000080"], ["NewName  (NEW)  (CIK 0000000080)"],
                               ["2.02"])])  # fmt: skip
        c, _ = classifier(
            tmp_path, companies={80: [RELEASE]}, fts=fts, identity={80: 4}, pages=RELEASE_PAGE
        )
        r = c.classify("OLDT", EVENT)
        assert (r.status, r.cik, r.cik_source, r.cik_evidence) == (
            "resolved", 80, "full_text", "exchange_self_reference",
        )  # fmt: skip

    def test_too_many_unmatched_candidates_are_ambiguous(self, tmp_path: Path) -> None:
        fts = fts_payload(
            [
                hit(
                    f"9{i}-16-1",
                    [f"000000009{i}"],
                    [f"Co{i}  (C{i})  (CIK 000000009{i})"],
                    ["2.02"],
                )
                for i in range(4)
            ]
        )
        c, seen = classifier(tmp_path, companies={}, fts=fts)
        r = c.classify("WIRE", EVENT)
        assert (r.status, r.reason) == ("unresolved", "fts_ambiguous")
        assert not any("ciks" in s.url.params for s in seen)  # type: ignore[attr-defined]

    def test_unresolved_reasons(self, tmp_path: Path) -> None:
        amended: Row = ("0000000045-16-000001", "6-K/A", "2016-02-25", "2016-02-25T12:00:00.000Z",
                        "")  # fmt: skip
        c, _ = classifier(
            tmp_path,
            companies={45: [amended], 46: []},
            tickers={"FPIA": 45, "GONE": 47, "IDLE": 46},
        )
        assert (c.classify("FPIA", EVENT).reason) == "6k_only"
        assert (c.classify("GONE", EVENT).reason) == "submissions_unavailable"
        assert (c.classify("IDLE", EVENT).reason) == "no_filing_in_window"
        r = c.classify("NONE", EVENT)
        assert (r.status, r.reason, r.rule, r.confidence) == ("unresolved", "no_cik", "", "")


class TestFallback:
    def test_periodic_report_when_no_8k(self, tmp_path: Path) -> None:
        tenq: Row = ("0000000090-16-000003", "10-Q", "2016-02-26", "2016-02-25T21:40:00.000Z", "")
        c, _ = classifier(
            tmp_path, companies={90: [tenq]}, tickers={"SMAL": 90},
            pages={tenq[0]: "2016-02-25 16:40:00"},
        )  # fmt: skip
        r = c.classify("SMAL", EVENT)
        assert (r.rule, r.form, r.confidence, r.session) == (
            "periodic",
            "10-Q",
            "medium",
            "post_close",
        )
        assert any("loose upper bound" in n for n in r.notes)

    def test_amended_8k_is_never_the_fallback(self, tmp_path: Path) -> None:
        amend: Row = ("0000000091-16-000001", "8-K/A", "2016-02-25", "2016-02-25T21:00:00.000Z",
                      "2.02")  # fmt: skip
        c, _ = classifier(tmp_path, companies={91: [amend]}, tickers={"AMND": 91})
        r = c.classify("AMND", EVENT)
        assert (r.status, r.reason) == ("unresolved", "no_filing_in_window")


class TestVerdictAndLabels:
    def site(self, tmp_path: Path, row: Row, accepted: str) -> Classifier:
        c, _ = classifier(
            tmp_path, companies={11: [row]}, tickers={"ACME": 11}, pages={row[0]: accepted}
        )
        return c

    def test_amc_label_agrees_and_enters_next_day(self, tmp_path: Path) -> None:
        r = self.site(tmp_path, RELEASE, "2016-02-25 16:05:00").classify("ACME", EVENT, label="amc")
        assert (r.edgar_label, r.label_agrees) == ("amc", True)
        assert (r.entry_used, r.entry_source, r.verdict) == (
            date(2016, 2, 26),
            "label",
            "before_entry",
        )

    def test_bmo_label_on_an_evening_release_is_look_ahead(self, tmp_path: Path) -> None:
        # the OSPN-style case: an evening 8-K labelled bmo
        r = self.site(tmp_path, RELEASE, "2016-02-25 16:05:00").classify("ACME", EVENT, label="BMO")
        assert (r.edgar_label, r.label_agrees, r.verdict) == ("amc", False, "after_entry")
        assert r.to_row()["label_agrees"] == "false"

    def test_bmo_dated_on_the_reaction_day(self, tmp_path: Path) -> None:
        # accepted Wednesday evening, vendor dates the release Thursday bmo
        prev: Row = (RELEASE[0], "8-K", "2016-02-24", "2016-02-24T21:30:00.000Z", "2.02")
        r = self.site(tmp_path, prev, "2016-02-24 16:30:00").classify("ACME", EVENT, label="bmo")
        assert (r.edgar_label, r.label_agrees, r.verdict) == ("prior_day", False, "before_entry")
        assert any("reaction day" in n for n in r.notes)

    def test_explicit_entry_date_wins(self, tmp_path: Path) -> None:
        intraday: Row = (RELEASE[0], "8-K", "2016-02-25", "2016-02-25T16:00:00.000Z", "2.02")
        c = self.site(tmp_path, intraday, "2016-02-25 11:00:00")
        r = c.classify("ACME", EVENT, entry_date="2016-02-25", label="amc")
        assert (r.session, r.edgar_label, r.verdict) == ("intraday", "dmh", "entry_session")
        assert r.entry_source == "given"

    def test_weekend_acceptance_rolls_back(self, tmp_path: Path) -> None:
        sat: Row = (RELEASE[0], "8-K", "2016-02-29", "2016-02-27T15:00:00.000Z", "2.02")
        c = self.site(tmp_path, sat, "2016-02-27 10:00:00")
        r = c.classify("ACME", "2016-02-29")  # Monday
        assert (r.session, r.session_date, r.rolled) == ("post_close", date(2016, 2, 26), True)
        assert (r.verdict, r.edgar_label) == ("before_entry", "prior_day")

    def test_unknown_label_is_noted_and_ignored(self, tmp_path: Path) -> None:
        r = self.site(tmp_path, RELEASE, "2016-02-25 16:05:00").classify(
            "ACME", EVENT, label="time-not-supplied"
        )
        assert r.label_agrees is None and r.entry_source == "bmo_assumed"
        assert any("not bmo/amc/dmh" in n for n in r.notes)

    def test_row_and_dict_shapes(self, tmp_path: Path) -> None:
        r = self.site(tmp_path, RELEASE, "2016-02-25 16:05:00").classify("ACME", EVENT, label="amc")
        row = r.to_row()
        assert tuple(row) == CSV_COLUMNS
        assert row["accepted_et"] == "2016-02-25T16:05:00-05:00"
        assert row["label_agrees"] == "true" and row["cik"] == "11"
        d = r.to_dict()
        assert d["window"] == ["2016-02-24", "2016-02-26"]  # no entry date: event date
        assert d["entry_used"] == "2016-02-26"

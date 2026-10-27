# Method

How `earnings-session` times an earnings release, and why each rule is the way it is. The rules come from an audit of a post-earnings-drift backtest I ran in September and October 2026; the numbers quoted below are from that audit.

## 1. What an acceptance time can prove

A company that reports results under Item 2.02 "furnishes" the press release on Form 8-K. The 8-K goes to EDGAR together with the release or after it, never before. Its acceptance time is therefore an **upper bound** on when the news became public.

That asymmetry decides what the tool can and cannot claim:

- An 8-K accepted before the entry-day open proves the release was public when the backtest bought. This holds whatever the filing lag.
- An 8-K accepted later shows only that the release came out no later than that. It could have gone out a session earlier with a slow filing.

## 2. Sessions

`classify_acceptance` places an acceptance in an NYSE session, comparing instants in UTC against the exchange's own open and close:

| session | acceptance time (US/Eastern) |
|---|---|
| `pre_open` | before 09:30 |
| `intraday` | 09:30 up to the close |
| `post_close` | the close or later |

- The close is 16:00, or 13:00 on early-close days (the day after Thanksgiving, Christmas Eve and July 3 when those are trading days, as `exchange_calendars` lists them).
- 09:30:00 is `intraday`; 16:00:00 is `post_close`.
- An acceptance on a weekend or exchange holiday is `post_close` of the previous trading day (`rolled = true`). A Saturday filing is news for Monday's open, the same as a Friday-evening one.
- The calendar is `exchange_calendars` XNYS. It treats Columbus Day and Veterans Day as trading days, which they are on NYSE (some bank-holiday calendars close them), and includes the two unscheduled closures of recent years, 2018-12-05 and 2025-01-09.
- Time zones are handled with `zoneinfo`, so a 16:30 ET filing is 20:30Z in summer and 21:30Z in winter, and a 21:00 ET filing whose UTC date is already the next day stays on its ET day.

## 3. Verdict against an entry

`entry_verdict` compares the session with a position opened at the open of `entry_date`:

| verdict | rule | reading |
|---|---|---|
| `before_entry` | accepted on an earlier trading day, or `pre_open` on the entry day | the release was public at entry; conclusive |
| `entry_session` | `intraday` on the entry day | ambiguous: an intraday release, or a pre-open release whose 8-K came late |
| `after_entry` | `post_close` on the entry day, or any later day | look-ahead, unless the release came a whole session before its 8-K |

The entry used is, in order: the `entry_date` given; else the one the vendor label implies (the event day's open for `bmo` and `dmh`, the next trading day's open for `amc`); else the event day's open. The last case is the common backtest shortcut of treating an unlabelled event as BMO, and is what the tool tests by default.

`edgar_label` restates the session relative to the event date, so it can be put next to a vendor label: `bmo`, `dmh` or `amc` when the acceptance falls in that session of the event's trading day, `prior_day` or `later_day` when it falls on another trading day. A BMO label on a filing from the previous evening reads `prior_day`: the vendor dated the release on the day the market reacted. The label disagrees, but a BMO entry on that date is still after the release, which the verdict shows.

## 4. Finding the issuer

1. **Current ticker list** (`company_tickers.json`). Dots and dashes are tried both ways (`BRK.B`, `BRK-B`).
2. **Full-text search.** EDGAR full-text search for the quoted ticker in the window's 8-Ks covers renamed, delisted and acquired issuers. Only Item 2.02 hits count. A hit is evidence of identity only when one of these holds:
   - the issuer's display name lists the ticker, or the ticker plus "Q" (the OTC symbol of a bankrupt issuer);
   - the candidate's own 8-Ks within a year of the event cite the ticker after an exchange name, such as "(NASDAQ: X)" or "(Nasdaq/NMS: X)". The search is restricted to that CIK.

   A plain match of the string is not enough. Short tickers are ordinary words or units ("ML" matches millilitres), and an earlier version of these rules without the identity check matched 6 events to the wrong company. With more than 3 candidates and no display-name match the event is `fts_ambiguous`; with candidates that fail the check it is `fts_unconfirmed`. Neither is guessed.
3. **Reused tickers.** When a display-name match names a different CIK from the one in today's ticker list, the ticker has changed hands and the full-text issuer is tried first. Example: VNOM belonged to CIK 1602065 in 2020; today's list gives the 2025 successor, CIK 2074176.
4. **Foreign issuers.** If the first issuer identified files only 6-Ks in the window, the search stops there instead of moving on to a stranger's 8-K that happens to contain the ticker.

## 5. Choosing the filing

- **Window**, by acceptance date in US/Eastern: from the trading day before the event date to the trading day after the later of the event date and the entry date (the event date when no entry date is given). Vendors date an after-close release on either side of midnight; the window catches both.
- **Not `filingDate`.** EDGAR gives an acceptance after 17:30 ET the next business day's filing date, so matching on it loses evening releases. The ET acceptance date is right whichever zone the API value is in, because EDGAR accepts filings only between 06:00 and 22:00 ET.
- **Item 2.02 8-K first.** The original 8-K (not 8-K/A) with Item 2.02 nearest the event date; on a tie, the earliest acceptance. `rule = item_2.02`. In the audit no window held two Item 2.02 filings; `n_in_window` and a note flag it when one does.
- **Fallback.** If the ticker-list issuer has no Item 2.02 8-K in the window: its nearest other original 8-K (often Item 7.01 or 8.01), else its nearest 10-Q, 10-K or 10-KT. `rule = other_8k` or `periodic`. In the audit this timed 220 of 451 otherwise untimed events that had a CIK. Not applied when the ticker has been reused.
- **6-K proxy.** An issuer with only 6-Ks gets the nearest original 6-K. `rule = 6k_proxy`, always `low` confidence. In the audit 27 of 53 such windows held more than one 6-K.

## 6. The acceptance time

The submissions API gives `acceptanceDateTime` with a "Z" suffix. It is not always UTC. In September 2026, comparing 228 values against the filing index pages:

- 185 were real UTC. Apple's earnings 8-Ks, filed around 16:30 ET, read 20:30Z in summer and 21:30Z in winter.
- 43 were the US/Eastern wall clock labelled Z. ARNA 2016-05-09 read "16:18:17Z" while its index page said "Accepted 2016-05-09 16:18:17", and the stock opened 5.1% higher the next day. All 43 came from issuers no longer in the current ticker list.

Read as UTC, those 43 move 4 to 5 hours earlier: in that audit 26 after-entry filings would have read as intraday. On 2026-10-27 seven of the 43 were checked again and all seven now read as real UTC, so the data has been changed at least in part since September.

The tool therefore takes the time from the index page's "Accepted" line, which is US/Eastern, and keeps the API value beside it with `zone_convention`:

| `zone_convention` | meaning |
|---|---|
| `utc` | the API value is the same instant in UTC |
| `et_as_utc` | the API value is the ET wall clock labelled Z |
| `mismatch` | neither; the index page is used |
| `unverified` | no index page; the API value was read as UTC |

## 7. Confidence

| rule | index page read | no index page |
|---|---|---|
| `item_2.02` | high | medium |
| `other_8k`, `periodic` | medium | low |
| `6k_proxy` | low | low |

## 8. Audit statistics

`audit` cross-tabulates vendor label against `edgar_label`, and reports each verdict's share with a 95% Wilson score interval. Item 2.02 rows, fallback rows and 6-K proxies are reported separately. Foreign issuers differ from 8-K filers in timing, so mixing them would change the answer for both groups. In the backtest audit, 95 Item 2.02 events from the unlabelled group gave `after_entry` 56/95 = 58.9% (48.9% to 68.3%), against 5/39 in the BMO-labelled control group.

## 9. Known gaps

- The 8-K is an upper bound (section 1), and in the BMO control group at most 3 of 39 8-Ks came after the open for reasons EDGAR can't separate from a wrong date.
- About a quarter of the sampled events could not be timed, mostly because the window held no filing at all. A wider window was not tested.
- A ticker printed only inside a list of tickers ("Nasdaq: A, B, X") is not matched by the identity check, so a few issuers that share filings with affiliates stay unresolved.
- Events EDGAR can't time could be placed from the price and volume reaction. That is planned, not built: see the README roadmap.

## 10. Price-bar alignment (`bars`)

The session rules above fix the entry day. A backtest can still price that day wrong. The usual lookup is "the first bar on or after the entry date" and "the last bar on or before the exit date". With no limit on the distance, a ticker that has no bars near the target gets whatever bar is next, weeks or years away, and the return lands in the wrong period without an error.

I found this in October 2026 in a different backtest of mine, a monthly-rebalanced 10-K text-change signal. Its monthly return function searched for the entry and exit bars with no distance limit. 719 of 3,282 name-months (21.9%, 56 tickers) were priced from bars outside their month. For 34 of those tickers the price table only started in December 2025, so a run in 2026 booked 2026 prices into months from 2019 to 2025. Every result from that backtest had to be thrown out. The fix required both bars within 7 calendar days of their targets and dropped the name-month otherwise.

`bars` measures that distance for every trade.

- **Targets.** The entry target is `entry_date` and the exit target `exit_date`. A target that isn't a trading day moves to the trading day the rule would reach (the next one for an entry, the previous one for an exit). The move goes into `notes` and is not a gap.
- **Bars.** With `--prices`, each ticker's dates are searched in the same direction as the rule, with no distance limit, because the point is to see how far the bar is. Without `--prices`, the bar dates the backtest recorded (`entry_bar_date`, `exit_bar_date`) are used.
- **Distance.** Signed, in NYSE trading days (the XNYS calendar of section 2) and in calendar days, measured from the trading day the rule should have reached; positive means the bar is later. A trading day with no bar counts as skipped: a Friday entry priced from a Monday bar is 1 day late.
- **Flags.** One per row, the most serious that applies, in this order: `early_bar` (a recorded entry bar before `entry_date`), `history_break` (a bar more than `--break-days` trading days away, default 250), `no_bar`, `entry_gap` and `exit_gap` (more than `--max-gap` trading days away, default 0), `ok`. The others that also apply are listed in `notes`.
- **Defaults.** `--max-gap 0` because an event entry is one session's open, and a bar one session late is a different trade. Weekly or monthly rebalancing can tolerate a few days; the fix above allowed 7 calendar days, about 5 trading days. 250 trading days is about a year, far beyond any lookup a backtest means to make.
- **Returns.** With a `return` column, the summary gives the mean and sum for flagged and clean rows, and the share of the summed return the flagged rows carry. A flagged minority that carries most of the result is the fingerprint of this bug.

Limits:

- It checks dates, not prices. A bar on the right day with a wrong price passes.
- A gap can be real. Halts, suspensions and thinly traded names have missing days; the flag says to look, not that the trade is wrong.
- The price table has to be the one the backtest used. A cleaner table than the backtest saw passes trades the backtest mispriced.
- Bars dated outside the calendar range (1995 to about a year ahead) are dropped and counted.

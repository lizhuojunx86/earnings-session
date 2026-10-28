# earnings-session

Earnings calendars tag each release BMO (before the market opens) or AMC (after it closes). When the tag is wrong, or missing and treated as BMO, a backtest that buys at the open buys before the news is out, and the return it books is look-ahead. `earnings-session` checks the tag against SEC EDGAR. It finds the company's earnings 8-K, reads the time EDGAR accepted it, and tells you which trading session that time falls in and whether your backtest entered before it.

I wrote it after running this check by hand on my own post-earnings-drift backtest. About half of the backtest's trades came from events with no timing label, and the backtest entered them at the open as if they were BMO. In a sample of 95 of those events with an earnings 8-K, 59% of the 8-Ks were accepted at or after the close of the entry day. Once every event was moved to the session its 8-K supports, net alpha per trade fell from 1.21% to about 0.25%. The full write-up is on [dev.to](https://dev.to/lizhuojunx86/my-pead-backtests-edge-fell-from-121-to-025-per-trade-once-i-checked-announcement-times-3k31), with a Chinese version on [Zhihu](https://zhuanlan.zhihu.com/p/2098525849448346965).

The second check, `bars`, is about prices. A backtest that takes "the first bar on or after the entry date" with no limit on the distance prices a trade from whatever bar comes next, months or years later when the ticker has no history around that date, and nothing raises an error. I found this in a monthly 10-K text-signal backtest of mine: 719 of 3,282 name-months (21.9%) were priced from bars outside their month, and for 34 tickers the price table only started in December 2025, so 2026 prices had been booked into 2019-2025. `bars` reads your trade list and price table, measures how far each bar sits from its target in NYSE trading days, and flags the rows to look at. It runs offline.

## Install

```bash
pip install earnings-session
```

Python 3.10 or later. SEC asks every automated client to say who it is, so set a User-Agent with your name and a contact address before the first call:

```bash
export EARNINGS_SESSION_USER_AGENT="Your Name you@your-domain.com"
```

or pass `--user-agent` on each command. Without one the tool exits and prints the format SEC expects. `bars` never goes online and doesn't need one.

## Use

### One event

```bash
earnings-session classify --ticker OSPN --date 2019-10-29 --label bmo
```

```json
{
  "ticker": "OSPN",
  "event_date": "2019-10-29",
  "vendor_label": "bmo",
  "entry_used": "2019-10-29",
  "rule": "item_2.02",
  "confidence": "high",
  "cik": 1044777,
  "company": "OneSpan Inc.",
  "form": "8-K",
  "accession": "0001558370-19-009352",
  "accepted_et": "2019-10-29T20:27:07-04:00",
  "api_value": "2019-10-30T00:27:07.000Z",
  "zone_convention": "utc",
  "session": "post_close",
  "verdict": "after_entry",
  "edgar_label": "amc",
  "label_agrees": false,
  "notes": ["..."]
}
```

(Output shortened.) The release was labelled BMO, but its 8-K was accepted at 20:27 ET. A backtest that bought at the 2019-10-29 open bought eleven hours before the filing; the stock opened 21% higher the next morning.

`--entry-date` sets the backtest entry (the position is opened at that day's open). Without it, the entry follows the label: the event day's open for `bmo` and `dmh`, the next trading day's open for `amc`, and the event day's open when there is no label.

### A file of events

```bash
earnings-session batch events.csv timed.csv
```

Input columns: `ticker`, `event_date` (YYYY-MM-DD), and optionally `entry_date` and `vendor_label` (`bmo`, `amc`, `dmh`; anything else counts as no label). The output keeps the input columns and adds:

| column | meaning |
|---|---|
| `cik`, `company`, `form`, `accession` | the filing used |
| `accepted_et` | EDGAR acceptance time in US/Eastern, from the filing's index page |
| `api_value` | `acceptanceDateTime` as the submissions API gave it |
| `zone_convention` | `utc`, `et_as_utc` (Eastern time labelled Z), `mismatch`, or `unverified` (no index page) |
| `session`, `session_date` | `pre_open`, `intraday` or `post_close`, and the trading day it refers to |
| `edgar_label` | what the filing supports relative to the event date: `bmo`, `dmh`, `amc`, `prior_day`, `later_day` |
| `label_agrees` | `edgar_label` equals the vendor label |
| `entry_used`, `verdict` | the entry open compared against, and `before_entry`, `entry_session` or `after_entry` |
| `rule`, `confidence` | how the filing was found (`item_2.02`, `other_8k`, `periodic`, `6k_proxy`) and `high` / `medium` / `low` |
| `reason` | why an event stayed unresolved (`no_cik`, `fts_ambiguous`, `fts_unconfirmed`, `no_filing_in_window`, ...) |
| `notes` | anything a reviewer should look at |

### Audit a vendor's labels

```bash
earnings-session audit timed.csv          # text
earnings-session audit timed.csv --json   # the same report as JSON
```

```text
== 8-K Item 2.02: n = 5 ==
vendor label x EDGAR acceptance session (relative to the event date)
                bmo        dmh        amc  prior_day  later_day
bmo               0          0          2          0          0
amc               0          0          2          0          0
(none)            0          0          1          0          0
verdict vs entry open (Wilson 95%)
  before_entry  2/5 = 40.0% (11.8%-76.9%)
  entry_session 0/5 = 0.0% (0.0%-43.4%)
  after_entry   3/5 = 60.0% (23.1%-88.2%)
by vendor label
  bmo     agrees 0/2 = 0.0% (0.0%-65.8%); after_entry 2/2 = 100.0% (34.2%-100.0%)
  amc     agrees 2/2 = 100.0% (34.2%-100.0%); after_entry 0/2 = 0.0% (0.0%-65.8%)
```

Rows found through other 8-Ks, 10-Qs or 10-Ks, and 6-K proxies, are reported in their own blocks and never mixed into the Item 2.02 numbers. If the input has no `verdict` column, `audit` classifies it first.

### Check the price bars

```bash
earnings-session bars trades.csv --prices prices.csv -o checked.csv
```

`trades.csv` needs `ticker` and `entry_date`; `exit_date` and `return` are optional, and other columns are carried through. `prices.csv` is the price table your backtest read, with `ticker` and `date` columns (the rest is ignored). The entry bar is the first one on or after `entry_date` and the exit bar the last one on or before `exit_date`, which is how most backtests look them up; `--entry-side` and `--exit-side` turn either one around.

```text
trades: 5
bars from prices.csv: 3532 rows, 3 tickers
entry = first bar on or after entry_date; exit = last bar on or before exit_date; max gap 0 trading days; history break > 250

flags (Wilson 95%)
  history_break 1/5 = 20.0% (3.6%-62.4%)
  no_bar        1/5 = 20.0% (3.6%-62.4%)
  entry_gap     1/5 = 20.0% (3.6%-62.4%)
  ok            2/5 = 40.0% (11.8%-76.9%)

distance from target, trading days
  entry 0: 2, 1: 0, 2-5: 1, 6-20: 0, 21-250: 0, >250: 1, no bar: 1
  exit  0: 3, 1: 0, 2-5: 0, 6-20: 0, 21-250: 0, >250: 0, no bar: 2

returns (the 'return' column)
  flagged  n 3, mean 0.038, sum 0.114
  ok       n 2, mean 0.008, sum 0.016
  flagged rows carry 87.7% of the total
```

Each row gets one flag, the most serious that applies:

| flag | meaning |
|---|---|
| `early_bar` | the entry bar the backtest recorded is before `entry_date`: the trade was priced before the day it claims |
| `history_break` | a bar more than 250 trading days (`--break-days`) from its target: another stretch of history, maybe another company on a reused ticker |
| `no_bar` | the ticker isn't in the price table, or there is no bar in that direction |
| `entry_gap`, `exit_gap` | a bar more than `--max-gap` trading days (default 0) from its target |
| `ok` | none of the above |

`checked.csv` keeps your columns and adds, for each leg, the target day, the bar found, and the distance in trading days and calendar days, then the flag and notes. If your trade file already records the bars the backtest used (`entry_bar_date`, `exit_bar_date`), leave out `--prices` and those are checked instead; that is the only way to catch `early_bar`. `--fail-on-flag` exits 1 when any row is flagged, so the check can run in CI.

In the example, three flagged trades out of five carry 88% of the summed return. When the flagged rows carry the result, the backtest is measuring its own lookup.

If you build returns with `pandas.merge_asof`, pass `tolerance`. Without it the join takes the nearest bar at any distance.

### From Python

```python
from earnings_session import Classifier

with Classifier() as c:  # reads EARNINGS_SESSION_USER_AGENT
    r = c.classify("OSPN", "2019-10-29", label="bmo")

print(r.session, r.verdict, r.accepted_et)  # post_close after_entry 2019-10-29 20:27:07-04:00
r.to_dict()  # the JSON above
```

The CLI calls the same `Classifier.classify`. For a single call there is also `earnings_session.classify(...)`.

```python
from datetime import date
from earnings_session import find_bar

bars = [date(2025, 12, 8), date(2025, 12, 9)]        # a ticker whose history starts late
find_bar(bars, date(2019, 6, 28))                  # date(2025, 12, 8): six years late
find_bar(bars, date(2019, 6, 28), max_gap_td=5)    # None: no bar within 5 trading days
```

`earnings_session.check_trades` runs the full `bars` check on a list of trades.

## How it decides

1. **Issuer.** The ticker is looked up in SEC's current ticker list. Tickers that are no longer listed (delisted, renamed, acquired) go through EDGAR full-text search, and a candidate is accepted only with evidence that it really is that ticker's issuer. Otherwise the event stays unresolved, with the reason.
2. **Window.** Filings accepted (by US/Eastern date) from the trading day before the event date to the trading day after the entry date, or after the event date when no entry date is given.
3. **Filing.** The original 8-K with Item 2.02 nearest the event date; 8-K/A amendments are skipped. Without one: the nearest other 8-K, then 10-Q or 10-K. A foreign issuer that files only 6-Ks gets its nearest 6-K, marked low confidence.
4. **Time.** The "Accepted" time on the filing's index page, which is US/Eastern. The API's `acceptanceDateTime` is kept next to it.
5. **Session.** Before 09:30 ET is `pre_open`, 09:30 to the close is `intraday`, the close and later is `post_close`. The close is 16:00, or 13:00 on early-close days. Weekend and holiday acceptances count as `post_close` of the previous trading day. The calendar is NYSE's (`exchange_calendars` XNYS), so Columbus Day and Veterans Day are trading days.
6. **Verdict.** `before_entry` if accepted before the entry-day open, `entry_session` if during the entry-day session, `after_entry` if at the close or later.

[METHOD.md](METHOD.md) has the full rules and the reasons behind them.

## Limits

- **An 8-K acceptance is an upper bound on the release time.** A company can put out its press release first and file the 8-K later, never the other way round. So `before_entry` is conclusive: the news was public when you bought. `after_entry` means look-ahead unless the release came a full session before its 8-K.
- **A late 8-K makes a real pre-open release look like intraday or post-close.** In my check of 39 events labelled BMO, 34 had their 8-K before the entry open, 2 were evening releases the label got wrong, and 3 had an 8-K the next morning (a late filing or a wrong date; EDGAR alone can't tell which).
- **A 6-K has no item code.** For foreign issuers the tool takes the 6-K nearest the event date, which may be a different announcement. These rows are `low` confidence and audited separately.
- **Tickers get reused.** SEC's ticker list gives today's owner of a ticker. When the window's filings name a different company under that ticker, that company is tried first and the row says so in `notes`. A ticker the issuer used only in the past can be missed if its press releases never print it next to an exchange name. Check `company` when it matters.
- **Other 8-Ks, 10-Qs and 10-Ks are looser bounds.** Small issuers sometimes announce results without an Item 2.02. The fallback filing may come days after the release, so these rows are `medium` or `low` confidence.
- **The API's time zone is not stable.** In September 2026, 43 of 228 `acceptanceDateTime` values I checked were US/Eastern wall-clock times labelled "Z", all from issuers no longer in SEC's ticker list. Read as UTC, they put a 16:18 filing at 12:18. On 2026-10-28 I rechecked all 43: every one now reads as real UTC, and none of the 43 filers is back in the ticker list. The tool reads the index page either way and records which convention the API used.
- **The window is narrow.** A vendor date off by more than one trading day finds no filing, and the event comes back `no_filing_in_window`.
- **`bars` checks dates, not prices.** A bar on the right day with a wrong price passes, and a real trading halt shows up as a gap. [METHOD.md](METHOD.md) section 10 has the rules.
- **Calendar.** Early closes are the ones `exchange_calendars` lists. It has no early close on 2026-07-02, and published holiday tables disagree on that day. Dates must fall between 1995 and about a year from today.

## Roadmap

- Price-reaction timing for events EDGAR can't place: compare the event-day open gap and volume with the next day's, calibrated on EDGAR-timed events. Calling a release BMO when it was AMC brings back look-ahead, so the rule only says BMO when out-of-sample precision clears a Wilson lower bound of 0.95. On my data no threshold cleared it, which is why it isn't in v0.1.

## SEC access

Default 5 requests per second across www.sec.gov, data.sec.gov and efts.sec.gov (SEC's limit is 10; `--max-rps` changes it). Rate-limit and server errors are retried with back-off; a 403 stops the run. Responses are cached in `~/.cache/earnings-session` (or `$XDG_CACHE_HOME/earnings-session`, or `--cache-dir`). The ticker list, submissions and search results are refetched after 24 hours; filing index pages are kept. `--no-cache` turns the cache off. Expect about 3 requests per event; the ticker list is fetched once per run.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests scripts && ruff format --check src tests scripts
mypy --strict
pytest                 # offline, uses recorded SEC responses in tests/fixtures
pytest -m network      # live checks against sec.gov; needs EARNINGS_SESSION_USER_AGENT
```

`scripts/record_fixtures.py` re-records the fixtures. SEC EDGAR data is in the public domain.

## License

MIT

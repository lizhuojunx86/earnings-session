# Changelog

## 0.1.0 (2026-10-27)

First release.

- `classify`: one event to JSON. Session (`pre_open` / `intraday` / `post_close`) of the earnings filing's EDGAR acceptance time, verdict against a backtest entry (`before_entry` / `entry_session` / `after_entry`), and agreement with a vendor's BMO / AMC / DMH label.
- `batch`: CSV in, CSV out, input columns kept.
- `audit`: vendor label against EDGAR, with Wilson 95% intervals; Item 2.02, fallback filings and 6-K proxies reported separately.
- Issuer lookup through SEC's ticker list, then EDGAR full-text search with an identity check; unconfirmed matches stay unresolved.
- Filing choice: original Item 2.02 8-K, then other 8-K, then 10-Q / 10-K; 6-K proxy for foreign issuers.
- Acceptance time from the filing index page (US/Eastern), with the API value and its zone convention alongside.
- NYSE calendar from `exchange_calendars` (XNYS), including early closes and the 2018-12-05 and 2025-01-09 closures.
- SEC access: required User-Agent, 5 requests per second by default, retries, on-disk cache.

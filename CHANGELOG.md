# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

- Add `QuotaManager.reserve`, `commit` and `release` (and the `Reservation` and
  `UnknownReservationError` types): an atomic check-and-hold that closes the
  gap between `check_quota` and `track_operation`, where two callers could both
  pass the check for the same units. Holds count against the limit, are never
  expired automatically, and `get_usage()` gains a `reserved` key. `check_quota`
  and `can_safely_perform` remain informational.

## 0.1.0

First release.

- `QuotaManager` — a per-project ledger of YouTube Data API unit spend, shared
  between processes on one host via a file lock. Read-modify-write happens
  inside a single lock hold, so concurrent callers cannot overwrite each
  other's totals.
- `DEFAULT_COST_TABLE` — Google's published per-operation unit costs, keyed by
  API method name, overridable per manager via `cost_table=`.
- Unknown operation names raise `UnknownOperationError` instead of costing
  zero.
- Optional enforcement (`enforce=True`) raising `QuotaExceededError`.
- Usage reports and history, with breakdowns by operation and by brand label.
- Configurable daily rollover boundary via `reset_tz`; defaults to UTC.
- Ledger location resolves from `$YT_QUOTA_GUARD_HOME`, then
  `$XDG_STATE_HOME`, then `~/.local/state` — never relative to the current
  working directory.
- Atomic ledger writes, and recovery from a truncated ledger rather than a
  permanent crash.

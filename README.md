# yt-quota-guard

A shared ledger for YouTube Data API quota, so a fleet of scripts cannot burn
through the daily allowance without noticing.

## The problem

The YouTube Data API meters your **Google Cloud project**, not your credential
and not your channel. Every script, cron job, channel and brand that
authenticates against the same project draws from one daily allowance. Costs
are lopsided — a video upload is worth 1600 list calls — so one unattended
backfill can spend the whole day's quota before lunch, and the first symptom
everything else sees is `quotaExceeded` on an unrelated call.

Nothing in the client libraries keeps that running total for you. This library
does: one JSON ledger per project, guarded by a file lock, that every caller
reads and writes.

## Install

```bash
pip install yt-quota-guard
```

Requires Python 3.11+. One runtime dependency: [`filelock`](https://pypi.org/project/filelock/).

## Usage

```python
from yt_quota_guard import QuotaManager

quota = QuotaManager(
    project_id="my-gcp-project",  # the unit Google meters
    brand_id="cooking-channel",  # a label for reporting, not a separate budget
)

# Claim the units first. This checks the limit and holds the units in one
# step, so two scripts contending for the last of the day's quota cannot both
# be admitted. Raises QuotaExceededError if the units are not there.
hold = quota.reserve("videos.insert")  # priced from the cost table: 1600

# ... perform the upload against the YouTube API ...

quota.commit(hold, metadata={"video_id": "abc123"})  # turn the hold into spend
# quota.release(hold)  # instead, only if the request certainly never went out

for line in quota.get_warnings():
    print(line)  # WARNING at 80%, CRITICAL at 90%

print(quota.generate_report())
```

Any other process pointed at the same `project_id` and storage directory sees
that 1600 units immediately — that is the entire point of the file lock.

### Reserve, then commit

`check_quota` followed by `track_operation` is two separate steps, so two
callers can both pass the check for the same remaining units and together
overspend. Treat `check_quota` and `can_safely_perform` as informational, and
use `reserve` when the answer must hold:

- `reserve(operation, cost=None, metadata=None)` checks the limit and holds the
  units in one lock hold. Units held by other callers count, so the second of
  two 80-unit contenders for a 100-unit budget gets `QuotaExceededError`.
- `commit(reservation, cost=None, metadata=None)` converts the hold into recorded spend
  (pass `cost=` if the actual charge differed). Committing twice charges once.
  Commit even if the request then failed: Google may still have charged it.
- `release(reservation)` drops a hold for a request that certainly was not
  sent. Releasing twice, or after a commit, does nothing and never refunds.

A hold is never expired automatically. If a process dies between `reserve`
and `commit` it may or may not have sent its request, so the units stay held
(counted in `get_usage()["reserved"]` and subtracted from `remaining`) until
something commits or releases them, or the day rolls over. Holds live in the
ledger file under a `reservations` key; version 0.1.0 ignores that key and
drops it on its next write, so let outstanding holds settle before downgrading.

Set `enforce=True` on `track_operation` to make it raise `QuotaExceededError`
rather than record an overspend. The default is to account, not to police:
the call already happened, so refusing to write it down would only make the
ledger wrong.

## Unit costs

Costs charged against the shared daily pool, per API method:

| Method | Units |
| --- | --- |
| `videos.insert` (upload) | 1600 [^1] |
| `captions.update` | 450 |
| `captions.insert` | 400 |
| `search.list` | 100 [^1] |
| `captions.list`, `captions.delete` | 50 |
| `channels.update` | 50 |
| `playlists.insert`, `playlists.update`, `playlists.delete` | 50 |
| `playlistItems.insert`, `playlistItems.update`, `playlistItems.delete` | 50 |
| `thumbnails.set` | 50 |
| `videos.update`, `videos.delete`, `videos.rate`, `videos.reportAbuse` | 50 |
| `channels.list` | 1 |
| `playlists.list` | 1 |
| `playlistItems.list` | 1 |
| `videos.list` | 1 |
| `videos.getRating` | 1 |

Default pool: **10,000 units per day per project**, reset at **midnight Pacific
Time**.

### These are Google's numbers, and Google changes them

This table is a convenience default, not a contract. The authority is
[Google's published quota cost table][costs]; check it before you rely on any
number here.

[^1]: Two rows in particular have moved. Google's current published table
meters `videos.insert` and `search.list` as **separate per-day call
allowances** — 100 calls each per day — for projects on the default
allocation, rather than as 1600 and 100 units against the shared 10,000-unit
pool. This library keeps the 1600/100 unit costs as its defaults because they
still describe projects on an extended quota, and because over-charging fails
safe: you stop early rather than late. If your project is on the default
allocation, those two endpoints need their own call counters, which this
library does not model — track them with a second `QuotaManager` whose
`daily_limit=100` and whose costs are 1 each.

Override the whole table when Google moves, without waiting for a release:

```python
QuotaManager(project_id="p", cost_table={"videos.insert": 1, "videos.list": 1})
```

An operation name that is not in the table raises `UnknownOperationError`
rather than costing zero — a typo that silently costs nothing is how a quota
guard stops guarding. Pass `estimate_cost(name, default=...)` if you want a
fallback.

[costs]: https://developers.google.com/youtube/v3/determine_quota_cost

## Where the ledger lives

One directory per project, under a state directory chosen in this order:

1. `$YT_QUOTA_GUARD_HOME`
2. `$XDG_STATE_HOME/yt-quota-guard`
3. `~/.local/state/yt-quota-guard`

Nothing is written relative to the current working directory, so the ledger
does not depend on where a script happened to be launched from. Pass
`storage_path=` to put it somewhere specific — a path all your jobs can reach
is what makes the total shared:

```python
QuotaManager(project_id="p", storage_path="/var/lib/yt-quota")
```

The lock file is created group-writable (0o664) so two UNIX accounts in the
same group can share one ledger on a build host; otherwise whichever ran first
leaves a lock the other cannot acquire.

## Daily rollover

The ledger resets the first time it is read or written after the day changes —
lazily, on access, not on a timer. Google's quota day ends at midnight Pacific
Time, so that is the default boundary (`reset_tz="America/Los_Angeles"`), and
it follows daylight saving: the boundary is 07:00 UTC in summer and 08:00 UTC
in winter. `reset_tz` takes a `tzinfo` or an IANA name:

```python
QuotaManager(project_id="p")  # midnight Pacific
QuotaManager(project_id="p", reset_tz="UTC")  # midnight UTC
QuotaManager(project_id="p", reset_tz="Europe/Paris")
```

The zone is stored in the ledger (`reset_tz` key). A ledger has one boundary:
a caller that passes a different zone than the ledger's gets
`ResetZoneMismatchError` instead of shifting the boundary for everyone else.
Pacific time needs a timezone database; on a system without one (typically
Windows) install `tzdata`, or pass `reset_tz="UTC"`, otherwise constructing a
manager raises a `RuntimeError` saying so.

### Upgrading from 0.1.0 (default was UTC)

The default changed from UTC to Pacific. Existing ledgers keep their spent
units and operations: a ledger with no stored zone adopts the zone of the first
caller that opens it, and nothing is reset at upgrade time. Because UTC
midnight falls in the late afternoon Pacific, a ledger last rolled at UTC
midnight simply carries its units until the next Pacific midnight, so the
switch never grants a second allowance. To keep the old boundary, pass
`reset_tz="UTC"` everywhere.

## API

| | |
| --- | --- |
| `QuotaManager(project_id, brand_id=None, storage_path=None, daily_limit=10000, cost_table=None, reset_tz="America/Los_Angeles")` | Open or create a project ledger. |
| `.track_operation(operation, cost, metadata=None, enforce=False)` | Record a call and charge it. Returns the `QuotaOperation`. |
| `.estimate_cost(operation_type, default=None)` | Table lookup. Raises `UnknownOperationError` if unknown. |
| `.reserve(operation, cost=None, metadata=None)` | Atomically check the limit and hold units. Returns a `Reservation`; raises `QuotaExceededError`. |
| `.commit(reservation, cost=None, metadata=None)` | Turn a hold into recorded spend. Idempotent. Returns the `QuotaOperation`. |
| `.release(reservation)` | Drop a hold for a request that was never sent. |
| `.check_quota(cost)` | Informational: would `cost` more fit, counting held units? Not a claim. |
| `.can_safely_perform(operation_type)` | `check_quota(estimate_cost(...))`. Informational. |
| `.get_usage()` | Counters: used, reserved, remaining, percentage, operation count. |
| `.get_warnings()` | `[]`, one `WARNING` line at 80%, or one `CRITICAL` line at 90%. |
| `.get_history(limit=100)` | The most recent operations, oldest first. |
| `.generate_report()` | Usage plus breakdowns by operation and by brand. |
| `.refresh()` | Re-read the ledger from disk. |
| `.reset_quota()` | Zero it now, without waiting for the rollover. |
| `QuotaManager.get_project_usage(project_id, storage_path=None)` | Units used today, without constructing a manager. |

Every read re-reads the ledger under the lock, so two callers never disagree.
That means each accessor touches the disk — fine at API call rates, wrong for
a hot loop.

## Limits

Worth knowing before you trust it:

- **One host, one filesystem.** The lock is an OS file lock. It coordinates
  processes that can see the same directory. It is not a distributed counter:
  `flock` over NFS is unreliable, and two machines with separate disks keep
  two separate ledgers that each think they have the full 10,000.
- **It records what you tell it.** Nothing intercepts HTTP. A call that skips
  `track_operation` is invisible, and a retry you forget to record is free.
  Google's own accounting is the authority; this is a mirror, and mirrors
  drift.
- **Costs are a table, not a measurement.** The API does not report what a
  call actually cost. See the caveats above.
- **The ledger is rewritten in full on every operation.** A day of ten
  thousand one-unit calls means ten thousand growing rewrites. That is fine
  for the upload-and-playlist workloads this was built for; it is not a
  metrics backend.
- **Enforcement is opt-in and advisory.** `enforce=True` and `reserve` stop
  *this* library from admitting or recording the spend. Only Google can stop
  the API call.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and unit-cost corrections
are both welcome — if Google moves a number, a PR against `DEFAULT_COST_TABLE`
with a link to the published table is the fastest fix.

## License

MIT — see [LICENSE](LICENSE).

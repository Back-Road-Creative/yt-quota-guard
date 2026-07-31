# Contributing

Thanks for taking the time. This is a small library; the bar is a green suite
and a clear reason for the change.

## Getting set up

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Before you open a pull request

- `ruff check .` and `ruff format --check .` are clean.
- `pytest` is green. CI runs it on Python 3.11, 3.12 and 3.13.
- Behaviour changes come with a test. Bug fixes come with a test that fails
  before the fix.
- Update `README.md` and `CHANGELOG.md` in the same change, not afterwards.

## Corrections to the cost table

Google changes published quota costs, and this library's defaults will go
stale. Corrections are welcome and are the easiest contribution to review:
change the entry in `DEFAULT_COST_TABLE`, update the README table, and link
the [published cost table](https://developers.google.com/youtube/v3/determine_quota_cost)
in the pull request so a reviewer can check it in one click.

## Scope

In scope: accounting for API unit spend, the cost table, and making the shared
ledger harder to get wrong.

Out of scope: wrapping the YouTube API itself, credential handling, retry
logic, and turning this into a distributed rate limiter. If you need a counter
shared across machines, this library is the wrong shape — it is a file lock on
one host, by design.

## Reporting a bug

Include the Python version, the `filelock` version, the operating system, and
the smallest snippet that reproduces it. If the ledger ended up in a state you
did not expect, the contents of `quota_usage.json` (it holds no credentials)
are usually the whole story.

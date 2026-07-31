# Security Policy

## Supported versions

Only the latest released tag receives fixes. Pin a released `v*` tag; `main`
is unstable.

## Reporting a vulnerability

Please report suspected vulnerabilities privately. Do **not** open a public
issue for a security report.

Open a private advisory via GitHub's **Security → Report a vulnerability** tab
on this repository. If that tab is unavailable to you, the maintainer's contact
address is in this project's package metadata (`pyproject.toml`); use
`yt-quota-guard security` as the subject and send a first, contentless message
if you would rather exchange a key before sending details.

Please include the affected version, a description of the issue and its
impact, reproduction steps, and any suggested remediation.

## What to expect

- Acknowledgement within 5 business days.
- Initial assessment and severity triage within 10 business days.
- Coordinated disclosure: we agree a timeline with you before any public
  write-up, and credit reporters who want it.

## Scope

In scope: the library code under `src/`, and the release workflow under
`.github/workflows/`.

Out of scope, and worth stating plainly:

- **The ledger is not a security boundary.** It is a plain JSON file with a
  lock beside it. Anyone who can write to the storage directory can rewrite
  your recorded usage. Put it somewhere only your jobs can write.
- The lock file is created group-writable (0o664) on purpose, so accounts in
  a shared group can co-operate. If that is wrong for your host, point
  `storage_path` at a directory whose permissions enforce what you need.
- This library never handles credentials, never makes network calls, and
  never talks to Google. It only counts.

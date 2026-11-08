# Security policy

## Reporting a vulnerability

Do not open a public issue. Report it privately through GitHub: **Security
→ Report a vulnerability** on this repository. Include what you found, how to
reproduce it, and the impact you expect.

The maintainers aim to:

| Step | Within |
| --- | --- |
| Acknowledge the report | 2 business days |
| Confirm it and rate its severity | 5 business days |
| Release a fix for critical and high issues | 14 days |
| Release a fix for medium and low issues | the next patch release of the latest minor |

Fixes are published as a GitHub security advisory and a patch release, and
reporters are credited unless they ask not to be.

## Supported versions

| Release line | Gets | For how long |
| --- | --- | --- |
| The latest minor of the current major (`2.X`) | Every fix, of every severity, as patch releases | Until the next minor is released |
| 1.8, the last 1.x minor | Fixes for critical and high severity vulnerabilities | Until 2027-11-05, 12 months from the release of 2.0.0 |
| The last 2.x minor, once 3.0 is released | Fixes for critical and high severity vulnerabilities | 12 months from the release of 3.0.0 |
| Every other 1.x and 2.x minor, and every 0.x release | Nothing | |

Only the newest patch release of a supported line is supported. The Python
versions supported are those `requires-python` admits, each of which CI
tests. [docs/versioning.md](docs/versioning.md#security-fixes-and-support)
gives the details: what a fix that cannot be backported safely becomes, and
when a Python version is dropped.

## What domainkit does for you, and what it doesn't

domainkit ships secure defaults: validated settings, deployment checks for
staging and production, hidden SQL parameters, an error format that never
echoes input, trusted-proxy handling, rate limiting, an append-only audit
trail, caches that store JSON only (never pickle) and require TLS and
credentials for network Redis, and message brokers that require TLS and a
user of their own. `domainkit.contrib.auth` ([docs/auth.md](docs/auth.md))
adds users, groups, permissions, password hashing, and token
authentication that makes rate limits, idempotency keys, and the audit
trail per user. With auth installed, which new projects have, every route
must say who may call it (`Require`, or `Public` for anyone): `check` lists
routes that do not, and staging and production refuse to start with any.
Staff using the admin API can grant only the permissions they hold. The
settings reference
([docs/reference.md](docs/reference.md), a page for each area) lists every control, and
[docs/threat-model.md](docs/threat-model.md) what they defend against,
boundary by boundary, and what is left to a project.

## Known exceptions

| Advisory | Package | Why it does not apply |
| --- | --- | --- |
| PYSEC-2026-1325 (CVE-2024-23342) | `ecdsa`, through the `immudb` extra | A timing attack on signing, key generation, and ECDH. immudb's SDK uses `ecdsa` only to verify the server's state signatures with its public key, which the advisory states is unaffected. No fix is planned upstream. CI's pip-audit ignores this one advisory. |

## How this repository stays secure

| Control | Where |
| --- | --- |
| Lint with bandit rules (ruff `S`), strict typing (mypy) | `pyproject.toml`, CI |
| Dependency audit (pip-audit) and secret scan (gitleaks) on every push, and weekly; pip-audit also over the floors of the ranges and their newest releases, and at each release over what the published ranges resolve to | `.github/workflows/ci.yml`, `.github/workflows/release.yml` |
| Static analysis with Semgrep's Python and OWASP Top 10 rules on every push, and weekly | `.github/workflows/ci.yml` |
| CodeQL's security queries, which follow values across functions and modules, on pushes to `main`, pull requests, and weekly; a required check, and a pull request with a new high or critical alert does not merge | `.github/workflows/codeql.yml`, `.github/rulesets/main.json` |
| OpenSSF Scorecard's checks of the repository's practices (pinned actions, token permissions, branch protection, signed releases, fuzzing), on pushes to `main` and weekly; its findings in code scanning, its score published for the README's badge | `.github/workflows/scorecard.yml` |
| The workflows themselves audited by zizmor (injection, persisted credentials, unpinned actions, excessive permissions) on every push, and weekly | `.github/workflows/ci.yml` |
| A CycloneDX bill of materials for every wheel CI builds: the packages the lock resolves for it | `.github/workflows/ci.yml` |
| Actions pinned to commit SHAs and CI's images to digests; dependencies updated by Renovate after 14 days, security fixes at once | `.github/workflows/`, `renovate.json` |
| `main` changes only through pull requests that pass CI; release tags are never moved or deleted (once the rulesets are imported, see `docs/versioning.md`) | `.github/rulesets/` |
| Releases only of the release commit of a version (its changelog section dated, newer than the tags before it), on `main`, whose push run passed every check the ruleset requires, built in CI, attested by GitHub (SLSA build provenance, and the bill of materials), approved in the `pypi` environment by the maintainer (there is one today), and published to PyPI with trusted publishing (no API tokens) and attestations | `.github/workflows/ci.yml` (`attest`), `.github/workflows/release.yml` |
| Fuzz tests of every parser of untrusted input (query strings, entity tags, trace headers, signed values, WebAuthn data, `Accept` and `Accept-Language`), never a 500, with fifty times the examples weekly; property-based tests, random test order, and a weekly mutation run whose score each shard reports | `domainkit_tests/`, `.github/workflows/ci.yml` |

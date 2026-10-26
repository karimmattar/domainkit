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
| Release a fix for medium and low issues | the next minor release |

Fixes are published as a GitHub security advisory and a patch release, and
reporters are credited unless they ask not to be.

## Supported versions

Only the latest minor release gets fixes. Before 1.0 there are no backports.
See [docs/versioning.md](docs/versioning.md), which drafts a longer support
statement for the owner to confirm
([Security fixes and support](docs/versioning.md#security-fixes-and-support)).

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
([docs/reference.md](docs/reference.md)) lists every control, and
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
| Dependency audit (pip-audit) and secret scan (gitleaks) on every push, and weekly | `.github/workflows/ci.yml` |
| Static analysis with Semgrep's Python and OWASP Top 10 rules on every push, and weekly | `.github/workflows/ci.yml` |
| CodeQL's security queries, which follow values across functions and modules, on pushes to `main`, pull requests, and weekly | `.github/workflows/codeql.yml` |
| The workflows themselves audited by zizmor (injection, persisted credentials, unpinned actions, excessive permissions) on every push, and weekly | `.github/workflows/ci.yml` |
| A CycloneDX bill of materials for every wheel CI builds: the packages the lock resolves for it | `.github/workflows/ci.yml` |
| Actions pinned to commit SHAs and CI's images to digests; dependencies updated by Renovate after 14 days, security fixes at once | `.github/workflows/`, `renovate.json` |
| `main` changes only through pull requests that pass CI; release tags are never moved or deleted (once the rulesets are imported, see `docs/versioning.md`) | `.github/rulesets/` |
| Releases only from commits on `main` that pass the whole CI workflow, built in CI, attested by GitHub (SLSA build provenance, and the bill of materials), approved in the `pypi` environment by the maintainer (there is one today), and published to PyPI with trusted publishing (no API tokens) and attestations | `.github/workflows/ci.yml` (`attest`), `.github/workflows/release.yml` |
| Property-based tests, random test order, and a weekly mutation run whose score each shard reports | `domainkit_tests/`, `.github/workflows/ci.yml` |

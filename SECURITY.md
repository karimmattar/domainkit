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
See [docs/versioning.md](docs/versioning.md).

## What domainkit does for you, and what it doesn't

domainkit ships secure defaults: validated settings, deployment checks for
staging and production, hidden SQL parameters, an error format that never
echoes input, trusted-proxy handling, rate limiting, an append-only audit
trail, caches that store JSON only (never pickle) and require TLS and
credentials for network Redis, and message brokers that require TLS and a
user of their own. It does not authenticate requests yet; projects must not expose an
API publicly until they add authentication. The settings reference
([docs/reference.md](docs/reference.md)) lists every control.

## Known exceptions

| Advisory | Package | Why it does not apply |
| --- | --- | --- |
| PYSEC-2026-1325 (CVE-2024-23342) | `ecdsa`, through the `immudb` extra | A timing attack on signing, key generation, and ECDH. immudb's SDK uses `ecdsa` only to verify the server's state signatures with its public key, which the advisory states is unaffected. No fix is planned upstream. CI's pip-audit ignores this one advisory. |

## How this repository stays secure

| Control | Where |
| --- | --- |
| Lint with bandit rules (ruff `S`), strict typing (mypy) | `pyproject.toml`, CI |
| Dependency audit (pip-audit) and secret scan (gitleaks) on every push | `.github/workflows/ci.yml` |
| Actions pinned to commit SHAs; dependencies updated by Renovate after 14 days, security fixes at once | `.github/workflows/`, `renovate.json` |
| Releases built in CI and published to PyPI with trusted publishing (no API tokens) and attestations | `.github/workflows/release.yml` |
| Property-based and mutation tests, random test order | `domainkit_tests/` |

# External security audit: scope

What an outside security review of the framework is asked to cover, in
the order it matters, with the size of each part, so a firm can quote it
and plan it. The framework has had internal reviews only: their findings
are listed, by id, on [the audit ids page](0.9.0.md). This is the first
review from outside.

## What the review is for

Services built on the framework hold money, credentials, and personal
data, and the framework decides much of how they do: who may call a route,
how a token is checked, how an amount is booked, how a key is wrapped. The
review is asked to find what an attacker, a careless operator, or a
malicious tenant can do that the [threat model](../threat-model.md) says
cannot happen, and what the threat model leaves out.

## In scope, by priority

Sizes are lines of code without blank lines or comments, at 1.6.0; the
whole package is about 98,000.

| Priority | Area | Code | Lines | What to look for |
| --- | --- | --- | ---: | --- |
| 1 | Authentication and route access | `contrib/auth`, `http/access.py` | 13,400 | Passwords, opaque tokens, JWT, second factors, cookie sessions, client certificates, scoped personal tokens, sign-up, reset, and verification; the rule every route must declare (deny by default); step-up; impersonation, and that it is audited; the principal cache and its expiry. |
| 1 | Identity providers and devices | `contrib/identity`, `contrib/devices` | 3,200 | OpenID Connect, OAuth 2, SAML 2.0, LDAP, linking a provider to a user; passkeys (WebAuthn), device binding, and signed requests. New since the last internal review. |
| 1 | Money | `money`, `contrib/accounting`, `contrib/limits`, `contrib/approvals`, `contrib/idempotency` | 12,000 | Exact amounts and rounding; double-entry postings under row locks; reversals; closed periods; limits counted with the posting; maker-checker approvals; a retried request never paying twice. |
| 1 | Keys and signatures | `signing.py`, `encryption.py`, `keys`, `contrib/keys`, `db/encrypted.py`, `contrib/webhooks` | 6,400 | Keys derived per purpose from `SECRET_KEY`, and their rotation; field encryption and blind indexes; data keys wrapped by a key in KMS, Vault, or an HSM; per-person keys destroyed on erasure; webhook signatures. |
| 2 | Queries and tenancy | `db/query.py`, `db/repository.py`, `tenancy.py`, `http/filters.py`, `search` | 1,800 | Filters from the query string, and how criteria become SQL. Since 1.6, a repository keeps one statement per shape of query and binds the values as parameters, the criteria that scope a caller to their own rows and tenant included: a statement reused for another caller or tenant must never carry the first one's values. Tenant scoping of rows, caches, and limits. |
| 2 | Untrusted files and requests | `storage`, `http/uploads.py` | 2,000 | Upload limits, virus scanning hooks, presigned URLs, content types; request bodies, cursors, conditional headers (fuzzed already: `domainkit_tests/test_fuzz.py`). |
| 2 | The audit trail | `contrib/audit`, `contrib/evidence`, `ledger` | 5,000 | That who did what cannot be changed, removed, or left unwritten: the hash chain, seals, ledger anchors, and evidence verified against immudb. |
| 3 | Operators | `management`, `contrib/privacy`, `privacy.py` | 6,800 | `manage.py` commands that change data name their operator and are audited; erasure and export reach what they say they reach. |
| 3 | The supply chain | `.github/workflows`, `utils/module_loading.py` | small | The release workflow, its attestations, and the rulesets; backends a setting names by an installed package's entry point (since 1.6), which load only what settings name. |

## Out of scope

- The services the framework talks to (PostgreSQL, Redis, RabbitMQ and the
  other brokers, immudb, cloud key services, identity providers), except
  how the framework configures and trusts them.
- FastAPI, Starlette, SQLAlchemy, Pydantic, and the drivers, except where
  the framework relies on them for a guarantee.
- A project's own code, deployment, and infrastructure.
- Volumetric denial of service; social engineering; physical access.

## What changed since the last internal review

The last internal reviews were of 0.15 and 1.0 ([the audit ids
page](0.9.0.md)). Since then, 1.5 and 1.6 added:

- identity providers, passkeys and device binding, KMS and HSM key
  providers, more brokers, multi-tenancy, search and places, and object
  storage (the adapters, A1 to A8, in [the roadmap](../roadmap.md));
- phone verification (`contrib.phones`);
- statements kept per query shape (P2 in [the roadmap](../roadmap.md));
- backends from installed packages by name (X1 in the roadmap);
- guarded routes that no longer resolve the dependency that only documents
  the bearer token (W1 in the roadmap).

## What the reviewers get

- The repository, the documentation, and the [threat
  model](../threat-model.md), with the [request flow](../request-flow.md)
  and the [hardening checklist](../security.md).
- A running project in minutes: `domainkit init`, then the
  [tutorial](../tutorial.md); the services for the integration tests in
  `compose.yaml` (`make integration`).
- The findings of the internal reviews, by id, on [the audit ids
  page](0.9.0.md); the reviews themselves on request.
- A maintainer who answers questions within a working day.

## What is asked back

- Each finding with its severity (CVSS 3.1, or Critical, High, Medium,
  Low, Info), the steps that reproduce it, and how to fix it.
- A retest of the fixes.
- Findings reported privately, as [SECURITY.md](../../SECURITY.md) says,
  and kept private until a release fixes them; a public summary after
  that, which this page will link to.

# 0124. Write a bank's controls on and wired, as a profile over the project template

- Status: Accepted
- Date: 2026-10-31

## Context

The fintech audit's FIN-74: a bank's first review of a stack asks for its
threat model, its provenance, its key management, maker-checker with
step-up, a verifiable audit trail that names operators, written
idempotency semantics, and a card-number column that a dump cannot read.
0.14 and 0.15 built each control as an app or a setting, every one off
by default, as an app a project does not install costs it nothing. A
team starting a payments service then had to find, install, and
configure eleven apps and their settings, and wire them into a use case,
before its first review; and each it missed was a finding.

`domainkit init` writes a project from one template. Three ways to give
it a bank's controls were tried on the fintech project as it now stands:

| Design | Files written for fintech | Kept apart from the project template | What a change to the project template does |
| --- | --- | --- | --- |
| A. a second template tree | 40 | all 40 | must be made twice; nothing tells when it is not |
| B. a profile: files written over the project template | 29 (22 its own, 7 in place of the template's) | 7 replaced; 11 shared as they are | reaches the 11 shared files at once; the 7 replaced are pinned by tests (the workflow is the project's with `mypy`) |
| C. edits to the project's files after they are written (as `startdomain` installs a domain) | 22, and edits to 7 | none | reaches every file, but an edit that no longer finds its anchor fails at `init` time, and the settings a bank reads are spread over code that writes them |

B keeps every file a reviewer reads as a file (the settings are the
settings, not a program that writes them), and shares what does not
change: the image, its `.dockerignore`, `manage.py`, the ASGI module.

## Decision

- `domainkit init NAME --profile fintech` renders the project template,
  then the profile's files (`management/templates/profiles/fintech`) over
  it, with the same placeholders and one more, the project's ledger
  namespace (`bankAPI`: `bankapi`). The project depends on the extras its
  controls need: `audit`, `httpclient`, `immudb`, and `mfa` besides the
  project's own.
- The defaults the profile changes, and why:

  | Setting | domainkit's default | The profile's | Why |
  | --- | --- | --- | --- |
  | `INSTALLED_DOMAINS` | messaging, contenttypes, auth | also audit, evidence, idempotency, keys, privacy, accounting, approvals, limits, and the project's `controls` and `payments` | every control of the list on |
  | `MIDDLEWARE` | the project's | `IdempotencyMiddleware` last | retried requests run once; it reads whole bodies, which the body limit has bounded |
  | `AUTH.PASSWORD_PRESET` | none | `"pci-dss-4"` | PCI DSS v4.0's requirement 8, refused at startup when contradicted |
  | `APPROVALS.APPROVE_RULE` | none | `StepUp(max_age=5 minutes)` | an approval is an act that asks for a fresh second factor |
  | `APPROVALS.ENTRY_THRESHOLDS`, `ENTRY_APPROVALS` | none, 2 | 10,000.00 USD and EUR, 2 | postings over it need two other clerks |
  | `APPROVALS.SUPERUSER_APPROVALS` | 0 | 2 | no one makes a superuser alone |
  | `LIMITS.DEFAULTS` | none | per customer and currency: 10,000.00 a transfer, 25,000.00 and 50 transfers a day | a stolen login drains at most a day's limit |
  | `KEYS.PROVIDER` | `"local"` | `"local"` in development and tests; in production `aws-kms` or `vault`, from the environment, with no `ALLOW_LOCAL_KEK` | the development key derives from `SECRET_KEY`: `keys.E001` refuses it where deployed |
  | `LEDGERS`, `AUDIT.LEDGER`, `AUDIT.NAMESPACE`, `EVIDENCE` | none | `memory://` in development and tests; immudb with its CA and signing key in production, the trail anchored under the project's namespace | the trail's baseline (`audit.E001` to `E005`) and evidence's subject key are required where deployed |
  | production pool, `MAX_CONCURRENT_REQUESTS` | SQLAlchemy's 5 + 10, 1,000 | 10 + 5, 60, from the environment | `check --deploy` refuses the warnings the project template leaves (`deploy.W004`, `deploy.W006`) |
  | `AUTH.PRINCIPAL_CACHE.STAMPS` in production | none | `"shared"` | a revoked login is refused by every replica at once |
  | the project's `[tool.mypy]`, CI | none | `strict = true`; CI runs `mypy` | the sample's types are checked as domainkit's are |

- Two controls are the project's code, on public names, rather than new
  library settings, so the security review of 0.15 reads no new library
  surface for them: the transfer route refuses a request without an
  `Idempotency-Key` (`400 idempotency_key_required`), and
  `domains.controls` runs `protect()` on the trail's two tables after each
  `migrate` (an `on_migrated` hook), so the project's revisions need no
  edit for `audit.E001` to pass.
- The sample transfer goes through each control: a card registered after
  a step-up, its number `Encrypted(subject="owner_id", blind_index=True)`;
  an amount in `Money`; an authorization the maker confirms for the amount
  and the card, spent at the route before anything else; the maker's
  limit counted in the transfer's transaction (`consume`, behind a port of
  the unit of work); and a guard (`submit=True`) that submits a transfer
  over 1,000.00 for another person's approval (`202`). The card's number
  is in no command, payee, or event: an approval request keeps its command
  as JSON, an authorization its payee, and the trail an event's fields, all
  in clear, so they name the card by its id and its last four digits.

## Consequences

- A new payments project starts with the first-review list answered, and
  `docs/fintech.md` maps each item to its page, setting, and test; the
  tests that page names are checked to exist. `test_init_fintech.py` runs
  the project's CI steps (its 15 tests, ruff, `mypy --strict`, `check`,
  its migrations), `check --deploy` with each key service, each documented
  secret left out, and, on PostgreSQL, the trail made append-only by
  `migrate`.
- The 7 files the profile replaces drift from the project template's
  unless a change is made in both; a test pins the workflow, and the
  settings are read side by side in review.
- The thresholds, limits, and approvers are examples a bank replaces; the
  profile says so where they are set.
- Migrations written on SQLite do not apply on PostgreSQL (a boolean
  column's default is written `0`): the page says to make them against
  the engine that is deployed, as the PostgreSQL test does.

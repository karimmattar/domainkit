# Examples

Each example is a small, complete project that solves one problem
domainkit was built for. `domainkit init` made it, and `manage.py
startdomain` its domain; then the shortest code that solves the problem
properly was written in. Each has a walkthrough that starts from the
problem, and tests.

| Example | The problem | What it uses |
| --- | --- | --- |
| [payments-ledger](payments-ledger/README.md) | A retried payment books twice; two payments at once overdraw a wallet; nothing shows the books add up. | Double-entry books, `Idempotency-Key`, holds, reversals, the trial balance. |
| [maker-checker](maker-checker/README.md) | One person moves a large payout alone, or approves something other than what they read. | Approvals, action authorizations (`StepUp(for_=...)`), the audit trail. |
| [outbox-webhooks](outbox-webhooks/README.md) | Another service hears of a change that rolled back, or never hears of one that committed, and cannot tell a real webhook from a forged one. | The outbox, durable handlers, the inbox, signed webhooks, retries, dead letters. |

Two more are planned, once the fixes to tenancy they need are released:
`saas-tenancy`, a multi-tenant service with organizations in the path,
and `document-intake`, an upload pipeline with checks.

## Run one

Each example runs against the domainkit of this checkout. Its README's
"Run it" gives the commands: a virtual environment, domainkit installed
from `../..` with the extras the example needs, its migrations made, and
its tests. An example holds no migrations: they are written for the
domainkit installed, as a project's are after an upgrade. Nor does it
hold the `.env` that `init` writes, which names the settings module: each
README exports the variable instead.

## How they stay working

domainkit's own tests (`domainkit_tests/test_examples.py`) copy each
example, make its migrations, and run the steps of its CI workflow:
ruff, mypy in strict mode, `check`, `migrate`, `makemigrations --check`,
and its tests. On PostgreSQL its tests run again. So a change to the
framework that breaks an example fails domainkit's CI, and is fixed in
the same pull request. The tests also check that each Python block of a
walkthrough is code the example holds, as it is there.

The examples are not in the package: the wheel and the sdist hold
`src/domainkit` alone. [ADR 0190](../docs/adr/0190-keep-runnable-examples-as-projects-that-domainkits-tests-run.md)
records these choices.

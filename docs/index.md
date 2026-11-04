# domainkit

domainkit brings Django's conventions to domain-driven REST services on
FastAPI: settings, installed domains, generators, migrations, users and
permissions, and a pytest plugin, around aggregates, commands, and events.

These pages describe domainkit 1.0: the pages of each release are under
its tag in the repository (`docs/` at `vX.Y.Z`), and those of `main`
describe the next one. What a 1.x minor adds is marked *New in 1.x* where
it is described; [upgrading.md](upgrading.md) says what changes between
versions.

## Where to start

- **New to domainkit?** [Getting started](tutorial.md): six short parts,
  from `pip install` to a tested service with rules, permissions, and a
  worker.
  [Coming from Django](from-django.md) and
  [coming from FastAPI](from-fastapi.md) map what you know to its name
  here.
- **Doing one thing?** The [how-to recipes](recipes.md): a payment that
  runs again, work that commits once per message, replacing a JWT key,
  running behind a proxy, reading from a replica.
- **Building a service?** The guides: [resources](resources.md) (aggregates
  over HTTP, and the generators), [queries](queries.md),
  [databases](databases.md), [money](money.md), [auth](auth.md), the
  [optional apps](contrib.md) (outbox, audit, idempotency, webhooks,
  evidence, flags, [keys and encrypted fields](contrib/keys.md), the
  [double-entry ledger](contrib/accounting.md),
  [approvals](contrib/approvals.md), [limits](contrib/limits.md)),
  [step-up](auth/step-up.md), [caches](cache.md), [live connections](live.md),
  [uploads](uploads.md), [mail](mail.md), [languages](i18n.md),
  [personal data](privacy.md), [testing](testing.md),
  [how it runs](runtime.md), and [performance](performance.md).
- **Running one?** The [deployment checklist](deploying.md),
  [operations](operations.md), the [runbook](runbook.md),
  the [hardening checklist](security.md), the
  [threat model](threat-model.md), [a bank's first review](fintech.md),
  and [upgrading](upgrading.md).
- **Looking something up?** [Settings, commands, and errors](reference.md),
  the [public API](api.md) and its generated reference, the
  [compatibility policy](versioning.md), and the
  [decisions](adr/README.md) behind the design.

The source, issues, and releases are on
[GitHub](https://github.com/karimmattar/domainkit); every change is in the
[CHANGELOG](../CHANGELOG.md).

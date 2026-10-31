# Optional apps

Install an app by adding it to `INSTALLED_DOMAINS`, then run
`makemigrations` and `migrate`: each one brings its own tables. Each is
configured by one setting, a dictionary whose keys are all optional.

| App | Setting | Tables |
| --- | --- | --- |
| `domainkit.contrib.messaging` | `MESSAGING` | `domainkit_messages`, `domainkit_inbox`, `domainkit_ordering_keys` |
| `domainkit.contrib.webhooks` | `WEBHOOKS` | `domainkit_webhook_subscriptions`, `_deliveries`, `_attempts` |
| `domainkit.contrib.audit` | `AUDIT` | `domainkit_audit_log` |
| `domainkit.contrib.idempotency` | `IDEMPOTENCY` | `domainkit_idempotency_keys` |
| `domainkit.contrib.evidence` | `EVIDENCE` | none: records live on a ledger |
| `domainkit.contrib.keys` | `KEYS` | `domainkit_keys_subject_keys`, `domainkit_keys_erasures`, in every database that hosts a domain; see [keys](contrib/keys.md) |
| `domainkit.contrib.contenttypes` | none | `domainkit_content_types` |
| `domainkit.contrib.auth` | `AUTH` | users, groups, permissions, and the principal cache; see [auth.md](auth.md) |
| `domainkit.contrib.privacy` | none | none: exports, erases, and purges domains' rows; see [privacy.md](privacy.md) |
| `domainkit.contrib.flags` | `FLAGS` | `domainkit_flags`, `domainkit_dynamic_settings` |
| `domainkit.contrib.objectpermissions` | none | `domainkit_object_permissions`, in every database that hosts a domain; see [auth.md](auth/users.md#grants-on-single-objects) |
| `domainkit.contrib.limits` | `LIMITS` | `domainkit_limits_limits`, `_subjects`, `_usage`, in the ledger's database when it is installed; see [limits](contrib/limits.md) |
| `domainkit.contrib.accounting` | `ACCOUNTING` | `domainkit_accounting_accounts`, `_periods`, `_entries`, `_postings`, `_holds`, `_statement_lines`, `_matches`, `_breaks`, in one database; see [accounting](contrib/accounting.md) |
| `domainkit.contrib.approvals` | `APPROVALS` | `domainkit_approval_requests`, `domainkit_approval_decisions`, in one database; see [approvals](contrib/approvals.md) |

With several databases ([databases.md](databases.md)), the outbox, its
inbox and ordering keys, and the audit trail commit with business data, so their tables are
created in every database that hosts a domain. The idempotency table stays in its own
`DATABASE`.

### What each app needs

Some apps cannot work without another; each declares so in
`DomainConfig.requires`, and `setup()` refuses to start, naming every app
missing, before any app's `ready()` runs:

| App | Requires | Because |
| --- | --- | --- |
| `domainkit.contrib.auth` | `domainkit.contrib.contenttypes` | Permissions hang off content types. |
| `domainkit.contrib.mail` | `domainkit.contrib.messaging` | The worker sends the mail. |
| `domainkit.contrib.notifications` | `domainkit.contrib.messaging` | The worker sends the messages. |
| `domainkit.contrib.webhooks` | `domainkit.contrib.messaging` | Deliveries are outbox tasks. |
| `domainkit.contrib.privacy` | `domainkit.contrib.audit` | It records every operation in the audit trail. |
| `domainkit.contrib.approvals` | `domainkit.contrib.messaging`, `domainkit.contrib.audit` | Approved commands run in the worker; every decision is audited. |

The order in `INSTALLED_DOMAINS` does not matter for these. Other apps use
messaging when it is installed (scheduled purges and seals, durable
recording) and work without it. A project's own domains declare theirs the
same way:

```python
from domainkit.apps import DomainConfig, Requirement


class BillingConfig(DomainConfig):
    requires = (
        "domainkit.contrib.auth",
        Requirement("domainkit.contrib.messaging", "invoices are sent by the worker"),
    )
```

```text
billing needs 'domainkit.contrib.messaging' in INSTALLED_DOMAINS: invoices are sent by the worker.
```

An app uses another only through its public names: the app's package and
the modules listed in [api.md](api.md). Imports of another app's internal
modules that predate this rule are listed, with their reasons, in
`domainkit_tests/test_contrib_boundaries.py`; the test fails on a new one,
and 0.10 removes the rest ([ADR 0077](adr/0077-declare-what-apps-require.md)).

This page has moved, a part a page:

- [Messaging: the outbox, durable handlers, and background tasks](contrib/messaging.md)
- [Webhooks: signed events for other services](contrib/webhooks.md)
- [Audit: who did what, when, from where](contrib/audit.md)
- [Idempotency: safe retries of unsafe requests](contrib/idempotency.md)
- [Evidence: records that provably never change](contrib/evidence.md)
- [Keys: encrypted fields under a KMS, and crypto-shredding](contrib/keys.md)
- [Flags: feature flags and dynamic settings](contrib/flags.md)
- [Accounting: double-entry books, holds, periods, and reconciliation](contrib/accounting.md)
- [Approvals: maker-checker for sensitive commands](contrib/approvals.md)
- [Limits: transaction and velocity limits on money](contrib/limits.md)

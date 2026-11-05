# Optional apps

domainkit comes with optional apps under `domainkit.contrib`. Each app
adds one feature that many services need, such as an outbox, an audit
trail, or users. You turn on only the apps you use. This page lists
every app, the page that explains it, its setting and its tables, and
which apps need which.

## The apps and their guides

| App | What you get | Guide |
| --- | --- | --- |
| `domainkit.contrib.messaging` | The outbox, durable event handlers, background and periodic tasks, and the worker that runs them. | [Messaging](contrib/messaging.md) |
| `domainkit.contrib.webhooks` | Domain events sent, signed, to URLs that staff or users subscribe. | [Webhooks](contrib/webhooks.md) |
| `domainkit.contrib.audit` | A record of who did what, when, and from where, sealed in a hash chain. | [Audit](contrib/audit.md) |
| `domainkit.contrib.idempotency` | Safe retries of `POST` and `PATCH` requests with an `Idempotency-Key`. | [Idempotency](contrib/idempotency.md) |
| `domainkit.contrib.evidence` | Records on a ledger that provably never change. | [Evidence](contrib/evidence.md) |
| `domainkit.contrib.keys` | Encrypted fields under a key management service, and erasure by destroying a person's key. | [Keys](contrib/keys.md) |
| `domainkit.contrib.flags` | Feature flags and settings you change at run time, without a deployment. | [Flags](contrib/flags.md) |
| `domainkit.contrib.accounting` | Double-entry books: accounts, entries, holds, periods, and reconciliation. | [Accounting](contrib/accounting.md) |
| `domainkit.contrib.approvals` | Maker-checker: a sensitive command runs only after other people approve it. | [Approvals](contrib/approvals.md) |
| `domainkit.contrib.limits` | Limits on how much money may move, at once and in a time window. | [Limits](contrib/limits.md) |
| `domainkit.contrib.auth` | Users, groups, permissions, tokens, logins, second factors, and the admin API. | [Users and permissions](auth.md) |
| `domainkit.contrib.contenttypes` | The kinds of stored data, which permissions are attached to. | [Installing auth](auth/install.md) |
| `domainkit.contrib.objectpermissions` | Permissions granted on one object only. | [Grants on single objects](auth/users.md#grants-on-single-objects) |
| `domainkit.contrib.identity` | Logins through OpenID Connect, OAuth 2, SAML, and LDAP. | [Identity providers](auth/identity.md) |
| `domainkit.contrib.devices` | Passkeys, keys bound to a user's devices, and push tokens. | [Passkeys and bound devices](auth/devices.md) |
| `domainkit.contrib.phones` | Phone numbers that a user proved they receive text messages at. | [Phone numbers](auth/phones.md) |
| `domainkit.contrib.ciba` | Sign-ins approved from the user's phone: push, QR codes, and OIDC CIBA. | [Sign-in approvals](auth/ciba.md) |
| `domainkit.contrib.mail` | Email, sent by the worker through the outbox. | [Mail](mail.md) |
| `domainkit.contrib.notifications` | Text messages (SMS) and push notifications, sent by the worker. | [Text messages and push notifications](mail.md#text-messages-and-push-notifications) |
| `domainkit.contrib.privacy` | Export and erasure of a person's data, and purges of old rows. | [Privacy requests and retention](privacy.md) |
| `domainkit.contrib.admin` | A generic admin API over REST for any aggregate. It is not an installed app: you register aggregates with it and mount its router. | [The generic admin](resources.md#the-generic-admin) |

## Installing an app

Add the app to `INSTALLED_DOMAINS`, then write and apply a migration for
its tables:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.audit",
    "domains.accounts",
]
```

```bash
python manage.py makemigrations -m "audit"
python manage.py migrate
```

A new project from `domainkit init` already has `messaging`,
`contenttypes`, and `auth`.

## Settings and tables

An app reads one setting, named after the app's label in capital
letters. The setting is a dictionary, and all of its keys are optional.
Each guide has the table of keys, and
[settings](reference/settings.md) explains how settings load.

| App | Setting | Tables |
| --- | --- | --- |
| `messaging` | `MESSAGING` | `domainkit_messages`, `domainkit_inbox`, `domainkit_ordering_keys` |
| `webhooks` | `WEBHOOKS` | `domainkit_webhook_subscriptions`, `_deliveries`, `_attempts` |
| `audit` | `AUDIT` | `domainkit_audit_log`, `_seals`, `_checkpoints`, `_keys` (the keys of the pseudonyms), and `domainkit_ledger_states` |
| `idempotency` | `IDEMPOTENCY` | `domainkit_idempotency_keys` |
| `evidence` | `EVIDENCE` | None: the records live on a ledger. |
| `keys` | `KEYS` | `domainkit_keys_subject_keys`, `domainkit_keys_erasures` |
| `flags` | `FLAGS` | `domainkit_flags`, `domainkit_dynamic_settings` |
| `accounting` | `ACCOUNTING` | `domainkit_accounting_accounts`, `_periods`, `_entries`, `_postings`, `_holds`, `_statement_lines`, `_matches`, `_breaks` |
| `approvals` | `APPROVALS` | `domainkit_approval_requests`, `domainkit_approval_decisions` |
| `limits` | `LIMITS` | `domainkit_limits_limits`, `_subjects`, `_usage`, `_kept` |
| `auth` | `AUTH` | `domainkit_users`, `_groups`, `_permissions`, `_users_groups`, `_users_permissions`, `_group_permissions`, `_tokens`, `_mfa_devices`, `_recovery_codes`, `_pending_signups`, `_step_ups`, `_sca_challenges`, `_password_history` |
| `contenttypes` | None | `domainkit_content_types` |
| `objectpermissions` | None | `domainkit_object_permissions` |
| `identity` | `IDENTITY` | `domainkit_identities`, `domainkit_identity_flows`, `domainkit_identity_tickets` |
| `devices` | `DEVICES` | `domainkit_passkeys`, `domainkit_passkey_challenges`, `domainkit_devices`, `domainkit_push_tokens` |
| `phones` | `PHONES` | `domainkit_phone_codes`, `domainkit_phones` |
| `ciba` | `CIBA` | `domainkit_ciba_clients`, `domainkit_ciba_requests`, `domainkit_ciba_assertions` |
| `mail` | `MAIL` | None |
| `notifications` | `NOTIFICATIONS` | None |
| `privacy` | None | None: it exports, erases, and purges the rows of other domains. |

## Apps and several databases

A project can use several databases ([databases](databases.md)). Each
app puts its tables where its work commits:

- **In every database that hosts a domain:** the outbox, its inbox and
  ordering keys (`messaging`), the audit trail (`audit`), the people's
  keys (`keys`), and the object permissions (`objectpermissions`). They
  commit in the same transaction as the business data beside them.
- **In one database of their own choice:** the idempotency keys stay in
  `IDEMPOTENCY["DATABASE"]`, apart from the business data.
- **In one database:** `accounting` and `approvals` keep all their
  tables together. `limits` keeps its tables in the books' database
  when `accounting` is installed. `phones` and `ciba` must share the
  database of `auth`.
- **In no SQL database:** `evidence` writes to a ledger, and `privacy`
  keeps no tables.

## What each app needs

Some apps cannot work without another app. Each one says so in
`DomainConfig.requires`. Before any app's `ready()` runs, `setup()`
checks every requirement, and refuses to start if an app is missing. It
names every missing app at once.

| App | Requires | Because |
| --- | --- | --- |
| `domainkit.contrib.auth` | `domainkit.contrib.contenttypes` | Permissions are attached to content types. |
| `domainkit.contrib.mail` | `domainkit.contrib.messaging` | The worker sends the mail. |
| `domainkit.contrib.notifications` | `domainkit.contrib.messaging` | The worker sends the messages. |
| `domainkit.contrib.webhooks` | `domainkit.contrib.messaging` | Deliveries are tasks in the outbox. |
| `domainkit.contrib.privacy` | `domainkit.contrib.audit` | It records every operation in the audit trail. |
| `domainkit.contrib.approvals` | `domainkit.contrib.messaging`, `domainkit.contrib.audit` | Approved commands run in the worker, and every decision is audited. |
| `domainkit.contrib.identity` | `domainkit.contrib.auth` | A provider's login becomes a user. |
| `domainkit.contrib.devices` | `domainkit.contrib.auth` | Passkeys and devices belong to users. |
| `domainkit.contrib.phones` | `domainkit.contrib.auth`, `domainkit.contrib.messaging`, `domainkit.contrib.notifications` | The worker sends the codes by text message. |
| `domainkit.contrib.ciba` | `domainkit.contrib.auth`, `domainkit.contrib.messaging`, `domainkit.contrib.notifications`, `domainkit.contrib.devices` | Bound devices approve; the worker sends the pushes. |

The order in `INSTALLED_DOMAINS` does not matter for these. Other apps
use `messaging` when it is installed, for example for scheduled purges,
seals, and durable recording, and work without it.

Your own domains declare what they need in the same way. Give a reason
with `Requirement`, or name the package alone:

```python
from domainkit.apps import DomainConfig, Requirement


class BillingConfig(DomainConfig):
    requires = (
        "domainkit.contrib.auth",
        Requirement("domainkit.contrib.messaging", "invoices are sent by the worker"),
    )
```

Without `messaging` in `INSTALLED_DOMAINS`, `setup()` raises
`CheckFailed`:

```text
Installed domains need what INSTALLED_DOMAINS lacks:
  apps.E001: billing needs 'domainkit.contrib.messaging' in INSTALLED_DOMAINS: invoices are sent by the worker.
```

## How apps use each other

An app uses another app only through its public names: the app's
package, and the modules that [the public API](api.md) lists. A few
older imports of another app's internal modules remain. The test
`domainkit_tests/test_contrib_boundaries.py` lists them with their
reasons. It fails on a new one, and the list may only shrink
([ADR 0077](adr/0077-declare-what-apps-require.md)).

## The guides

This page was once one long page. Each app now has its own:

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

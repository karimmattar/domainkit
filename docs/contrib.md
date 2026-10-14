# Optional apps

Install an app by adding it to `INSTALLED_DOMAINS`, then run
`makemigrations` and `migrate`: each one brings its own tables. Each is
configured by one setting, a dictionary whose keys are all optional.

| App | Setting | Tables |
| --- | --- | --- |
| `domainkit.contrib.messaging` | `MESSAGING` | `domainkit_messages` |
| `domainkit.contrib.audit` | `AUDIT` | `domainkit_audit_log` |
| `domainkit.contrib.idempotency` | `IDEMPOTENCY` | `domainkit_idempotency_keys` |
| `domainkit.contrib.evidence` | `EVIDENCE` | none: records live on a ledger |

With several databases ([databases.md](databases.md)), the outbox and the
audit trail commit with business data, so their tables are created in every
database that hosts a domain. The idempotency table stays in its own
`DATABASE`.

## Messaging: outbox, durable handlers, background tasks

Work that must happen after a change commits, even if the process dies right
after, is written to an outbox table **in the same transaction as the
change**. A worker process (`python manage.py runworker`) runs it.

```python
from domainkit.contrib.messaging import periodic, task


@task  # or @task(name=..., max_attempts=...)
async def send_welcome(user_id: UUID) -> None: ...


# In a use case: stored only if the change commits.
async with uow:
    ...
    uow.defer(send_welcome, user_id=user.id)  # delay=, dedupe_key=
    await uow.commit()

# In DomainConfig.ready(): run the handler in the worker, not in the request.
event_bus.subscribe(UserCreated, notify_crm, durable=True)


@periodic(every=timedelta(hours=1))  # once per interval across all workers
async def purge_expired() -> None: ...
```

Outside a unit of work, `await enqueue(send_welcome, user_id=...)` queues a
task in its own transaction. Arguments are stored as JSON and converted back
using the task's type hints (`UUID`, `datetime`, `Decimal`, models).

**Delivery is at least once.** A message is claimed with
`FOR UPDATE SKIP LOCKED` (so any number of workers can share the outbox) and
locked for `VISIBILITY_TIMEOUT`. If its worker dies, another claims it again.
Handlers and tasks must therefore be idempotent. Order between messages is
not guaranteed.

**Failures** are retried with exponential backoff and jitter, up to
`max_attempts`. Then the message is dead-lettered and sent to
`ERROR_REPORTERS`. A message naming an unknown task or handler is
dead-lettered at once. `python manage.py messages --dead` lists dead
letters, and `--retry ID` (or `all`) queues them again after a fix.

A unit of work that defers tasks or publishes events with durable handlers,
without messaging installed, fails instead of losing the work.

**Several databases.** Each database that hosts domains (each shard, too)
has its own outbox, written in the same transaction as that database's
changes. `runworker` runs one worker per outbox, all in one process by
default; `runworker --database <target>` runs only a target's, so groups can
scale apart. Periodic tasks and `enqueue()` use the home outbox,
`MESSAGING.DATABASE`, which must be one alias. `messages --database <alias>`
inspects any outbox.

| `MESSAGING` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | The home outbox: periodic tasks and `enqueue()` write here, and its worker schedules periodic tasks. One alias, not a shard group. |
| `MAX_ATTEMPTS` | `10` | Runs before dead-lettering, unless the task sets its own. |
| `RETRY_BASE_SECONDS` | `2.0` | First retry delay; it doubles per attempt. |
| `RETRY_MAX_SECONDS` | `3600.0` | Cap on the retry delay. |
| `TASK_TIMEOUT` | `240.0` | Seconds a message may run; longer counts as a failure. |
| `VISIBILITY_TIMEOUT` | `300.0` | Seconds a claim lasts; must exceed `TASK_TIMEOUT`. |
| `POLL_INTERVAL` | `1.0` | Seconds an idle worker waits before looking again. |
| `BATCH_SIZE` | `50` | Most messages claimed at once. |
| `CONCURRENCY` | `10` | Messages one worker runs at the same time. |
| `RETENTION_SECONDS` | `604800.0` | Finished messages are deleted after this (7 days). |
| `CLEANUP_INTERVAL` | `3600.0` | Seconds between those deletions. |

Run `runworker --metrics-port 9100` to expose the worker's metrics. Scale by
running more workers; `SIGTERM` lets running messages finish first.

## Audit: who did what, when, from where

Every committed domain event becomes a row in `domainkit_audit_log`, written
in the same transaction as the change. A row holds:

- `occurred_at`, `actor`, `request_id`, and `client_ip`, from the request
- `action`, named `<domain label>.<event in snake case>` (`catalog.product_created`)
- `outcome`, `target`, `event_id`, and the event's fields as `details`

Record what no event describes, such as refused logins or reads of sensitive
data, in its own transaction:

```python
from domainkit.contrib.audit import Outcome, record

await record("auth.login", Outcome.DENIED, target=username, reason="mfa")
```

`python manage.py auditlog --actor alice --action catalog.product_created` reads the
trail. Events are audited in the database where they commit, so with several
databases, `auditlog --database <alias>` reads another one.

The application only inserts. To make sure a compromised API cannot rewrite
history, make the table append-only in the database itself. Add a revision
that calls `domainkit.contrib.audit.protection.protect(op)` in `upgrade()`
and `unprotect(op)` in `downgrade()`. On PostgreSQL this installs triggers
that refuse `UPDATE`, `DELETE`, and `TRUNCATE` for every role. Only the table
owner (the migration role) can drop them, for example to apply a retention
policy:

```python
from alembic import op

from domainkit.contrib.audit.protection import protect, unprotect


def upgrade() -> None:
    protect(op)


def downgrade() -> None:
    unprotect(op)
```

| `AUDIT` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Where `record()` writes, and what `auditlog` reads by default. |
| `EVENTS` | `True` | Record every committed domain event. |
| `EXCLUDE` | `()` | Actions to skip, as shell patterns (`catalog.*`, `*.viewed`). |

## Idempotency: safe retries of unsafe requests

A client sends `Idempotency-Key: <random UUID>` with a `POST` or `PATCH`. The
first request runs and its response is stored. A retry with the same key and
the same request gets the stored response, with `Idempotent-Replayed: true`.

| Situation | Answer |
| --- | --- |
| Same key, same method, path, query and body | The stored response, replayed |
| Same key, different request | 422 `idempotency_key_reused` |
| Same key while the first is still running | 409 `idempotency_key_in_use`, `Retry-After: 1` |
| Key not 1 to 255 printable ASCII characters | 400 `invalid_idempotency_key` |
| First response was a 5xx, or larger than `MAX_RESPONSE_BYTES` | Not stored: the retry runs again |

Keys are scoped to the actor, or to the client address for anonymous
callers, so one caller cannot replay another's response. Add
`IdempotencyMiddleware` last in `MIDDLEWARE`: it reads whole bodies, which
`RequestBodyLimitMiddleware` has already bounded. With messaging installed,
expired keys are purged hourly.

| `IDEMPOTENCY` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Alias holding the keys. |
| `TTL_SECONDS` | `86400.0` | How long a key is remembered (24 hours). |
| `METHODS` | `POST`, `PATCH` | Methods that honour the header; others ignore it. |
| `MAX_RESPONSE_BYTES` | `1048576` | Larger responses are passed through but not stored. |

## Evidence: records that provably never change

Evidence is what you may have to prove later, exactly as it was: a
verification result, a signed consent, a regulatory submission. It goes to a
ledger (an immudb database, [ADR 0011](adr/0011-immudb-ledger-for-evidence.md)),
not to PostgreSQL. The audit trail records who did what; evidence records
facts and the digests of the documents behind them.

```python
from domainkit.contrib.evidence import Evidence, evidence_store, sha256

evidence = Evidence(
    kind="kyc.document_verified",
    subject="customer:42",
    facts={"result": "match", "model": "v3"},
    artifacts={"passport": sha256(document_bytes)},
)
receipt = await evidence_store().record(evidence)  # keep it with your data
recorded = await evidence_store().verify(receipt)  # raises EvidenceTampered
```

- A record is canonical JSON (sorted keys, no spaces, UTF-8) with the id,
  kind, subject, facts, artifact digests, time, actor, and request id. The
  receipt holds its key, its SHA-256 digest, and the ledger transaction.
- Keys are `<NAMESPACE>/<subject digest>/<evidence id>`: no personal data
  in keys, and `for_subject("customer:42")` is a prefix scan.
- Recording is idempotent by evidence id: a retry returns the same receipt,
  and another record with that id is an `EvidenceConflict` (409).
- Reads return revision 1, the original. If anything wrote the key again,
  `RecordedEvidence.intact` is false and `verify()` raises
  `EvidenceTampered`. With immudb, every read and write is also checked
  against a Merkle proof and, in deployments, the server's signature.
- Receipts are what tie evidence to your data: anyone holding the ledger's
  write credentials can add records, though never change one unnoticed. Keep
  each receipt (or key) with the change it proves; `for_subject` is a
  convenience listing.
- Inside a use case, `key = defer_evidence(uow, evidence)` records it
  through the outbox once the change commits (messaging must be
  installed). Store the key with the change.

**Never put personal data in facts**: a ledger cannot forget. Store
references or digests, or encrypt personal fields with keys you can destroy.

`python manage.py evidence --subject customer:42` lists a subject's records;
`evidence --verify <key>` proves one.

| `EVIDENCE` key | Default | Meaning |
| --- | --- | --- |
| `LEDGER` | `"evidence"` | Alias in `LEDGERS` (`immudb://`; `memory://` in tests). |
| `NAMESPACE` | `"evidence"` | Key prefix, so projects or environments can share one ledger. |
| `MAX_BYTES` | `262144` | Largest record; keep documents elsewhere and record their digests. |

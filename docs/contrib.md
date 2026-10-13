# Optional apps

Install an app by adding it to `INSTALLED_DOMAINS`, then run
`makemigrations` and `migrate`: each one brings its own tables. Each is
configured by one setting, a dictionary whose keys are all optional.

| App | Setting | Tables |
| --- | --- | --- |
| `domainkit.contrib.messaging` | `MESSAGING` | `domainkit_messages` |
| `domainkit.contrib.audit` | `AUDIT` | `domainkit_audit_log` |
| `domainkit.contrib.idempotency` | `IDEMPOTENCY` | `domainkit_idempotency_keys` |

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

| `MESSAGING` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Alias holding the outbox; the units of work that defer must use it. |
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
trail.

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
| `DATABASE` | `"default"` | Alias holding the trail; event audits need units of work on it. |
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

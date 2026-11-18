# 0187. Keep the worker's heartbeat to its liveness, fail deliveries whose task ended, and share provider clients

- Status: Accepted
- Date: 2026-11-18
- Amends: [0142](0142-while-the-broker-is-away-relaying-workers-run-the-outbox.md)

## Context

The 2.8 audit found three defects in how the worker and the backends
that call other services behave when something they depend on fails.

1. **The heartbeat ages while the broker is away (A28-REL-02).**
   `runworker --heartbeat-file` rewrote its file only while `/ready`'s
   checks (domains, databases, ledgers, caches) and the broker's held.
   The documented health check restarts a container whose file is older
   than 60 seconds. While the broker is away, the relaying workers run
   the outbox themselves
   ([0142](0142-while-the-broker-is-away-relaying-workers-run-the-outbox.md)),
   but the health check killed them in a loop: each kill stopped the
   running messages and started the domains again, keys unwrapped
   included. A cache that fails open (`READINESS` on) did the same to
   healthy workers. The heartbeat came in 2.4, before 0142, and was never
   brought in line with it.
2. **A delivery whose last attempt ends outside `DeliveryFailed` stays
   `pending` (A28-REL-05).** `deliver` marked a delivery `failed` only
   once the last attempt's outcome was written. When the worker was
   killed during that attempt, when the attempt ran past its time limit,
   or when the database failed as the outcome was written, the task died
   and the delivery stayed `pending` for good. `failed_deliveries` did
   not list it, `replay` refused it, and the hourly purge never removed
   it, though the docs said it was marked failed.
3. **Backends made per call made a client per call (A28-ARCH-02).**
   `get_sms_backend()`, `search_backend()`, `identity.services.provider()`
   and each captcha check made a new `HttpClient`, so a new circuit
   breaker and a new pool. A provider that was down was called, and
   waited for, on every call: of 12 sends to a Twilio answering 503, 12
   reached it, against 5 with one client kept. Each call opened a
   connection, and the pools were left to the garbage collector. Mail
   kept one client per timeout, and push used the text messages'
   `shared_client`, which the text messages did not use themselves.

### What the sources say

- Kubernetes: a failed liveness probe restarts the container, and a
  failed readiness probe takes it out of traffic. "Liveness probes must
  be configured carefully to ensure that they truly indicate
  unrecoverable application failure, for example a deadlock", and an
  incorrect one "can lead to cascading failures". Where an application
  depends on back-end services, "the liveness probe passes when the app
  itself is healthy, but the readiness probe additionally checks that
  each required back-end service is available" [1].
- HTTPX: requests made without a client open "a new connection for every
  single request"; a client "will reuse the underlying TCP connection",
  and anything beyond a script should use one [2].
- Celery acknowledges a message whose worker process was killed, so that
  a task that kills its worker does not come back in a loop, and records
  its failure [3]. domainkit's worker does the same with
  `AttemptsExhausted`; what was missing is the delivery's own record.

### Options

For the heartbeat:

- **Check only what the worker needs to make progress** (its outbox
  databases, and the broker for a `--no-relay` consumer), as the audit
  proposed. A restart does not bring a database back, and a worker
  already waits out a database outage, longer each time; such a check
  would still restart every worker during a database outage.
- **Add `--heartbeat-strict` for the old behaviour.** It keeps a flag
  whose use is the defect.
- **Make the heartbeat liveness alone, and report readiness apart.** The
  file is rewritten from the loop whatever the checks find; a second
  file holds the checks' verdict. Chosen.

For the deliveries:

- **Mark the delivery failed in `deliver`, around the last attempt.**
  Covers a write that fails, if the database answers the second write;
  not a kill, nor a time limit, which cancel the task.
- **A hook in the worker when a task dies** (`task(on_dead=...)`, as
  Dramatiq's `on_retry_exhausted` [4]). A new public option of `task`, for
  one caller; it misses a task an operator cancels or drops, and
  deliveries stuck since an earlier release.
- **A sweep of pending deliveries whose task no longer waits.** Covers
  every way a task ends, those of earlier releases included. Chosen,
  with the first option for the common case, so most deliveries are
  failed at once.

For the clients:

- **Cache the backends** against their settings, as `renderer()` does.
  `search_backend()` documents a new backend per call, and a test's
  `MemorySearchBackend` would then keep its index between tests.
- **One public `shared_client(**options)` registry in
  `domainkit.http.client`.** A public name keyed on every option of
  `HttpClient` (headers, networks, transports), for four callers.
- **A shared client per adapter, as mail and push already have.**
  Chosen. Shared provider scaffolding (`ProviderBackend[T, Options]`,
  the audit's E2) would give mail, text messages and push one base that
  owns the client; it changes three public class hierarchies and is
  left for later.

## Decision

1. **The heartbeat is the worker's liveness.** `--heartbeat-file` is
   rewritten every `--heartbeat-interval` seconds, from the worker's own
   event loop, before the round's checks and whatever they find. A stuck
   loop lets it age; a broker, a database, or a cache away does not.
2. **Readiness is a file of its own.** `--ready-file` is rewritten in the
   same round while `/ready`'s checks and the broker's hold, and removed
   when one fails. A failing check is logged, with or without the file.
   Both files go when the worker stops. A project that wants the old
   restarts points its health check at the ready file.
3. **A stopping relay waits for the drain only while it runs the
   outbox.** It gets what is left of `DRAIN_TIMEOUT`, then
   `HELPER_GRACE`, when it still runs messages it claimed while the
   broker was away. A relay that is publishing, to a broker that
   answers, gets `HELPER_GRACE` alone, as the code's comment said
   (A28-TEST-07).
4. **Every way a delivery's task ends marks it failed.** A last attempt
   that raises marks its delivery `failed` before the task dies, if the
   database lets it. The hourly purge first marks `failed` each pending
   delivery with no task waiting to run (dead, cancelled, or dropped),
   with a `last_error` that says the attempt ended without an outcome,
   and logs how many. A delivery and its task are written in one
   transaction, and so are a replay and its task, so a pending delivery
   without one has no attempt to come. The purge matches a task to its
   delivery by the id in its arguments, in one statement on PostgreSQL
   and SQLite, through `messaging.admin.unfinished_arguments`: webhooks
   reads the outbox through messaging's public names.
5. **An outcome stays.** An attempt's outcome is written only to a
   delivery still `pending`. A late duplicate run counts and logs its
   attempt, and leaves `succeeded` or `failed` as it was.
6. **Backends share their client.** Text message providers use
   `shared_client(TIMEOUT)`, as push does; the search engines share one
   client per URL and timeout; identity providers share one client; and
   captcha checks share one. Each is an ordinary `HttpClient`:
   `close_all`, which shutdown runs, closes its pool.
7. **Tests start with closed circuits.** `domainkit.http.client.
   reset_circuits()` and `CircuitBreaker.reset()` forget every failure.
   The test plugin calls the first before each test, so a client shared
   between tests carries no open circuit into the next.

## Consequences

- During a broker or cache outage the workers keep running, and the
  relays run the outbox, as 0142 intended. An operator who relied on a
  restart when the broker or a database failed moves the health check
  to the ready file. `upgrading.md` says so.
- A degraded worker is visible: a fresh heartbeat beside a missing
  ready file, and a warning each round.
- `beat` touches its path whatever the checks find, and takes `ready`;
  `heartbeat`'s path may be None when only the ready file is wanted.
- A delivery whose task died is `failed` and replayable with
  `webhooks replay`. Retrying its dead task with `messages retry` sends
  nothing, since the task finds the delivery finished. Deliveries stuck
  `pending` by an earlier release are marked failed by the first hourly
  purge after the upgrade.
- The sweep reads every pending delivery and every waiting `deliver`
  task once an hour. On PostgreSQL it is a hash anti-join; on SQLite, a
  scan per pending delivery.
- A provider that is down now fails fast after `BREAKER_FAILURES`
  failures, with `CircuitOpen` in the error's text (`Twilio could not be
  reached (CircuitOpen).`), for `BREAKER_COOLDOWN` seconds. Text
  messages retry as before; a captcha answers 503 as before, at once;
  an identity login fails at once.
- The circuits of the shared clients count every caller's failures: one
  per host, as for mail and push.
- The worker's log lines about a failing message name its id
  (A28-REL-08): log rules that match their whole text change.

## Sources

1. Kubernetes, "Liveness, Readiness, and Startup Probes": <https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/>
2. HTTPX, "Clients": <https://www.python-httpx.org/advanced/clients/>
3. Celery, "Tasks" (`acks_late`, `task_reject_on_worker_lost`, `on_failure`): <https://docs.celeryq.dev/en/stable/userguide/tasks.html>
4. Dramatiq, API reference, `Retries` (`on_retry_exhausted`: "Name of an actor to send a message to when message is failed due to retries being exceeded"): <https://dramatiq.io/reference.html>

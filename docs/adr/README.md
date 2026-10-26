# Architecture decision records

Each record explains one decision that is costly to reverse: its context, what
was decided, and what follows from it. Add a record before such a change, by
copying [template.md](template.md) to the next number.

Never rewrite an accepted record's decision. To change it, either append a
dated `## Amendment (YYYY-MM-DD): ...` section at its end, or supersede it
with a new record that says `- Amends:` or `- Supersedes:` the old one; link
both ways. Either way the old record's status line says so, and this index
repeats it: `Accepted; amended 2026-10-25 (at the end)`,
`Accepted; amended by` and a link to the new record, or `Superseded by` and
that link. The `(Amended YYYY-MM-DD.)` marks inside 0023, 0056, 0057, 0061,
and 0062 are earlier corrections made in place, before this rule; their
status says `amended in place`. Number 0067 was never used.
`domainkit_tests/test_docs.py` checks the order of this index, its
statuses, and the amendments they name.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-django-conventions-for-ddd-services.md) | Django's conventions for domain-driven services | Accepted |
| [0003](0003-enforced-layer-rules.md) | Layer rules enforced by `check` | Accepted; amended by [0037](0037-one-clock-frozen-per-context.md), and 2026-10-25 (at the end) |
| [0004](0004-async-sqlalchemy-imperative-mapping.md) | Async SQLAlchemy with imperative mapping | Accepted; amended 2026-10-25 (at the end) |
| [0005](0005-transactional-outbox.md) | A transactional outbox instead of a broker | Accepted |
| [0006](0006-environment-prefix-from-project-name.md) | Environment variable prefix from the project name | Accepted |
| [0007](0007-own-repository-released-to-pypi.md) | Its own repository, released to PyPI | Accepted |
| [0008](0008-multiple-databases-and-routing.md) | Several databases: two kinds, routed by domain | Accepted |
| [0009](0009-sharding-with-hash-buckets.md) | Shard by key over fixed hash buckets | Accepted |
| [0010](0010-postgresql-range-partitioning.md) | Range-partition large tables in PostgreSQL | Accepted |
| [0011](0011-immudb-ledger-for-evidence.md) | Keep evidence in an immudb ledger, verified by the client | Accepted; keys and subjects amended by [0018](0018-evidence-subjects-are-internal-ids.md); amended 2026-10-26 (at the end) |
| [0012](0012-caches-json-values-fail-open.md) | Caches with JSON values that fail open | Accepted |
| [0013](0013-shared-rate-limits-in-redis.md) | Share rate limits through Redis, with GCRA on the server's clock | Accepted; amended 2026-10-25 (at the end) |
| [0014](0014-outbox-as-record-broker-for-dispatch.md) | Keep the outbox as the record; use the broker to dispatch | Accepted |
| [0015](0015-auth-and-content-types.md) | Users, groups, and permissions modelled on Django's | Accepted |
| [0016](0016-authenticating-requests.md) | Authenticate requests with tokens; rules decide who may call routes | Accepted; amended by [0056](0056-reauthorize-open-connections.md), [0058](0058-cache-principals-and-stamp-revocations.md), and [0059](0059-read-principals-in-two-queries-without-the-hash.md) |
| [0017](0017-deny-by-default-and-admin-api.md) | Routes deny by default; staff manage users through an admin API | Accepted; amended by [0056](0056-reauthorize-open-connections.md); its consequence that mounts are not checked is superseded by [0021](0021-check-mounted-applications.md) |
| [0018](0018-evidence-subjects-are-internal-ids.md) | Evidence subjects are internal ids, keyed by a secret digest | Accepted |
| [0019](0019-idempotency-keys-commit-with-changes.md) | Mark idempotency keys committed in the transaction of the changes | Accepted; leases added by [0020](0020-lease-idempotency-keys.md) |
| [0020](0020-lease-idempotency-keys.md) | Lease idempotency keys, and write them only under the holder's token | Accepted; amended 2026-10-21 (at the end) |
| [0021](0021-check-mounted-applications.md) | Check mounted applications and Starlette routes too | Accepted |
| [0022](0022-query-repositories-with-criteria.md) | Query repositories with criteria that fakes evaluate as SQL does | Accepted |
| [0023](0023-keyset-pages-and-typed-cursors.md) | Keyset pages with versioned, typed cursors; filters declared per field | Accepted; amended in place 2026-10-20 |
| [0024](0024-replicas-and-derived-tables.md) | Read from replicas in read-only work; derive tables from domain classes | Accepted |
| [0025](0025-resources-commands-and-expected-versions.md) | Serve aggregates as resources; change them by commands, on the version the client read | Accepted |
| [0026](0026-object-permission-grants.md) | Grant permissions on single objects, beside the objects, within the granter's own | Accepted |
| [0027](0027-batches-and-exports.md) | Run batches as many use cases, atomic on request; stream exports, bounded | Accepted |
| [0028](0028-one-secret-key-signs-and-derives.md) | Sign with one `SECRET_KEY`, and derive a key per purpose from it | Accepted |
| [0029](0029-messages-to-people-leave-through-the-outbox.md) | Send mail, text messages, and push notifications through the outbox | Accepted; amended 2026-10-25 (at the end) |
| [0030](0030-encrypt-totp-secrets-under-a-derived-key.md) | Encrypt TOTP secrets with AES-GCM under a key derived from `SECRET_KEY` | Accepted |
| [0031](0031-operation-ids-from-route-names.md) | Operation ids from route names, for new projects | Accepted |
| [0032](0032-problem-details-as-an-error-format.md) | Offer RFC 9457 problem details as an error format | Accepted |
| [0033](0033-feature-flags-in-the-database.md) | Keep feature flags and dynamic settings in the database, cached whole | Accepted |
| [0034](0034-cron-schedules-by-wall-clock-time.md) | Run cron schedules by wall-clock time, queued ahead of each run | Accepted |
| [0035](0035-task-policies-priorities-chains-in-the-outbox.md) | Keep per-task policies, priorities, chains, and rates in the outbox | Accepted |
| [0036](0036-webhooks-through-the-outbox.md) | Send webhooks through the outbox, signed, to checked addresses only | Accepted |
| [0037](0037-one-clock-frozen-per-context.md) | Read the time from one clock, frozen per context in tests | Accepted |
| [0038](0038-test-databases-of-their-own.md) | Give the tests PostgreSQL databases of their own | Accepted |
| [0039](0039-translate-messages-at-the-boundary.md) | Translate messages at the HTTP boundary, from gettext catalogs compiled in Python | Accepted |
| [0040](0040-store-files-under-random-names.md) | Store files under random names; trust only their content | Accepted |
| [0041](0041-privacy-through-personal-markers.md) | Answer privacy requests from Personal markers, through units of work | Accepted |
| [0042](0042-lease-publications-and-break-the-circuit.md) | Lease publications, and stop publishing on commit while the broker fails | Accepted |
| [0043](0043-the-outbox-database-decides-when-work-is-due.md) | Let the outbox's database decide when work is due, and wake workers | Accepted; amended 2026-10-25 (at the end) |
| [0044](0044-one-concurrency-budget-per-worker-process.md) | Run one concurrency budget per worker process | Accepted |
| [0045](0045-own-background-tasks-per-loop.md) | Own background tasks per event loop, started in clean contexts | Accepted; amended 2026-10-25 (at the end) |
| [0046](0046-one-shutdown-budget-in-phases.md) | Shut down in phases within one budget | Accepted; amended 2026-10-25 (at the end) |
| [0047](0047-seal-from-a-high-water-mark.md) | Seal the audit trail from a high-water mark, with a short late window | Accepted; amended 2026-10-21, 2026-10-25 and 2026-10-26 (at the end) |
| [0048](0048-request-deadlines.md) | Give every request a deadline, answer 503 when it passes, and pass it on | Accepted; amended 2026-10-25 (at the end) |
| [0049](0049-shed-load-past-a-concurrency-cap.md) | Shed load in the application: a cap per event loop, a short queue, and a fast 503 | Accepted; amended 2026-10-21 and 2026-10-25 (at the end) |
| [0050](0050-outgoing-http-client.md) | Call other services through pooled clients that keep to the deadline | Accepted; amended 2026-10-21 (at the end) |
| [0051](0051-logs-and-error-reports-leave-the-loop.md) | Write logs and error reports from threads, dropping what does not fit | Accepted; amended 2026-10-21 (at the end) |
| [0052](0052-keep-the-immudb-sdk-on-threads.md) | Keep immudb's SDK, on threads, rather than an async-native client | Accepted |
| [0053](0053-stay-on-fastapi.md) | Stay on FastAPI and Starlette | Accepted |
| [0054](0054-relationships-raise-instead-of-loading-lazily.md) | Make relationships raise instead of loading lazily | Accepted |
| [0055](0055-bound-live-connections.md) | Bound every live connection: caps, a send queue, a message rate, and clocks | Accepted |
| [0056](0056-reauthorize-open-connections.md) | Re-authorize open connections on a timer, and close them when that fails | Accepted; amended in place 2026-10-21; amended by [0058](0058-cache-principals-and-stamp-revocations.md) |
| [0057](0057-live-committed-events-in-process.md) | Stream committed events to clients in process, filtered per event by an access rule | Accepted; amended in place 2026-10-21 |
| [0058](0058-cache-principals-and-stamp-revocations.md) | Keep principals in each process, and stamp revocations in a shared cache | Accepted |
| [0059](0059-read-principals-in-two-queries-without-the-hash.md) | Read a principal in two queries, build each request's user, and deprecate its password hash | Accepted |
| [0060](0060-flatten-included-routers.md) | Flatten included routers when the application is built | Accepted |
| [0061](0061-read-without-a-transaction-and-ping-once.md) | Read without a transaction where it is safe, and ping a connection once | Accepted; amended in place 2026-10-22 |
| [0062](0062-claim-idempotency-keys-in-one-statement.md) | Claim idempotency keys in one statement, and commit the framework's short writes as they run | Accepted; amended in place 2026-10-22, and 2026-10-25 (at the end) |
| [0063](0063-encode-json-with-pydantic-and-sample-the-access-log.md) | Encode JSON with pydantic where the bytes stay the same, and let projects sample the access log | Accepted |
| [0064](0064-gate-performance-on-counts-not-time.md) | Gate performance in CI on counts, and report time | Accepted |
| [0065](0065-upgrade-fastapi-sqlalchemy-asyncpg-behind-the-ladder.md) | Upgrade FastAPI, SQLAlchemy, and asyncpg behind the ladder, with FastAPI's telemetry off | Accepted; amended 2026-10-25 (at the end) |
| [0066](0066-write-amounts-as-fixed-point-text.md) | Write amounts as fixed-point text | Accepted |
| [0068](0068-count-failed-attempts-and-hold-back-only-guesses.md) | Count failed attempts, and hold back only what can be guessed | Accepted; amended 2026-10-25 (at the end) |
| [0069](0069-namespace-audit-anchors-and-check-the-baseline.md) | Namespace audit anchors, and check the tamper-evident baseline where deployed | Accepted; amended 2026-10-25 (at the end) |
| [0070](0070-name-the-operator-of-data-changing-commands.md) | Name the operator of data-changing commands, and audit what they do | Accepted; amended 2026-10-25 (at the end) |
| [0071](0071-bind-cache-computations-to-their-owner.md) | Detach shared cache computations; bind those that borrow to their owner | Accepted |
| [0072](0072-walk-proxy-headers-from-the-right-and-trust-unix-sockets.md) | Walk proxy headers from the right, trust unix sockets by name, and name the scheme header | Accepted |
| [0075](0075-ranges-to-the-next-major-tested-at-the-top.md) | Declare ranges to the next major, and test the newest releases they allow | Accepted |
| [0076](0076-public-api-is-docs-api-md.md) | The public API is what docs/api.md lists, which is every `__all__` | Accepted |
| [0077](0077-declare-what-apps-require.md) | Declare what apps require, and reach other apps only through their public names | Accepted |
| [0078](0078-deliver-events-in-process-after-commit-or-durably-through-the-outbox.md) | Deliver events in process after commit, or durably through the outbox | Accepted |
| [0079](0079-validate-every-setting-in-one-model-and-each-app-in-its-own.md) | Validate every setting at startup: one model, and each app's own | Accepted |
| [0082](0082-signed-audit-checkpoints.md) | Prune the audit chain below signed checkpoints, in a fixed format | Accepted |

### Decisions still without a record

Taken, and in the code, but not yet written down (DSGN-15); planned for
0.10, beside the changes the audit proposes for each:

- `DomainConfig`'s hooks, `stage` and `committed` among them, called on
  every installed domain (DSGN-03).
- `domainkit.resources` depending on `contrib.auth` and
  `contrib.contenttypes` for its access rules (DSGN-02).
- Module-level singletons (the registry, the settings, the event bus, the
  caches) and test overrides that act on them (DSGN-07).
- No common exception root, and HTTP errors raised from modules below
  `http` (DSGN-06).
- How closers are registered for shutdown (DSGN-13).
- Whether the flags and webhooks admin APIs, built on `contrib.auth.admin`,
  keep to [0025](0025-resources-commands-and-expected-versions.md)'s
  "registry of resources, not a second kit"; amend 0025 or the code.

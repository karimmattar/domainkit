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
| [0007](0007-own-repository-released-to-pypi.md) | Its own repository, released to PyPI | Accepted; amended 2026-10-26 (at the end) |
| [0008](0008-multiple-databases-and-routing.md) | Several databases: two kinds, routed by domain | Accepted |
| [0009](0009-sharding-with-hash-buckets.md) | Shard by key over fixed hash buckets | Accepted |
| [0010](0010-postgresql-range-partitioning.md) | Range-partition large tables in PostgreSQL | Accepted |
| [0011](0011-immudb-ledger-for-evidence.md) | Keep evidence in an immudb ledger, verified by the client | Accepted; keys and subjects amended by [0018](0018-evidence-subjects-are-internal-ids.md); amended 2026-10-26 (at the end) |
| [0012](0012-caches-json-values-fail-open.md) | Caches with JSON values that fail open | Accepted |
| [0013](0013-shared-rate-limits-in-redis.md) | Share rate limits through Redis, with GCRA on the server's clock | Accepted; amended 2026-10-25 (at the end); amended by [0098](0098-share-login-throttles-and-fail-closed-where-deployed.md) |
| [0014](0014-outbox-as-record-broker-for-dispatch.md) | Keep the outbox as the record; use the broker to dispatch | Accepted |
| [0015](0015-auth-and-content-types.md) | Users, groups, and permissions modelled on Django's | Accepted |
| [0016](0016-authenticating-requests.md) | Authenticate requests with tokens; rules decide who may call routes | Accepted; amended by [0056](0056-reauthorize-open-connections.md), [0058](0058-cache-principals-and-stamp-revocations.md), [0059](0059-read-principals-in-two-queries-without-the-hash.md), and [0097](0097-name-the-key-of-every-json-web-token-and-retire-keys-within-a-token-lifetime.md) |
| [0017](0017-deny-by-default-and-admin-api.md) | Routes deny by default; staff manage users through an admin API | Accepted; amended by [0056](0056-reauthorize-open-connections.md) and [0087](0087-count-proven-devices-apart-and-bound-scopes-by-route.md); its consequence that mounts are not checked is superseded by [0021](0021-check-mounted-applications.md); amended 2026-10-26 (at the end) |
| [0018](0018-evidence-subjects-are-internal-ids.md) | Evidence subjects are internal ids, keyed by a secret digest | Accepted |
| [0019](0019-idempotency-keys-commit-with-changes.md) | Mark idempotency keys committed in the transaction of the changes | Accepted; leases added by [0020](0020-lease-idempotency-keys.md); amended by [0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md); amended 2026-10-26 (at the end) by [0096](0096-keep-committed-idempotency-keys-as-tombstones.md) |
| [0020](0020-lease-idempotency-keys.md) | Lease idempotency keys, and write them only under the holder's token | Accepted; amended 2026-10-21 and 2026-10-26 (at the end); amended by [0096](0096-keep-committed-idempotency-keys-as-tombstones.md) |
| [0021](0021-check-mounted-applications.md) | Check mounted applications and Starlette routes too | Accepted |
| [0022](0022-query-repositories-with-criteria.md) | Query repositories with criteria that fakes evaluate as SQL does | Accepted |
| [0023](0023-keyset-pages-and-typed-cursors.md) | Keyset pages with versioned, typed cursors; filters declared per field | Accepted; amended in place 2026-10-20 |
| [0024](0024-replicas-and-derived-tables.md) | Read from replicas in read-only work; derive tables from domain classes | Accepted |
| [0025](0025-resources-commands-and-expected-versions.md) | Serve aggregates as resources; change them by commands, on the version the client read | Accepted; amended 2026-10-26, twice, and 2026-10-28 (at the end) by [0102](0102-hold-if-match-on-the-unit-of-work-that-changes-the-aggregate.md) |
| [0026](0026-object-permission-grants.md) | Grant permissions on single objects, beside the objects, within the granter's own | Accepted |
| [0027](0027-batches-and-exports.md) | Run batches as many use cases, atomic on request; stream exports, bounded | Accepted |
| [0028](0028-one-secret-key-signs-and-derives.md) | Sign with one `SECRET_KEY`, and derive a key per purpose from it | Accepted |
| [0029](0029-messages-to-people-leave-through-the-outbox.md) | Send mail, text messages, and push notifications through the outbox | Accepted; amended 2026-10-25 (at the end) |
| [0030](0030-encrypt-totp-secrets-under-a-derived-key.md) | Encrypt TOTP secrets with AES-GCM under a key derived from `SECRET_KEY` | Accepted |
| [0031](0031-operation-ids-from-route-names.md) | Operation ids from route names, for new projects | Accepted |
| [0032](0032-problem-details-as-an-error-format.md) | Offer RFC 9457 problem details as an error format | Accepted |
| [0033](0033-feature-flags-in-the-database.md) | Keep feature flags and dynamic settings in the database, cached whole | Accepted; amended 2026-10-28 (at the end) |
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
| [0046](0046-one-shutdown-budget-in-phases.md) | Shut down in phases within one budget | Accepted; amended 2026-10-25 and 2026-10-26 (at the end) |
| [0047](0047-seal-from-a-high-water-mark.md) | Seal the audit trail from a high-water mark, with a short late window | Accepted; amended 2026-10-21, 2026-10-25 and 2026-10-26 (at the end) |
| [0048](0048-request-deadlines.md) | Give every request a deadline, answer 503 when it passes, and pass it on | Accepted; amended 2026-10-25, 2026-10-26 and 2026-10-28 (at the end) |
| [0049](0049-shed-load-past-a-concurrency-cap.md) | Shed load in the application: a cap per event loop, a short queue, and a fast 503 | Accepted; amended 2026-10-21 and 2026-10-25 (at the end) |
| [0050](0050-outgoing-http-client.md) | Call other services through pooled clients that keep to the deadline | Accepted; amended 2026-10-21 (at the end) |
| [0051](0051-logs-and-error-reports-leave-the-loop.md) | Write logs and error reports from threads, dropping what does not fit | Accepted; amended 2026-10-21 and 2026-10-26 (at the end) |
| [0052](0052-keep-the-immudb-sdk-on-threads.md) | Keep immudb's SDK, on threads, rather than an async-native client | Accepted |
| [0053](0053-stay-on-fastapi.md) | Stay on FastAPI and Starlette | Accepted |
| [0054](0054-relationships-raise-instead-of-loading-lazily.md) | Make relationships raise instead of loading lazily | Accepted |
| [0055](0055-bound-live-connections.md) | Bound every live connection: caps, a send queue, a message rate, and clocks | Accepted |
| [0056](0056-reauthorize-open-connections.md) | Re-authorize open connections on a timer, and close them when that fails | Accepted; amended in place 2026-10-21; amended by [0058](0058-cache-principals-and-stamp-revocations.md) |
| [0057](0057-live-committed-events-in-process.md) | Stream committed events to clients in process, filtered per event by an access rule | Accepted; amended in place 2026-10-21 |
| [0058](0058-cache-principals-and-stamp-revocations.md) | Keep principals in each process, and stamp revocations in a shared cache | Accepted; amended 2026-10-26 (at the end) |
| [0059](0059-read-principals-in-two-queries-without-the-hash.md) | Read a principal in two queries, build each request's user, and deprecate its password hash | Accepted; amended 2026-10-26 (at the end) |
| [0060](0060-flatten-included-routers.md) | Flatten included routers when the application is built | Accepted; amended 2026-10-26 (at the end) |
| [0061](0061-read-without-a-transaction-and-ping-once.md) | Read without a transaction where it is safe, and ping a connection once | Accepted; amended in place 2026-10-22 |
| [0062](0062-claim-idempotency-keys-in-one-statement.md) | Claim idempotency keys in one statement, and commit the framework's short writes as they run | Accepted; amended in place 2026-10-22, and 2026-10-25 and 2026-10-26 (at the end); amended by [0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md) and [0096](0096-keep-committed-idempotency-keys-as-tombstones.md) |
| [0063](0063-encode-json-with-pydantic-and-sample-the-access-log.md) | Encode JSON with pydantic where the bytes stay the same, and let projects sample the access log | Accepted |
| [0064](0064-gate-performance-on-counts-not-time.md) | Gate performance in CI on counts, and report time | Accepted; amended by [0104](0104-import-what-a-process-uses-on-first-use-and-gate-its-modules.md) |
| [0065](0065-upgrade-fastapi-sqlalchemy-asyncpg-behind-the-ladder.md) | Upgrade FastAPI, SQLAlchemy, and asyncpg behind the ladder, with FastAPI's telemetry off | Accepted; amended 2026-10-25 (at the end) |
| [0066](0066-write-amounts-as-fixed-point-text.md) | Write amounts as fixed-point text | Accepted |
| [0068](0068-count-failed-attempts-and-hold-back-only-guesses.md) | Count failed attempts, and hold back only what can be guessed | Accepted; amended 2026-10-25 (at the end); amended by [0087](0087-count-proven-devices-apart-and-bound-scopes-by-route.md) and [0098](0098-share-login-throttles-and-fail-closed-where-deployed.md) |
| [0069](0069-namespace-audit-anchors-and-check-the-baseline.md) | Namespace audit anchors, and check the tamper-evident baseline where deployed | Accepted; amended 2026-10-25 and 2026-10-26 (at the end) |
| [0070](0070-name-the-operator-of-data-changing-commands.md) | Name the operator of data-changing commands, and audit what they do | Accepted; amended 2026-10-25 and 2026-10-26 (at the end) |
| [0071](0071-bind-cache-computations-to-their-owner.md) | Detach shared cache computations; bind those that borrow to their owner | Accepted |
| [0072](0072-walk-proxy-headers-from-the-right-and-trust-unix-sockets.md) | Walk proxy headers from the right, trust unix sockets by name, and name the scheme header | Accepted |
| [0075](0075-ranges-to-the-next-major-tested-at-the-top.md) | Declare ranges to the next major, and test the newest releases they allow | Accepted |
| [0076](0076-public-api-is-docs-api-md.md) | The public API is what docs/api.md lists, which is every `__all__` | Accepted; amended 2026-10-29 (at the end) by [0114](0114-split-the-reference-and-the-public-api-into-pages-by-area.md) |
| [0077](0077-declare-what-apps-require.md) | Declare what apps require, and reach other apps only through their public names | Accepted |
| [0078](0078-deliver-events-in-process-after-commit-or-durably-through-the-outbox.md) | Deliver events in process after commit, or durably through the outbox | Accepted; amended 2026-10-26 (at the end) |
| [0079](0079-validate-every-setting-in-one-model-and-each-app-in-its-own.md) | Validate every setting at startup: one model, and each app's own | Accepted; amended 2026-10-26 (at the end) |
| [0080](0080-access-rules-are-core-auth-is-one-provider.md) | Access rules are core; auth is one provider | Accepted; amended 2026-10-26 (at the end) |
| [0082](0082-signed-audit-checkpoints.md) | Prune the audit chain below signed checkpoints, in a fixed format | Accepted |
| [0084](0084-one-exception-root-mapped-to-http-at-the-boundary.md) | Root every exception at `DomainkitError`, and map errors to HTTP at the boundary | Accepted |
| [0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md) | Register commit and migration hooks by type, for the domains that have them | Accepted; amended 2026-10-28 (at the end) by [0101](0101-register-every-domain-hook-and-check-it-when-it-registers.md) |
| [0086](0086-secure-defaults-at-the-http-edge.md) | Make the HTTP edge's defaults the safe ones | Accepted; amended 2026-10-26 (at the end) |
| [0087](0087-count-proven-devices-apart-and-bound-scopes-by-route.md) | Count proven devices apart, and bound scoped credentials by route | Accepted; amended 2026-10-26 (at the end) |
| [0089](0089-build-the-docs-site-with-mkdocs-and-generate-the-api-reference-from-api-md.md) | Build the docs site with MkDocs, and generate the API reference from api.md | Accepted; amended 2026-10-29 (at the end) by [0114](0114-split-the-reference-and-the-public-api-into-pages-by-area.md) |
| [0090](0090-module-singletons-overridden-per-test.md) | Keep framework state in module-level singletons, overridden per test | Accepted |
| [0092](0092-retry-units-of-work-choose-isolation-and-lock-in-order.md) | Run units of work again after a lost race, let them choose their isolation, and lock in order | Accepted; amended 2026-10-28 and 2026-10-29 (at the end) |
| [0094](0094-an-inbox-of-handled-messages-written-with-the-handlers-commit.md) | Keep an inbox of handled messages, written with the handler's commit | Accepted |
| [0095](0095-fence-a-runs-commits-with-its-lease-instead-of-a-heartbeat.md) | Fence a run's commits with its lease, instead of a heartbeat | Accepted |
| [0096](0096-keep-committed-idempotency-keys-as-tombstones.md) | Keep committed idempotency keys as tombstones past their TTL | Accepted |
| [0097](0097-name-the-key-of-every-json-web-token-and-retire-keys-within-a-token-lifetime.md) | Name the key of every JSON Web Token, and retire keys within a token's lifetime | Accepted |
| [0098](0098-share-login-throttles-and-fail-closed-where-deployed.md) | Share the attempt throttles, and fail closed, where deployed | Accepted |
| [0099](0099-take-request-ids-from-trusted-proxies-only.md) | Take request ids from trusted proxies only, and drop a caller's own | Accepted |
| [0100](0100-guard-credentials-and-grants-from-loaddata-and-flush.md) | Guard credentials and grants from `loaddata` and `flush`, and load new rows only | Accepted; amended 2026-10-28, twice, and 2026-10-29 (at the end) |
| [0101](0101-register-every-domain-hook-and-check-it-when-it-registers.md) | Register every domain hook, and check it when it registers | Accepted; amended 2026-10-28 and 2026-10-29 (at the end) by [0111](0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md) |
| [0102](0102-hold-if-match-on-the-unit-of-work-that-changes-the-aggregate.md) | Hold `If-Match` on the unit of work that changes the aggregate | Accepted; amended 2026-10-28 and 2026-10-29 (at the end) |
| [0104](0104-import-what-a-process-uses-on-first-use-and-gate-its-modules.md) | Import what a process uses on first use, and gate the modules each rung loads | Accepted |
| [0105](0105-fingerprint-keyed-bodies-as-they-arrive-and-spool-them-past-a-threshold.md) | Fingerprint keyed bodies as they arrive, and spool them past a threshold | Accepted; amended 2026-10-28 (at the end) |
| [0106](0106-mutate-what-a-pull-request-changes.md) | Mutate what a pull request changes, before it merges | Accepted; amended 2026-10-29 (at the end) |
| [0107](0107-make-test-users-through-the-domain-and-sign-in-through-a-protocol.md) | Make test users through the domain, and sign in as them through a protocol | Accepted |
| [0108](0108-keep-the-full-domain-as-the-default-and-give-new-projects-an-image-and-ci.md) | Keep the full domain as `startdomain`'s default; give new projects an image and CI | Accepted; amended 2026-10-28 (at the end) |
| [0111](0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md) | Refuse a removed hook method until 1.0, wherever the domain defines it | Accepted |
| [0112](0112-refuse-the-test-databases-to-a-test-that-did-not-request-them.md) | Refuse the test databases to a test that did not request them | Accepted |
| [0113](0113-reset-module-state-through-public-resets-the-plugin-calls.md) | Reset module state through public resets the plugin calls | Accepted |
| [0114](0114-split-the-reference-and-the-public-api-into-pages-by-area.md) | Split the reference and the public API into pages by area | Accepted |
| [0115](0115-store-money-as-an-exact-amount-and-a-currency-code.md) | Store money as an exact amount and a currency code | Accepted |
| [0116](0116-keep-double-entry-books-locked-in-id-order-and-append-only.md) | Keep double-entry books: post under locks taken in id order, and append only | Accepted |
| [0117](0117-reconcile-statements-through-a-port-with-rules-in-order-and-record-breaks.md) | Reconcile statements through a port, match by rules in order, and record breaks | Accepted |

### Decisions still without a record

None. The five that 0.9.2 listed (DSGN-15) are recorded: the hook
broadcast in [0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md),
the module-level singletons in [0090](0090-module-singletons-overridden-per-test.md),
the exception root in [0084](0084-one-exception-root-mapped-to-http-at-the-boundary.md),
how closers are registered in [0046](0046-one-shutdown-budget-in-phases.md)'s
amendment of 2026-10-26, and the flags and webhooks admin APIs in
[0025](0025-resources-commands-and-expected-versions.md)'s second amendment
of 2026-10-26.

# Architecture decision records

Each record explains one decision that is costly to reverse: its context, what
was decided, and what follows from it. Add a record before such a change, by
copying [template.md](template.md) to the next number. Never rewrite an
accepted record: supersede it with a new one and link both ways.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-django-conventions-for-ddd-services.md) | Django's conventions for domain-driven services | Accepted |
| [0003](0003-enforced-layer-rules.md) | Layer rules enforced by `check` | Accepted |
| [0004](0004-async-sqlalchemy-imperative-mapping.md) | Async SQLAlchemy with imperative mapping | Accepted |
| [0005](0005-transactional-outbox.md) | A transactional outbox instead of a broker | Accepted |
| [0006](0006-environment-prefix-from-project-name.md) | Environment variable prefix from the project name | Accepted |
| [0007](0007-own-repository-released-to-pypi.md) | Its own repository, released to PyPI | Accepted |
| [0008](0008-multiple-databases-and-routing.md) | Several databases: two kinds, routed by domain | Accepted |
| [0009](0009-sharding-with-hash-buckets.md) | Shard by key over fixed hash buckets | Accepted |
| [0010](0010-postgresql-range-partitioning.md) | Range-partition large tables in PostgreSQL | Accepted |
| [0011](0011-immudb-ledger-for-evidence.md) | Keep evidence in an immudb ledger, verified by the client | Accepted |
| [0012](0012-caches-json-values-fail-open.md) | Caches with JSON values that fail open | Accepted |
| [0013](0013-shared-rate-limits-in-redis.md) | Share rate limits through Redis, with GCRA on the server's clock | Accepted |
| [0014](0014-outbox-as-record-broker-for-dispatch.md) | Keep the outbox as the record; use the broker to dispatch | Accepted |
| [0015](0015-auth-and-content-types.md) | Users, groups, and permissions modelled on Django's | Accepted |
| [0016](0016-authenticating-requests.md) | Authenticate requests with tokens; rules decide who may call routes | Accepted |
| [0017](0017-deny-by-default-and-admin-api.md) | Routes deny by default; staff manage users through an admin API | Accepted |
| [0018](0018-evidence-subjects-are-internal-ids.md) | Evidence subjects are internal ids, keyed by a secret digest | Accepted |
| [0019](0019-idempotency-keys-commit-with-changes.md) | Mark idempotency keys committed in the transaction of the changes | Accepted |
| [0020](0020-lease-idempotency-keys.md) | Lease idempotency keys, and write them only under the holder's token | Accepted |
| [0021](0021-check-mounted-applications.md) | Check mounted applications and Starlette routes too | Accepted |
| [0022](0022-query-repositories-with-criteria.md) | Query repositories with criteria that fakes evaluate as SQL does | Accepted |
| [0023](0023-keyset-pages-and-typed-cursors.md) | Keyset pages with versioned, typed cursors; filters declared per field | Accepted |
| [0024](0024-replicas-and-derived-tables.md) | Read from replicas in read-only work; derive tables from domain classes | Accepted |
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

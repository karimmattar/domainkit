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

# Databases, caches, storages, and brokers

A backend is a service that keeps your data or carries your work: a SQL
database, a ledger, a cache, a file storage, a search engine, or a message
broker. This page lists the settings of each backend, with its keys,
defaults, and bounds. The guides explain how to use them: see
[databases.md](../databases.md), [cache.md](../cache.md),
[uploads.md](../uploads.md), and [messaging](../contrib/messaging.md).

On this page you find:

- `DATABASES`, `LEDGERS`, `SHARDS`, `DATABASE_ROUTING`, and
  `MIGRATIONS_DIR`, and how to size connection pools;
- `CACHES`;
- `STORAGES`, `UPLOAD_SCANNER`, `SEARCH`, and `TENANCY`;
- `BROKERS`;
- how a package offers a backend by name.

## A first example

Each backend setting is a dict of entries by alias. `default` is the
entry that code uses when it names none:

```python
from domainkit.conf import Env

env = Env()

DATABASES = {
    "default": {
        "URL": env.str("DATABASE_URL"),  # postgresql+asyncpg://app@db/shop
        "MIGRATION_URL": env.str("DATABASE_OWNER_URL"),
        "SSLMODE": "verify-full",
        "POOL_SIZE": 10,
        "MAX_OVERFLOW": 5,
    }
}
CACHES = {
    "default": {
        "URL": env.str("REDIS_URL"),  # rediss://:password@cache:6380/0
        "KEY_PREFIX": "shop",
        "COMPUTE_TIMEOUT": 30,
    }
}
STORAGES = {
    "default": {
        "BACKEND": "domainkit.storage.FileSystemStorage",
        "OPTIONS": {"location": "/srv/shop/files"},
    }
}
BROKERS = {"default": {"URL": env.str("BROKER_URL"), "PREFIX": "shop"}}
```

Every URL is kept secret: logs, errors, and `repr` hide it.
Percent-encode `/ ? # @ :` in user names and passwords.

## Databases and migrations

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATABASES` | `{}` | SQL databases by alias (below). If any are set, `default` is required. A ledger URL here is refused, with a pointer to `LEDGERS`. |
| `LEDGERS` | `{}` | Ledgers by alias (below): write once, read many, verified. They hold evidence and are never migrated. |
| `SHARDS` | `{}` | Shard groups by name (below): SQL aliases that share a schema, split by a shard key. |
| `DATABASE_ROUTING` | `{}` | Domain label to target: a SQL alias or a shard group. Domains not listed use `default`. Their tables and their units of work both go there. |
| `MIGRATIONS_DIR` | `<BASE_DIR>/migrations` | The revisions of `default`. Every other target keeps its own history in `MIGRATIONS_DIR/<target>`. |

See [databases.md](../databases.md) for routing, sharding, partitioning,
and ledgers.

```python
DATABASE_ROUTING = {"billing": "accounts", "audit": "default"}
```

Each `DATABASES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | An SQLAlchemy async URL (`postgresql+asyncpg://…`, `sqlite+aiosqlite:///…`). Kept secret in logs and reprs. |
| `MIGRATION_URL` | `None` | A schema owner's URL, used only by `migrate` and `makemigrations` (and `dbshell --owner`). The application keeps a role limited to rows. |
| `ECHO` | `False` | Logs every statement. Refused in staging and production. |
| `LOG_PARAMETERS` | `False` | Shows bound values in logs and errors. Refused in staging and production. Without it, these are dropped too: the `DETAIL` that PostgreSQL adds to constraint errors (`Failing row contains (...)`, which repeats the row's values), its `CONTEXT`, and a value the driver or PostgreSQL refuses (`invalid input for query argument $1: <hidden>`). |
| `SSLMODE` | `None` | libpq's names: `disable`, `require`, `verify-ca`, `verify-full`. Staging and production need `verify-full` for network databases. `None` keeps the driver's default. |
| `SSLROOTCERT`, `SSLCERT`, `SSLKEY` | `None` | The CA bundle, and the client certificate and key for mutual TLS. `SSLCERT` and `SSLKEY` go together. |
| `STATEMENT_TIMEOUT` | `30.0` | Seconds; PostgreSQL cancels longer statements. Migrations are exempt. This timeout and the four below are at most 2147483.647 seconds (24.8 days), the longest PostgreSQL takes. A transaction's `statement_timeout` from a longer deadline asks for that much. |
| `LOCK_TIMEOUT` | `10.0` | Seconds a statement waits for a lock (at most 2147483.647). Migrations are exempt. |
| `IDLE_IN_TRANSACTION_TIMEOUT` | `60.0` | Seconds before PostgreSQL ends a session that idles inside a transaction (at most 2147483.647). |
| `CONNECT_TIMEOUT` | `10.0` | Seconds to open a connection (asyncpg's `timeout`; libpq's `connect_timeout`, in whole seconds; at most 2147483.647), migrations included. A request that needs a new connection waits this long at most, after its `POOL_TIMEOUT`. |
| `COMMAND_TIMEOUT` | `STATEMENT_TIMEOUT` plus 5 | Seconds asyncpg waits for one statement before it gives up on it: a network or a server that stopped answering (at most 2147483.647). It is longer than `STATEMENT_TIMEOUT`, so the server ends a slow statement first, with an error that keeps the connection. `None` without `STATEMENT_TIMEOUT`. Migrations are exempt. |
| `PGBOUNCER` | `False` | The database is reached through PgBouncer's transaction pooling: no statement cache, a unique name for every prepared statement, and the timeouts above set in each transaction (`SET LOCAL`), not for the session. PostgreSQL URLs only ([runtime.md](../runtime.md#database-timeouts-and-pgbouncer)). |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_RECYCLE` | SQLAlchemy's | Connection pool sizing ([sizing pools](#sizing-pools)). `POOL_SIZE` is 1 or more; `MAX_OVERFLOW` 0 or more. |
| `POOL_TIMEOUT` | `10.0` for network databases, else SQLAlchemy's | Seconds a request waits for a connection. Past it, the request gets 503 `database_busy`. |
| `POOL_PRE_PING` | `True` | Tests connections before use, so the pool survives a database restart. On asyncpg outside PgBouncer, the test is one round trip, outside a transaction ([databases.md](../databases.md#connection-pools)). |
| `TEST_NAME` | `None` | The PostgreSQL database that `domainkit.testing` creates for the tests, and drops after them; `test_<name>` when unset. It must start with `test_` ([docs/testing.md](../testing.md)). |
| `CONNECT_ARGS` | `{}` | More driver arguments, added to those the options above set. asyncpg's `server_settings` and libpq's `options` are merged with the timeouts. Setting what an option above already sets (TLS, a timeout, PgBouncer's statement cache) is refused when the application is built, however the server setting is spelled (`STATEMENT_TIMEOUT`, `--statement-timeout=`): change that option instead. `timeout`, `command_timeout`, and `connect_timeout` are refused the same way (deprecated in 0.8, refused from 0.9): set `CONNECT_TIMEOUT` and `COMMAND_TIMEOUT`. |
| `REPLICAS` | `()` | Aliases of read replicas of this database; read-only units of work take turns on them ([databases.md](../databases.md#read-replicas), [recipe](../recipes/read-replica.md)). Each is an alias of its own, other than `default`, of one primary, outside every shard group, with no replicas of its own, and never a routing target. |

### Sizing pools

Each event loop of each process has its own pool per alias. A pool holds
up to `POOL_SIZE` + `MAX_OVERFLOW` connections (SQLAlchemy's 5 + 10 when
unset). A loop that reads with `Reads.STATEMENTS` has a second pool of
the same size. The database must then accept this many connections:

```text
replicas × processes per replica × loops per process × (POOL_SIZE + MAX_OVERFLOW) × pools
```

For example, 50 replicas of 4 single-loop workers, with the defaults and
both pools: 50 × 4 × 1 × 15 × 2 = 6,000. That is past PostgreSQL's
default of 100 and the limits of most managed plans. Put PgBouncer in
front (`PGBOUNCER`), or size the pools down.

Each loop also admits `MAX_CONCURRENT_REQUESTS` (1,000) requests. Past
about four requests per connection, a request waits for a connection,
unseen by the shedding queue, for up to `POOL_TIMEOUT`, then fails with
503 `database_busy`. In staging and production, `check` warns when a
network database's pool holds fewer than a quarter of
`MAX_CONCURRENT_REQUESTS` (`deploy.W006`).

The defaults stay as they are. Measured with requests that each hold a
connection for 20 ms against a pool of 15, a burst of 200 is all served
under a cap of 1,000 (in 463 ms at most), where a cap of 60 refuses 40 of
them ([performance.md](../performance.md#010-foundations)).

### Ledgers

Each `LEDGERS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | `immudb://user:password@host:3322/database`, or `memory://` for tests and development. Kept secret. |
| `TLS_CA` | `None` | The CA certificate that signed the immudb server's; setting it turns TLS on. Required in staging and production. |
| `SERVER_SIGNING_KEY` | `None` | The public key of the server's `--signingKey`; every state is then checked. Required in staging and production. |
| `TIMEOUT` | `10.0` | Seconds each ledger call may take. |

```python
LEDGERS = {
    "default": {
        "URL": env.str("LEDGER_URL"),  # immudb://app:secret@immudb:3322/defaultdb
        "TLS_CA": "/etc/immudb/ca.pem",
        "SERVER_SIGNING_KEY": "/etc/immudb/signing.pub",
    }
}
```

### Shards

Each `SHARDS` group:

| Key | Default | Meaning |
| --- | --- | --- |
| `DATABASES` | required | The group's SQL aliases (at least one, not `default`). `makemigrations` compares against the first. |
| `BUCKETS` | `4096` | Hash buckets that keys spread over (1 to 1,048,576). Never change it once the group holds data. |
| `RANGES` | equal ranges | Bucket ranges per alias, both ends included, such as `{"accounts_0": [[0, 2047]], "accounts_1": [[2048, 4095]]}`. They must cover every bucket once. |
| `PINNED` | `{}` | Shard keys (as text) that always go to one alias, such as a large tenant. |

```python
SHARDS = {"accounts": {"DATABASES": ("accounts_0", "accounts_1")}}
DATABASE_ROUTING = {"billing": "accounts"}
```

## Caches

See [cache.md](../cache.md) for how to use caches.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CACHES` | `{"default": {}}` | Caches by alias (below). `default` is required; unless you set it, it is a `memory://` cache. |

Each `CACHES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | `memory://` | `memory://` (this process); `redis://` or `rediss://` (Redis or Valkey, through redis-py; the `redis` extra); `redis+sentinel://` or `redis+cluster://` (a Sentinel group or a Cluster, by several addresses; `rediss+` for TLS; see [cache.md](../cache.md#sentinel-and-cluster)); or `dummy://` (keeps nothing). redis-py's URL options (`?socket_keepalive=true`) apply to one server. Kept secret. |
| `TTL` | `300.0` | Seconds an entry lives unless a call passes `ttl`. `None` keeps entries until they are evicted. |
| `KEY_PREFIX` | the alias | The namespace of every key (`[A-Za-z0-9_.-]`, up to 64). Required for Redis: unique among the services that share a server. |
| `VERSION` | `1` | Part of every key. Raise it to drop every entry at once, as after you change a cached type. |
| `MAX_ENTRIES` | `10000` | `memory://` only: the least recently used entries are evicted past it. |
| `SOCKET_TIMEOUT` | `1.0` | Seconds one Redis command may take. |
| `CONNECT_TIMEOUT` | `1.0` | Seconds to connect, or to wait for a free pooled connection. |
| `RETRIES` | `1` | Quick retries of a command after a connection error or a timeout. |
| `MAX_CONNECTIONS` | `50` | Redis connections per process and event loop. |
| `FAILURE_COOLDOWN` | `5.0` | Seconds a failed cache is skipped, so requests do not each wait for timeouts. `0` tries again at once. |
| `EARLY_RECOMPUTE` | `1.0` | How early `get_or_set` refreshes hot entries before they expire (XFetch's beta). `0` turns it off. |
| `COMPUTE_TIMEOUT` | `None` | Seconds a shared `get_or_set` computation may run (at most an hour). The computation goes on after its callers stop waiting, so a slow factory still fills the cache, within this time; its statements and outgoing calls keep to it. `None` leaves it unbounded, as in 0.9.0. Set it, for example to `REQUEST_TIMEOUT`'s value: `check` warns without it where deployed (`caches.W001`), and the `init` template sets it. Factories that borrow their caller's resources (`borrows=True`) run under their caller's deadline instead ([cache.md](../cache.md#what-a-factory-may-use)). |
| `TLS_CA` | system CAs | The CA bundle that signed the server's certificate (`rediss://` only). |
| `TLS_CERT`, `TLS_KEY` | `None` | The client certificate and key for mutual TLS (`rediss://` only). |
| `READINESS` | `False` | Fails `/ready` while this cache cannot be reached. Leave it off for caches the service can run without, or an outage takes every replica out of rotation. |
| `SENTINEL_PASSWORD` | `None` | `redis+sentinel://` only: the Sentinels' own password, when they ask for one (the URL's password is the primary's). Kept secret. |
| `BACKEND` | `None` | The dotted path, or the name an installed package offers it by ([extension points](extension-points.md#backends-from-an-installed-package)), of a `domainkit.cache.Store` of the project's own. It is made as `Store(config)` once per event loop; the `URL`, of a scheme of its choosing, is its to read ([recipe](../recipes/cache-store.md)). |

```python
CACHES = {
    "default": {"URL": "rediss://:secret@cache:6380/0", "KEY_PREFIX": "shop"},
    "sessions": {
        "URL": "redis+sentinel://sentinel-1:26379,sentinel-2:26379/mymaster",
        "KEY_PREFIX": "shop-sessions",
    },
}
```

## Storages

Where files live; see [uploads.md](../uploads.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `STORAGES` | `{}` | File storages by alias (below), as Django's `STORAGES`. `storages["default"]` is the one that `save_upload` and `serve` use unless told otherwise. |

Each `STORAGES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `domainkit.storage.FileSystemStorage` | The dotted path, or the name an installed package offers it by ([extension points](extension-points.md#backends-from-an-installed-package)), of a `domainkit.storage.Storage` class: `FileSystemStorage`, `MemoryStorage` (tests; refused in staging and production), `domainkit.storage.s3.S3Storage`, `domainkit.storage.gcs.GCSStorage`, `domainkit.storage.azure.AzureBlobStorage` ([uploads.md](../uploads.md#object-storage)), or your own. |
| `OPTIONS` | `{}` | The class's keyword arguments. `FileSystemStorage` takes `location` (an absolute directory), `base_url` (where a web server serves it, if one does), `file_mode` (`0o600`), and `directory_mode` (`0o700`). |

### Scanning, search, and tenancy

| Setting | Default | Meaning |
| --- | --- | --- |
| `UPLOAD_SCANNER` | `None` | The virus scanner that `save_upload` streams every upload through ([uploads.md](../uploads.md#virus-scanning)). `None`: uploads are not scanned. |
| `SEARCH` | `None` | The search engine that `domainkit.search.search_backend()` returns ([search.md](../search.md#search-engines)). `None`: no search engine. |
| `TENANCY` | `None` | Multi-tenancy ([tenancy.md](../tenancy.md)): how a request names its tenant, who may act in it, and which databases keep a schema per tenant. `None`: one tenant, nothing scoped. |

| `UPLOAD_SCANNER` key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `domainkit.storage.scanning.ClamAVScanner` | A `domainkit.storage.scanning.Scanner` class, by dotted path or by name. |
| `OPTIONS` | `{}` | Its keyword arguments. `ClamAVScanner` takes `host`, `port` (3310), and `timeout` (30 seconds). |

| `SEARCH` key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `domainkit.search.MemorySearchBackend` | A `SearchBackend` class: `MemorySearchBackend` (one process; refused in staging and production, `deploy.E007`), `domainkit.search.meilisearch.MeilisearchBackend`, `domainkit.search.opensearch.OpenSearchBackend`, or your own. |
| `OPTIONS` | `{}` | Its keyword arguments: its `url`, and its key or login. |

| `TENANCY` key | Default | Meaning |
| --- | --- | --- |
| `SUBDOMAIN_OF` | `None` | A domain such as `example.com`: `acme.example.com` is served as the tenant `acme`. |
| `HEADER` | `None` | A header that names the tenant (`X-Tenant`), when there is no subdomain. Use it with `MEMBERSHIP`, because any caller may send any value. Not with `SUBDOMAIN_OF`. |
| `RESOLVER` | `None` | The dotted path of a function of the request (async or not) that returns its tenant, or `None`. It replaces `SUBDOMAIN_OF` and `HEADER`. |
| `MEMBERSHIP` | `None` | The dotted path of a function of the user and a tenant id (async or not): whether the user may act in that tenant (403 `not_a_member` if not). |
| `REQUIRED` | `True` | Refuses a request that names no tenant (400 `tenant_not_named`). |
| `EXEMPT_PATHS` | `("/health",)` | Paths that start with one of these may name no tenant. |
| `SCHEMA` | `"tenant_{tenant}"` | A tenant's PostgreSQL schema, on the `SCHEMA_DATABASES`. Lower-case letters and `_` around `{tenant}`. |
| `SCHEMA_DATABASES` | `()` | Database aliases (PostgreSQL) where every table lives in a schema per tenant; their units of work use the current tenant's. `migrate --tenant` migrates each tenant's schema. |

```python
TENANCY = {
    "SUBDOMAIN_OF": "example.com",
    "MEMBERSHIP": "domains.accounts.application.tenants.is_member",
}
```

## Brokers

Message brokers carry the worker's work when `MESSAGING.BROKER` names
one; see [messaging](../contrib/messaging.md#workers-on-rabbitmq). The
outbox stays the record of every message: a broker that loses a message
delays it, and never drops it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `BROKERS` | `{}` | Message brokers by alias (below). |

Each `BROKERS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | One of: `amqps://user:password@host:5671/vhost` or `amqp://...` (RabbitMQ, through aio-pika; the `rabbitmq` extra); `rediss://:password@host:6379/0` or `redis://...` (Redis Streams; the `redis` extra); `nats+tls://user:password@host:4222` or `nats://...` (NATS JetStream; the `nats` extra); `kafka+ssl://host1:9093,host2:9093` or `kafka://...` (Kafka, a user and password signing in with SCRAM-SHA-512; the `kafka` extra); `sqs://<region>`, or `sqs://KEY:SECRET@<region>`, with `?endpoint=` for another endpoint (Amazon SQS; the `cloud` extra); or `memory://` (one process, for tests and development). aiormq's URL options (`?heartbeat=30`) apply to RabbitMQ. Kept secret. |
| `PREFIX` | required but for `memory://` | Starts every exchange, stream, subject, topic, and queue name (`[A-Za-z0-9_.-]`, up to 64): `<PREFIX>.<queue>` (`<PREFIX>-<queue>` on SQS). Unique among the services that share a server. |
| `TLS_CA` | system CAs | The CA bundle that signed the server's certificate (`amqps://`, `rediss://`, `nats+tls://`, `kafka+ssl://` only). |
| `TLS_CERT`, `TLS_KEY` | `None` | The client certificate and key for mutual TLS (the same schemes only). |
| `REDELIVER_AFTER` | `300.0` | Seconds that a message taken and not answered waits before another consumer may take it (Redis, NATS, SQS); more than 0, at most 43,200. RabbitMQ and Kafka hand it on when its consumer goes away. |
| `PARTITIONS` | `6` | Partitions of each topic a Kafka broker creates, which bound the workers that share a queue; 1 to 1,000. |
| `REPLICAS` | `3` | Copies of each partition a Kafka broker creates (1 for one server); 1 to 10. |
| `CONNECT_TIMEOUT` | `10.0` | Seconds to connect. Also the deadline of opening channels, declaring, binding, setting the prefetch, and starting or stopping consumers. |
| `PUBLISH_TIMEOUT` | `10.0` | Seconds to wait for the broker to confirm a published message. While a relay publishes a message, it locks it for this time, `CONNECT_TIMEOUT`, and five seconds. |
| `BACKEND` | `None` | The dotted path, or the name an installed package offers it by ([extension points](extension-points.md#backends-from-an-installed-package)), of a `domainkit.broker.Broker` of the project's own (`broker.pull.PullBroker` for one that is asked for messages). It is made as `Broker(config)` once per event loop; the `URL`, of a scheme of its choosing, is its to read, and `PREFIX` is still required ([recipe](../recipes/broker.md)). |

```python
BROKERS = {"default": {"URL": env.str("BROKER_URL"), "PREFIX": "shop"}}
MESSAGING = {"BROKER": "default"}
```

`python manage.py broker check` connects to each broker, and
`broker declare` declares the worker's queues ([commands](commands.md#broker-check-brokers-or-declare-queues)).

## See also

- [Settings](settings.md): how settings load, and the core settings.
- [Extension points](extension-points.md#backends-from-an-installed-package):
  backends from an installed package.
- [Checks](checks.md): what staging and production refuse in these
  settings.
- [Databases](../databases.md), [caches](../cache.md),
  [uploads](../uploads.md), and [messaging](../contrib/messaging.md).

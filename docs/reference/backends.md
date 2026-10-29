# Databases, caches, storages, and brokers

The settings of the services a project keeps its data and its work in.

## Databases and migrations

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATABASES` | `{}` | SQL databases by alias (below). If any are set, `default` is required. A ledger URL here is refused with a pointer to `LEDGERS`. |
| `LEDGERS` | `{}` | Ledgers by alias (below): write once, read many, verified. They hold evidence and are never migrated. |
| `SHARDS` | `{}` | Shard groups by name (below): SQL aliases that share a schema, split by a shard key. |
| `DATABASE_ROUTING` | `{}` | Domain label to target: a SQL alias or a shard group. Domains not listed use `default`. Their tables and units of work both go there. |
| `MIGRATIONS_DIR` | `<BASE_DIR>/migrations` | Revisions of `default`; every other target keeps its own history in `MIGRATIONS_DIR/<target>`. |

See [databases.md](../databases.md) for routing, sharding, partitioning, and ledgers.

Each `DATABASES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | SQLAlchemy async URL (`postgresql+asyncpg://…`, `sqlite+aiosqlite:///…`). Kept secret in logs and reprs. |
| `MIGRATION_URL` | `None` | A schema-owner URL used only by `migrate` and `makemigrations`; the app keeps a row-level role. |
| `ECHO` | `False` | Log every statement; refused in staging and production. |
| `LOG_PARAMETERS` | `False` | Show bound values in logs and errors; refused in staging and production. Without it, the `DETAIL` PostgreSQL adds to constraint errors (`Failing row contains (...)`, which repeats the row's values) is dropped too, as are its `CONTEXT`, and a value the driver or PostgreSQL refuses (`invalid input for query argument $1: <hidden>`). |
| `SSLMODE` | `None` | libpq names: `disable`, `require`, `verify-ca`, `verify-full`. Staging and production need `verify-full` for network databases. |
| `SSLROOTCERT`, `SSLCERT`, `SSLKEY` | `None` | CA bundle, and client certificate and key for mutual TLS. |
| `STATEMENT_TIMEOUT` | `30.0` | Seconds; PostgreSQL cancels longer statements. Migrations are exempt. This and the four timeouts below are at most 2147483.647 seconds (24.8 days), the longest PostgreSQL takes; a transaction's `statement_timeout` from a longer deadline asks for that much. |
| `LOCK_TIMEOUT` | `10.0` | Seconds a statement waits for a lock (at most 2147483.647). Migrations are exempt. |
| `IDLE_IN_TRANSACTION_TIMEOUT` | `60.0` | Seconds before PostgreSQL ends a session idling inside a transaction (at most 2147483.647). |
| `CONNECT_TIMEOUT` | `10.0` | Seconds to open a connection (asyncpg's `timeout`; libpq's `connect_timeout`, in whole seconds; at most 2147483.647), migrations included. A request that needs a new connection waits this long at most, after its `POOL_TIMEOUT`. |
| `COMMAND_TIMEOUT` | `STATEMENT_TIMEOUT` plus 5 | Seconds asyncpg waits for one statement before giving up on it (a network or server that stopped answering; at most 2147483.647). Longer than `STATEMENT_TIMEOUT`, so the server ends a slow statement first, with an error that keeps the connection. None without `STATEMENT_TIMEOUT`; migrations are exempt. |
| `PGBOUNCER` | `False` | The database is reached through PgBouncer's transaction pooling: no statement cache, a unique name for every prepared statement, and the timeouts above set in each transaction (`SET LOCAL`) rather than for the session. PostgreSQL URLs only ([runtime.md](../runtime.md#database-timeouts-and-pgbouncer)). |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_RECYCLE` | SQLAlchemy's | Connection pool sizing ([sizing pools](#sizing-pools)). |
| `POOL_TIMEOUT` | `10.0` for network databases, else SQLAlchemy's | Seconds a request waits for a connection; past it the request gets 503 `database_busy`. |
| `POOL_PRE_PING` | `True` | Test connections before use, so database restarts are survived. On asyncpg outside PgBouncer the test is one round trip, outside a transaction ([databases.md](../databases.md#connection-pools)). |
| `TEST_NAME` | `None` | The PostgreSQL database `domainkit.testing` creates for the tests, and drops after them; `test_<name>` when unset. It must start with `test_` ([docs/testing.md](../testing.md)). |
| `CONNECT_ARGS` | `{}` | Extra driver arguments, added to those derived from the options above. asyncpg's `server_settings` and libpq's `options` are merged with the timeouts. Setting what the options above already set (TLS, a timeout, PgBouncer's statement cache) is refused when the application is built, however the server setting is spelled (`STATEMENT_TIMEOUT`, `--statement-timeout=`); change that option instead. `timeout`, `command_timeout`, and `connect_timeout` are refused the same way (deprecated in 0.8, refused from 0.9): set `CONNECT_TIMEOUT` and `COMMAND_TIMEOUT`. |
| `REPLICAS` | `()` | Aliases of read replicas of this database; read-only units of work take turns on them ([databases.md](../databases.md#read-replicas)). Each is an alias of its own, other than `default`, of one primary, outside every shard group, with no replicas of its own, and never a routing target. |

### Sizing pools

Each event loop of each process has its own pool per alias, of up to
`POOL_SIZE` + `MAX_OVERFLOW` connections (SQLAlchemy's 5 + 10 when unset),
and a second one of the same size when it reads with `Reads.STATEMENTS`.
The connections a database must accept are then:

```text
replicas × processes per replica × loops per process × (POOL_SIZE + MAX_OVERFLOW) × pools
```

50 replicas of 4 single-loop workers with the defaults and both pools:
50 × 4 × 1 × 15 × 2 = 6,000, past PostgreSQL's default of 100 and most
managed plans' limits; put PgBouncer in front (`PGBOUNCER`), or size the
pools down. Each loop also admits `MAX_CONCURRENT_REQUESTS` (1,000)
requests: past about four per connection, a request waits for one, unseen
by the shedding queue, for up to `POOL_TIMEOUT`, then fails with 503
`database_busy`. `check` warns, in staging and production, when a network
database's pool holds fewer than a quarter of `MAX_CONCURRENT_REQUESTS`.
The defaults stay: measured with requests that each hold a connection for
20 ms against a pool of 15, a burst of 200 is all served under a cap of
1,000 (in 463 ms at most), where a cap of 60 refuses 40 of them
([performance.md](../performance.md#010-foundations)).

Each `LEDGERS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | `immudb://user:password@host:3322/database`, or `memory://` for tests and development. Kept secret. |
| `TLS_CA` | `None` | CA certificate that signed the immudb server's; enables TLS. Required in staging and production. |
| `SERVER_SIGNING_KEY` | `None` | Public key of the server's `--signingKey`; every state is then checked. Required in staging and production. |
| `TIMEOUT` | `10.0` | Seconds each ledger call may take. |

Each `SHARDS` group:

| Key | Default | Meaning |
| --- | --- | --- |
| `DATABASES` | required | The group's SQL aliases (at least one, not `default`); `makemigrations` compares against the first. |
| `BUCKETS` | `4096` | Hash buckets keys spread over (1 to 1,048,576). Never change it once the group holds data. |
| `RANGES` | equal ranges | Inclusive bucket ranges per alias, such as `{"accounts_0": [[0, 2047]], "accounts_1": [[2048, 4095]]}`; they must cover every bucket once. |
| `PINNED` | `{}` | Shard keys (as text) that always go to one alias, such as a large tenant. |

## Caches

See [cache.md](../cache.md) for how to use them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CACHES` | `{"default": {}}` | Caches by alias (below). `default` is required; unless set, it is a `memory://` cache. |

Each `CACHES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | `memory://` | `memory://` (this process), `redis://` or `rediss://` (Redis or Valkey, through redis-py; the `redis` extra), `redis+sentinel://` or `redis+cluster://` (a Sentinel group or a Cluster, by several addresses; `rediss+` for TLS; see [cache.md](../cache.md#sentinel-and-cluster)), or `dummy://` (keeps nothing). redis-py URL options (`?socket_keepalive=true`) apply to one server. Kept secret. |
| `TTL` | `300.0` | Seconds an entry lives unless a call passes `ttl`; `None` keeps entries until evicted. |
| `KEY_PREFIX` | the alias | Namespace of every key (`[A-Za-z0-9_.-]`, up to 64). Required for Redis: unique among the services sharing a server. |
| `VERSION` | `1` | Part of every key. Raise it to drop every entry at once, as after changing a cached type. |
| `MAX_ENTRIES` | `10000` | `memory://` only: least recently used entries are evicted beyond it. |
| `SOCKET_TIMEOUT` | `1.0` | Seconds one Redis command may take. |
| `CONNECT_TIMEOUT` | `1.0` | Seconds to connect, or to wait for a free pooled connection. |
| `RETRIES` | `1` | Quick retries of a command after a connection error or timeout. |
| `MAX_CONNECTIONS` | `50` | Redis connections per process and event loop. |
| `FAILURE_COOLDOWN` | `5.0` | Seconds a failed cache is skipped, so requests do not each wait for timeouts. `0` retries at once. |
| `EARLY_RECOMPUTE` | `1.0` | How early `get_or_set` refreshes hot entries before they expire (XFetch's beta); `0` turns it off. |
| `COMPUTE_TIMEOUT` | `None` | Seconds a shared `get_or_set` computation may run (at most an hour). It goes on after its callers stop waiting, so a slow factory still fills the cache, within this; its statements and outgoing calls keep to it. `None` leaves it unbounded, as in 0.9.0; setting it is recommended, for example to `REQUEST_TIMEOUT`'s value; `check` warns without it where deployed (`caches.W001`), and the `init` template sets it. Factories that borrow their caller's resources (`borrows=True`) run under their caller's deadline instead ([cache.md](../cache.md#what-a-factory-may-use)). |
| `TLS_CA` | system CAs | CA bundle that signed the server's certificate (`rediss://` only). |
| `TLS_CERT`, `TLS_KEY` | `None` | Client certificate and key for mutual TLS (`rediss://` only). |
| `READINESS` | `False` | Fail `/ready` while this cache is unreachable. Leave it off for caches the service can run without, or an outage takes every replica out of rotation. |
| `SENTINEL_PASSWORD` | `None` | `redis+sentinel://` only: the Sentinels' own password, when they ask for one (the URL's is the primary's). Kept secret. |

## Storages

Where files live; see [uploads.md](../uploads.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `STORAGES` | `{}` | File storages by alias (below), as Django's `STORAGES`. `storages["default"]` is the one `save_upload` and `serve` use unless told otherwise. |

Each `STORAGES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `domainkit.storage.FileSystemStorage` | Dotted path to a `domainkit.storage.Storage` class: `FileSystemStorage`, `MemoryStorage` (tests; refused in staging and production), or your own. |
| `OPTIONS` | `{}` | The class's keyword arguments. `FileSystemStorage` takes `location` (an absolute directory), `base_url` (where a web server serves it, if one does), `file_mode` (`0o600`), and `directory_mode` (`0o700`). |

## Brokers

Message brokers carry the worker's work when `MESSAGING.BROKER` names one;
see [contrib.md](../contrib/messaging.md#workers-on-rabbitmq).

| Setting | Default | Meaning |
| --- | --- | --- |
| `BROKERS` | `{}` | Message brokers by alias (below). |

Each `BROKERS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | `amqps://user:password@host:5671/vhost` or `amqp://...` (RabbitMQ, through aio-pika; the `rabbitmq` extra), or `memory://` (one process, for tests and development). aiormq URL options (`?heartbeat=30`) apply. Kept secret. |
| `PREFIX` | required for RabbitMQ | Starts every exchange and queue name (`[A-Za-z0-9_.-]`, up to 64): `<PREFIX>.work`, `<PREFIX>.<queue>`. Unique among the services sharing a virtual host. |
| `TLS_CA` | system CAs | CA bundle that signed the server's certificate (`amqps://` only). |
| `TLS_CERT`, `TLS_KEY` | `None` | Client certificate and key for mutual TLS (`amqps://` only). |
| `CONNECT_TIMEOUT` | `10.0` | Seconds to connect; also the deadline of opening channels, declaring, binding, setting the prefetch, and starting or stopping consumers. |
| `PUBLISH_TIMEOUT` | `10.0` | Seconds to wait for the broker to confirm a published message. Relays lock a message for this, `CONNECT_TIMEOUT`, and five seconds while they publish it. |

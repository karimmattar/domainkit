# Public API: domains, data, and resources

The domain building blocks (`domainkit.ddd`), the database layer
(`domainkit.db`), resources, ledgers, caches, storages, and brokers. Part
of the [public API](../api.md), under its compatibility policy.

## `domainkit.broker`

Message brokers: durable work queues for the worker (RabbitMQ, or in memory).

- `Broker`: Durable work queues.
- `BrokerError`: Base class of broker errors.
- `BrokerHandler`: Create each configured broker on first use, per event loop.
- `BrokerRefused`: The broker refused a message (no queue took it, or it said no).
- `BrokerUnavailable`: The broker could not be reached, or did not confirm in time.
- `Delivery`: One message handed to a consumer; `ack` or `requeue` it exactly once.
- `Handler`: A consumer of deliveries: `async def handle(delivery) -> None`.
- `MemoryBroker`: Queues of this process: one consumer gets each message, as in RabbitMQ.
- `Subscription`: Deliveries flowing from queues to a handler.
- `brokers`: The process-wide brokers: `brokers["default"]`.

## `domainkit.cache`

Caches: in process memory, or in Redis or Valkey shared by every replica.

- `DEFAULT_TTL`: Use the cache's `TTL` setting.
- `TTL`: How long an entry lives: seconds or a `timedelta`; None keeps it.
- `Cache`: One configured cache; get it from `domainkit.cache.caches`.
- `CacheError`: Base class of cache errors.
- `CacheHandler`: Create each configured cache on first use, per event loop.
- `CacheUnavailable`: The cache could not be reached in time, or is skipped after a failure.
- `CachedFunction`: An async function whose results are cached by its arguments.
- `DummyStore`: Keep nothing: every read misses.
- `MemoryStore`: Entries in this process, evicted least recently used first.
- `Store`: Bytes under full keys; `ttl` is in milliseconds, None keeps the entry.
- `cache_page`: Cache what an async GET endpoint returns, for `ttl`, in `cache`.
- `cached`: Cache an async function's results, keyed by its arguments.
- `caches`: The process-wide caches: `caches["default"]`.
- `invalidate_on`: Delete the entries `keys(event)` names after each `event_type` event.

## `domainkit.db`

SQLAlchemy integration: connections, ORM metadata, repositories, migrations.

- `ConnectionHandler`: Lazily created engines and session factories per alias and event loop.
- `EnumValue`: An `Enum` stored by its members' values, so order follows them.
- `ExactDecimal`: Fixed-point amounts that come back exactly as stored, also on SQLite.
- `Isolation`: The isolation level of a unit of work's transaction (`isolation=`).
- `Reads`: How a read-only session reads (`SqlAlchemyUnitOfWork(reads=...)`).
- `SqlAlchemyRepository`: Store one imperatively mapped aggregate type; set `model` in subclasses.
- `SqlAlchemyUnitOfWork`: Open a session on enter, commit on `commit()`, close on exit.
- `UTCDateTime`: Timezone-aware UTC timestamps, also on SQLite (which has no time zones).
- `ValueObjectType`: A single-field value object, stored as its field (`ProductName.value`).
- `child`: A relationship to the one child entity an aggregate owns, or None.
- `children`: A relationship to the child entities an aggregate owns (`list[Line]`).
- `connections`: Process-wide connection handler.
- `create_engine`: Build a pooled engine; bound values stay out of logs and errors by default.
- `lock_key`: The 64-bit key `lock(name)` takes on PostgreSQL, signed, as `bigint`.
- `map_imperatively`: Map `class_` to `table` once; later calls are ignored.
- `mapper_registry`: Generalized registry for mapping classes.
- `metadata`: Every table of every domain; the target of `makemigrations`.
- `register_column_type`: Store fields of `cls` (and its subclasses) in `column_type`.
- `table_for`: The table that stores `cls`: one column per field (see the module).

## `domainkit.db.hooks`

The SQL commit and migration hooks a domain registers (`domainkit.apps.hooks`).

- `on_committed`: Follow up after a SQL unit of work committed: `(uow)`.
- `on_migrated`: Fill a database `migrate` upgraded: `(owner_connection, alias)`.
- `on_stage`: Add rows to a committing SQL unit of work: `(uow, events, tasks)`.
- `on_stage_transaction`: Join a transaction committed outside a unit of work: `(connection, alias)`.
- `on_transaction_committed`: Follow up after such a transaction committed: `(alias)`.

## `domainkit.db.models`

Installed models: the mapped classes of installed domains, and their `Meta`.

- `DEFAULT_PERMISSIONS`: Actions that get a permission for every model, unless `Meta` says.
- `InstalledModel`: A mapped class of an installed domain, and what it declares.
- `ModelOptions`: What a mapped class declares about itself (Django's `Meta`).
- `installed_models`: Every class mapped onto a table that an installed domain holds now.
- `model_key`: The `(domain, model)` of a mapped class.
- `model_options`: Read `cls.Meta`: `verbose_name`, `default_permissions`, `permissions`.

## `domainkit.db.partitioning`

Range partitioning of large PostgreSQL tables, managed by the framework.

- `Interval`: The length of one partition.
- `partition_by_range`: Declare `table` range-partitioned by the timestamp `column`.

## `domainkit.db.query`

Compile domain queries (`domainkit.ddd.query`) to SQLAlchemy expressions.

- `register_compiler`: Compile criteria of `kind` (a `Criteria` of your own) with `compiler`.

## `domainkit.db.routing`

Where each domain's data lives: routing targets, aliases, and shards.

- `bucket_for`: The bucket of `key` among `buckets`; stable across processes and hosts.
- `for_each_shard`: Run `function(alias)` on every database of `group` at once.
- `router`: The process-wide router; it reads settings on every call.

## `domainkit.ddd`

Domain-driven design building blocks ("seedwork").

- `UNSET`: A field left out of a partial change (`PatchModel`, `UpdateX` commands).
- `AggregateRoot`: Entry point to a consistency boundary; records events as it changes.
- `BusinessRuleViolation`: The request is well-formed but breaks an invariant of the model.
- `Command`: Request to change state, named in the imperative.
- `ConcurrentUpdate`: Someone else changed the aggregate first; reload it and try again.
- `Conflict`: The operation clashes with the current state, such as a duplicate.
- `Counted`: `Annotated` metadata: the audit trail keeps how many items, not which.
- `Criteria`: A condition on aggregates; combine them with `&`, `|`, and `~`.
- `DeferredTask`: Background work queued by `UnitOfWork.defer`.
- `Digits`: `Annotated` metadata: `precision` digits, `scale` of them decimal.
- `DomainError`: A business rule refused the operation; the message is safe to show.
- `DomainEvent`: Something that happened in the domain, named in the past tense.
- `DuplicateValue`: A value that must be unique is taken (a unique constraint refused it).
- `Entity`: Object with a lifecycle, equal to another only when the ids match.
- `EventBus`: Deliver events to handlers subscribed to their class or a base class.
- `EventHandler`: An event handler: `async def handle(event) -> None`.
- `F`: A field of an aggregate, by name, to build conditions and orderings.
- `HandlerTimedOut`: An in-process handler ran past its timeout and was cut off.
- `InMemoryRepository`: Dictionary-backed repository for unit tests and prototypes.
- `InvalidQuery`: A query names a field, lookup, or value its aggregate cannot take.
- `LockNotAvailable`: Another transaction holds the row's lock, and the caller would not wait.
- `MaxLength`: `Annotated` metadata: text of at most `length` characters.
- `NotFound`: The requested aggregate does not exist.
- `Ordering`: Sort by one field of the aggregate itself.
- `Page`: Up to `limit` items and whether more follow the last one.
- `PermissionDenied`: The actor may not perform the operation.
- `Personal`: `Annotated` metadata: the field holds personal data about someone.
- `PreconditionFailed`: The caller expected another version of the aggregate than the stored one.
- `Query`: Request for data that changes nothing.
- `ReadOnlyError`: A read-only unit of work was asked to commit.
- `Reference`: The id of another aggregate, which is referenced by id only (`Reference[XId]`).
- `Repository`: Load and store aggregates of one type; subclasses implement `_` methods.
- `Retry`: How `UnitOfWork.run` runs its work again when the transaction lost a race.
- `RowLock`: Lock the rows a query returns until the transaction ends (`FOR UPDATE`).
- `RowLocks`: Row locks for in-memory repositories, held until the unit of work ends.
- `Selection`: Which aggregates to load, in which order, and how many.
- `Sensitive`: A type whose values no schema shows or takes, such as a password hash.
- `SoftDeletable`: Mixin for aggregates that are marked deleted rather than removed.
- `Specification`: A named business rule about aggregates, for Python and for queries.
- `UnitOfWork`: Transaction boundary; subclasses expose repositories as attributes.
- `UnknownField`: A query names a field the class does not have.
- `Unset`: The type of `UNSET`: a field the request left out, as opposed to None.
- `ValueObject`: Immutable value defined only by its attributes; equal values are equal.
- `event_bus`: Process-wide bus used by units of work unless they are given another.
- `fields_about`: What `values` hold about `subject`, and about nobody else.

## `domainkit.ddd.preconditions`

Expected versions: change an aggregate only if it is still as the caller saw it.

- `Expectation`: The version an aggregate must have when a use case loads it.
- `expect_version`: Expect aggregate `id_` of `model` at one of `versions` in the block.

## `domainkit.ddd.query`

Queries over aggregates: criteria, ordering, and specifications.

- `Criteria`: A condition on aggregates; combine them with `&`, `|`, and `~`.
- `F`: A field of an aggregate, by name, to build conditions and orderings.
- `Ordering`: Sort by one field of the aggregate itself.
- `RowLock`: Lock the rows a query returns until the transaction ends (`FOR UPDATE`).
- `Selection`: Which aggregates to load, in which order, and how many.
- `Specification`: A named business rule about aggregates, for Python and for queries.

## `domainkit.ledger`

Ledgers: write-once, read-many stores whose reads and writes are verified.

- `Ledger`: An append-only key-value store with verified reads and writes.
- `LedgerChanged`: `append(..., unchanged_since=...)` found a later revision of the key.
- `LedgerEntry`: One verified revision of a key.
- `LedgerError`: Base class of ledger errors.
- `LedgerHandler`: Create each configured ledger on first use and keep it for the process.
- `LedgerKeyExists`: `create` found the key already written.
- `LedgerKeyNotFound`: The key, or the requested revision of it, does not exist.
- `LedgerRolledBack`: The ledger shows an older state than one a client verified: refused.
- `LedgerTampered`: A proof or a server signature did not verify: do not trust the ledger.
- `LedgerUnavailable`: The ledger could not be reached in time.
- `LedgerWrite`: Where a verified write landed.
- `MemoryLedger`: Revisions in a dictionary, numbered by a transaction counter.
- `ledgers`: The process-wide ledgers: `ledgers["evidence"]`.

## `domainkit.ledger.trust`

The ledger state a client trusts, kept where a restart does not lose it.

- `LedgerRolledBack`: The ledger shows an older state than one a client verified: refused.
- `MemoryStateStore`: States in a dictionary: this process only (tests, and the default).
- `StateStore`: Where ledgers keep the newest state they verified, by ledger.
- `TrustedState`: A ledger state a client verified: where the ledger was, and its digest.
- `registered_states`: The store an installed app registered (`use_states`); None if none.
- `use_states`: Keep ledgers' trusted states in `store`, while these domains are installed.

## `domainkit.resources`

REST resources over aggregates: routes, schemas, conditional writes, policies.

- `ALL_FIELDS`: `ResourceRouter(writable=ALL_FIELDS)`: `crud=True` writes every plain field.
- `MERGE_PATCH`: RFC 7396's media type; `PATCH` routes take it as well as JSON.
- `UNSET`: A field left out of a partial change (`PatchModel`, `UpdateX` commands).
- `Actions`: Rules that depend on a route's action (`view`, `add`, ...).
- `BatchAnswer`: One result per item, in order, and how many failed.
- `BatchError`: Why one item failed, as a request of its own would have been told.
- `BatchResult`: What became of one item.
- `DomainSchema`: A read schema `schema_for` made; `from_domain` fills it from an object.
- `ExportTooLarge`: `422`: an export would hold more rows than the resource allows.
- `FieldNotEditable`: A partial change sets a field the caller may not change.
- `FieldPolicy`: Who sees and who changes each field of a resource (see the module).
- `HasModelPermission`: Users granted `<domain>.<action>_<model>`, the model resolved per request.
- `ModelActions`: Model permissions by each route's action, after any `rules` given.
- `NotAcceptable`: `406`: the request accepts none of the media types offered.
- `Operation`: One route of a resource, as its rules and a generic client see it.
- `PatchModel`: A partial change: each field defaults to `UNSET`; unknown ones are refused.
- `PreconditionRequired`: The resource changes only with `If-Match`, and the request sent none.
- `RecordChanged`: Fields of an aggregate were changed through a generic route.
- `RecordCreated`: An aggregate was created through a generic route.
- `RecordDeleted`: An aggregate was deleted through a generic route.
- `ResourceRouter`: The routes of one aggregate type (see the module).
- `Unset`: The type of `UNSET`: a field the request left out, as opposed to None.
- `entity_tag`: The weak tag of one aggregate at `version`, sent as `representation`.
- `expected_versions`: The versions an `If-Match` value accepts; None for `*` (any).
- `schema_for`: A pydantic schema of `model`'s fields (see the module).
- `to_domain`: A request's `value` as `field` holds it: value objects built, enums found.

## `domainkit.storage`

Files: a storage port, on the file system or in memory.

- `FileMissing`: No file has the name (a malformed name names none): 404 over HTTP.
- `FileSystemStorage`: Files under `location`, an absolute directory created on first save.
- `FileTooLarge`: The content went over the size limit while it was saved; nothing was kept.
- `MemoryStorage`: Files in this process's memory; gone when it ends.
- `Storage`: Files under names the storage chooses: save, open, delete, check, size.
- `StorageError`: A storage refused an operation.
- `StorageHandler`: Make each configured storage on first use; one per alias and process.
- `StoredFile`: A file open for reading; close it, or use it in `async with`.
- `check_name`: Return `name` if a storage could hold a file under it.
- `new_name`: A new random name, in `directory`, ending in `extension` (`.png`).
- `storages`: The process-wide storages: `storages["default"]`.

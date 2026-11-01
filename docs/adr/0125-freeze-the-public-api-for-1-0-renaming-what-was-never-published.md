# 0125. Freeze the public API for 1.0, renaming what was never published

- Status: Accepted
- Date: 2026-11-01
- Amends: [0076](0076-public-api-is-docs-api-md.md), [0079](0079-validate-every-setting-in-one-model-and-each-app-in-its-own.md), [0090](0090-module-singletons-overridden-per-test.md), [0111](0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md)

## Context

1.0 freezes the public API ([0076](0076-public-api-is-docs-api-md.md)): every
name an `__all__` lists, the settings and their defaults, the commands, the
check ids, the problem codes, and the contrib tables. After it, a removal
waits for 2.0. The 0.15 design audit (DSGN-16 to DSGN-33) and the other
areas' freeze notes listed what should not be frozen as it is: names that
read wrong or collide, internals that were public by accident, two
defaults every generated project overrode, and two questions left open
since 0.9.0 (DSGN-07, DSGN-08).

The last release published was 0.12; 0.13 to 0.15 were merged but never
tagged. Of the 1,093 public names at 0.15, 290 were never published:
renaming those costs nobody anything now, and a major release later. The
others, and every setting and command 0.12 had, are in projects already.

## Decision

- **Never published (0.13 to 0.15): renamed outright**, no alias. The hook
  points `accounting.hooks.on_post_entry` and `on_stage_entry` (were
  `on_posting`, `on_booking`) and `apps.hooks.on_retain_subject` (was
  `on_erase_check`); accounting's sources `CAPTURE_SOURCE` and
  `REVERSAL_SOURCE`, and approvals' actions `POST_ENTRY`, `CAPTURE_HOLD`,
  `REVERSE_ENTRY`, and `GRANT_SUPERUSER` (each named for the action it is,
  so the two `CAPTURES` no longer mean two things); `AccountNotOpen`,
  `PeriodNotOpen` (no longer one `Is` from the events `AccountClosed`,
  `PeriodClosed`), `EntryNotReversible`, `PolicyRemoved`;
  `limits.check_movements`; `ACCOUNTING.MAX_ATTEMPTS`; `AUTH.PRESET`.
  `approvals.on_guard` is no longer public: `guard()` registers.
- **Published in 0.12: renamed with the old name kept, warning, until
  2.0.** `apps.hooks.on_ready` (was `on_check`, which beside
  `on_check_application` read as `manage.py check`'s),
  `WebhooksSettings`, `DomainConfig.retention`, `Credentials.login` (was
  `session`, which held a JWT login's family), `MESSAGING.DRAIN_TIMEOUT`
  (was `SHUTDOWN_TIMEOUT`, the name of the process's whole budget too),
  `domainkit.db.dbclock` (was public as `contrib.messaging.dbclock`), and
  the `db.models` names `contrib.contenttypes` repeated. Old names answer
  through `deprecation.renamed`/`moved`, a model validator, or a property,
  each with `DomainkitDeprecationWarning`. `conf.UNSET` and `ddd.UNSET`
  are one object.
- **No longer public**, still importable unchanged until 2.0, without a
  warning: internals that no page told anyone to use. `http.routing`,
  which rests on FastAPI's private `_IncludedRouter`, and
  `contrib.webhooks.guard` lose their `__all__`; `BuiltJSONResponse`
  (it writes non-JSON values unchecked), `Worker`'s mechanics (`claim`,
  `relay`, `relay_ids`, `take_over`, `process`, `schedule_periodic`,
  `relay_promoted`, now `_`-prefixed), the device-proof helpers, the key
  ring's and AWS signer's helpers, the checkpoint helpers, the layer
  checker's tables, `db.create_engine` (it shadowed SQLAlchemy's name)
  and `db.mapper_registry`, `http.CURSOR_PATTERN`,
  `conf.describe_errors`/`unwrap_settings`, `storage.check_name`/`new_name`,
  `lifecycle.shutdown_timeout`, `debug.statement_shape`, and
  `http.middleware.request_path` leave their `__all__`.
- **Made public**: `domainkit.http.health.router`, which every generated
  project mounts (DSGN-16); `conf.HttpClientConfig`, `DebugToolbarConfig`,
  and `ArchitectureConfig`, which a `SETTINGS_CLASS` needs; and the
  `keys` extra, for encrypted fields and their key services (INFRA-19).
- **Defaults flipped**: `OPENAPI_OPERATION_IDS = "route_name"`, which every
  generated project set, and `ERROR_FORMAT = "problem+json"`, a superset
  of the old body (`detail`, `code`, `request_id`, `errors` stay) that
  gateways and API guidelines expect. The old values stay valid.
- **Decided and kept**:
  - Settings stay flat (DSGN-08 closed): Django's names where Django has
    them, prefixes for families; a dict group only for named backends and
    for an optional subsystem with its own model. The framework's group
    models keep the `*Config` suffix, the apps' `*Settings`.
  - No application container in 1.x (DSGN-07 closed): the module
    singletons of [0090](0090-module-singletons-overridden-per-test.md)
    stay. The FastAPI subclass keeps the name `Application`; a container,
    if 2.0 wants one, takes another.
  - `apps.E002` refuses a removed hook method until 2.0, not 1.0: 1.0 is
    the first published release without those methods, and without the
    refusal a 0.12 project's `erase_subject` override would silently never
    run.
  - `@task`'s default name stays `module.qualname`; `messaging.W001` warns.
  - `encode_cursor`/`decode_cursor` stay, the plain form contrib apps use
    themselves; `HttpClient(guard=False)` stays the default, as a client
    calls services the project configured (a key service, its own
    services on a private network), while webhooks pass `guard=True`.
  - `refusal()` is documented as cheap and pure, as it may be called more
    than once a request; `AnyOf` and `Not` answer 403 whatever their rules
    name.
  - `ENTRY_APPROVERS` stays `accounting.add_entry`: clerks who may post
    check each other, and a project that wants approvers apart names a
    permission of its own.
  - Shard keys are placed by their text, frozen as it is: a `UUID` and its
    canonical (lower-case, hyphenated) text land together, other forms
    do not; projects shard on typed ids. `lock_key` is frozen as it is.
  - `UnitOfWork.rollback()` stays public.

## Consequences

- A 0.12 project upgrades to 1.0 with warnings, not errors, for every
  rename of a published name, and an upgrade note for each; the
  unpublished renames meet no one.
- 2.0 removes the aliases, `apps.E002`, and the names that left `__all__`.
- The names left in `__all__` are what 1.0 promises; `test_api` holds
  the pages to them, and now holds the generated project to them too.

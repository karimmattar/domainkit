# 0085. Register commit and migration hooks by type, for the domains that have them

- Status: Accepted
- Date: 2026-10-26
- Amends: [0019](0019-idempotency-keys-commit-with-changes.md), [0062](0062-claim-idempotency-keys-in-one-statement.md)

## Context

Until 0.10, `DomainConfig` carried every hook a domain could have: `ready`,
`startup`, `shutdown`, `check`, `stage`, `committed`, `stage_transaction`,
`transaction_committed`, `migrated`, `seed`, `check_application`,
`deployment_warnings`, `holds_table`, and `databases`, with `settings_class`,
`requires`, and `RETENTION` beside them. The framework called a hook on
every installed domain, whether it had overridden it or not: every SQL
commit called `stage` and `committed` on each domain, 26 coroutine calls
with the full contrib set, of which eight did anything. No record described
the broadcast (DSGN-15); [0019](0019-idempotency-keys-commit-with-changes.md)
and [0062](0062-claim-idempotency-keys-in-one-statement.md) use it to commit
idempotency keys with the changes.

`stage` was typed on the abstract `UnitOfWork`, but only
`SqlAlchemyUnitOfWork` calls it, so each app that used it cast the unit of
work, or looked for a `session` attribute (DSGN-03 of the 0.9.0 design
audit). Django's `AppConfig` has one hook, `ready`, and signals for the
rest; Litestar's plugins implement small protocols.

## Decision

- `domainkit.apps.hooks.HookPoint` is one purpose domains may join. A
  domain registers a function on it from `ready()`
  (`on_stage(self.stage_rows)`); iterating the point gives each installed
  domain's `Hook(domain, call)`, in `INSTALLED_DOMAINS` order, each
  domain's in the order it registered them. The list is worked out once
  per registry change, so a commit reads it in one lookup.
- `domainkit.db.hooks` declares the SQL points, typed on what they join:
  `on_stage(uow, events, tasks)` and `on_committed(uow)` on
  `SqlAlchemyUnitOfWork`; `on_stage_transaction(connection, alias)` and
  `on_transaction_committed(alias)` for a transaction the framework
  commits outside a unit of work; `on_migrated(owner_connection, alias)`.
  The unit of work, `messaging.enqueue`, and `migrate` call the hooks, and
  only the domains that registered.
- Registrations belong to the loaded domains: `registry.clear()` and the
  next `populate` forget them, and the domains register again.
- A point may name the `DomainConfig` method it replaces. A configuration
  that overrides `stage`, `committed`, `stage_transaction`,
  `transaction_committed`, or `migrated` is still called through it, after
  its registered hooks: method-style hooks keep working in 0.10, without a
  warning.
- The contrib apps register theirs; none casts the unit of work.
- Another app adds a point of its own the same way, with the method it
  replaces if it had one: an erasure point in `contrib.privacy`, for
  example, `HookPoint("erase", method="erase_subject")`, iterated with
  `hook.domain` to record what each domain did.

## Consequences

- An empty commit with the 13 domains of the full contrib set takes 226
  Python calls instead of 248 on 0.9.2, and stays at 226 with ten more
  project domains (268 on 0.9.2): a domain without hooks costs a commit
  nothing.
- Third-party apps join a commit without subclassing a hook they do not
  use, and type checkers see the SQL unit of work.
- `DomainConfig` keeps its other hooks: `seed`, `holds_table`, `databases`,
  `RETENTION`, `check_application` and `deployment_warnings` have no
  registered replacement yet, so the method-style hooks are not deprecated
  in 0.10. 0.11 moves the rest (the checks to `domainkit.checks`, retention
  to a privacy point) and then deprecates the methods together.
- `db/uow.py` still names `domainkit.contrib.messaging` to refuse durable
  work nobody runs; a point the outbox registers on would replace it.

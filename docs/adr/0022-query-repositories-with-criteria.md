# 0022. Query repositories with criteria that fakes evaluate as SQL does

- Status: Accepted
- Date: 2026-10-19

## Context

Repositories could only `add`, `get`, `find`, and `remove`. Every listing
added a hand-written `_page` to the port, the SQL adapter, and the
in-memory fake, and the fake had to copy the SQL adapter's ordering by
hand (the template's `_sort_key`); nothing checked that they agreed. A
use case that passed its tests against the fake could order, or filter,
differently in production. Locking, savepoints, soft delete, and bulk
changes had no port either: adapters reached for the SQLAlchemy session.

The options were:

- Expose SQLAlchemy `select()` through repositories. The domain would
  import infrastructure, and fakes could not evaluate it.
- Django's keyword lookups (`filter(status="open", total__gte=10)`).
  Familiar, but `OR` and `NOT` need a second mechanism (`Q`), and keyword
  names are checked late.
- Specifications as Python predicates only. They cannot become SQL, so
  every listing would load everything.
- A small expression tree in the domain (`F("total") >= 10`, `&`, `|`,
  `~`) with two interpreters: Python for fakes, a compiler for SQL.

## Decision

- `domainkit.ddd.query` holds the tree: conditions with Django's lookup
  names, `And`, `Or`, `Not`, orderings, `Query` (criteria, ordering,
  limit, offset or a keyset position, a row lock, `include_deleted`), and
  `Specification`, a named rule with `to_criteria()` and
  `is_satisfied_by()`. It imports only the standard library and
  `domainkit.ddd`.
- The semantics are fixed, so both interpreters can follow them:
  two-valued logic (a condition on a missing value is false, and `~`
  negates exactly; in SQL a condition on a nullable column carries
  `IS NOT NULL`), missing values sort last ascending and first descending
  (`NULLS LAST`/`NULLS FIRST` everywhere), every result is ordered totally
  by the query's ordering and then the id, and text lookups match
  characters, never patterns.
- Names and values are checked against the aggregate's dataclass fields
  and type hints (`domainkit.ddd.fields`) before any SQL: unknown names
  fail, values must fit their field's type, enums compare by value, and a
  single-field value object by what it wraps. The SQL compiler resolves
  names only to mapped columns, and dotted names only through mapped
  relationships (`EXISTS`). Values are always bound parameters.
- `filter`, `first`, `count`, `exists`, `paginate`, `add_all`,
  `remove_all`, `restore`, and `get_for_update` are on the port, with
  default implementations, so existing repository subclasses keep working.
- Set-based `update_rows` and `delete_rows` exist only on the SQL adapter,
  named for rows rather than aggregates. They run no aggregate method,
  check no invariant, and record no event; `update_rows` still bumps the
  version. Keeping them off the port keeps the domain from depending on
  them, and a fake from having to imitate them.
- Soft delete is a mixin (`SoftDeletable`); the base repository scopes
  every read to live aggregates unless asked, so both interpreters do.
- Row locks are `get_for_update` and `RowLock` (`nowait`, `skip_locked`):
  `SELECT ... FOR UPDATE OF` on PostgreSQL, nothing on SQLite, and
  `RowLocks` in memory, held until the unit of work commits or rolls back.
- `UnitOfWork.savepoint()` rewinds what the repositories tracked, the
  events recorded, and the tasks deferred inside it; the SQL unit of work
  also rolls back to a `SAVEPOINT` and reads changed aggregates again.

## Consequences

Fakes now answer queries as the database does, and hypothesis property
tests check it on SQLite and PostgreSQL. Two things stay the database's
own: case folding outside ASCII (SQLite's `lower()` folds only ASCII) and
the order of text (the column's collation on PostgreSQL); the docs say
how to align them. Queries load whole aggregates: there are no
projections, joins across aggregates, or aggregations beyond `count`.
Orderings use the aggregate's own fields. Keyset criteria are expanded
into `OR` terms, which PostgreSQL serves with an index on the ordering;
`contains` uses `LIKE` on PostgreSQL (a trigram index helps) and scans on
SQLite. The bulk escape hatch is easy to misuse for business changes; the
name, docstring, and docs say what it skips.

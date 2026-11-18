# 0185. Bound keyset pages, index the user lookups, and read fewer grants

- Status: Accepted
- Date: 2026-11-18

## Context

The audit of 2.8 (`PERF.md`) measured four reads that grow with a table,
where the benchmark ladder, on tables of a few rows, sees none of them.

- **Keyset pages (A28-PERF-01).** `following()` wrote what comes after a
  position as `a > $1 OR (a = $1 AND id > $2)`. No index can start a range
  scan with that condition: PostgreSQL read the index from its first row
  and filtered every row before the position. SQLite read every row and
  sorted them. On 300,000 rows, a page in the middle took 13 to 23 ms on
  PostgreSQL, a page near the start 0.03 ms.
- **User lookups (A28-PERF-02).** Logins, signups, and the checks for a
  taken address or username compare `lower(email)` and `lower(username)`,
  and the admin API lists users by the address, then the id. No index
  serves them: each read the whole users table, 30 to 60 ms at 300,000
  users.
- **Object grants (A28-PERF-05).** Every admin change, and every request
  of an impersonation token (`may_impersonate`), read every grant of the
  user changed, and then every grant of the staff member, loaded as
  aggregates: 140 ms of CPU on the event loop for a user of 10,000
  grants, even when a superuser made the change.
- **Exports (A28-PERF-08).** An export counted every match before it
  compared the count with `max_export` (10,000).

Options weighed:

1. **Keyset bound.**
   - A row comparison, `(a, id) > ($1, $2)`, gives PostgreSQL and SQLite
     the tightest start, ties included. It holds only where every field
     sorts one way and none may be missing (a missing value sorts last,
     a row comparison with NULL is unknown). It needs a criteria node of
     its own, compiled per dialect, evaluated in memory, and known to the
     statement cache (`shape_of`).
   - An inclusive bound on the first field, ahead of the alternatives:
     `a >= $1 AND (a > $1 OR (a = $1 AND id > $2))`. Every database can
     start an index scan with it ([Use The Index, Luke: fetch next
     page](https://use-the-index-luke.com/sql/partial-results/fetch-next-page)
     shows this form for databases without row values, and notes that
     optimizers do not factor it out of the `OR` themselves). It works
     for mixed directions, money, and the in-memory repository, since it
     is plain criteria.
2. **Where the user lookups' indexes live.**
   - `Index` objects on the table, made by a revision. PostgreSQL works.
     SQLite does not: SQLAlchemy cannot reflect an index on an expression
     there, and Alembic's autogenerate skips one with a warning
     ("autogenerate skipping metadata-specified expression-based index
     ... can't reflect these indexes"). A revision written on SQLite,
     the template's development database, then lacks the indexes, and
     once one exists every reflection warns ("Skipped unsupported
     reflection of expression-based index"): `makemigrations`, and
     `take_snapshot` in a project's tests.
   - A generated `lower()` column with a plain index: a new column on
     every user table, mapped or excluded, and filled by a data
     migration.
   - `migrate` makes them, on PostgreSQL, after the revisions
     (`on_migrated`), and revisions leave them alone
     (`unmanaged_indexes`). The audit trail's history indexes work so
     (PERF-29).
3. **Object grants.**
   - Answer the bound in SQL, with an anti-join and `LIMIT 1`, as the
     audit proposed. The refusal says how many grants the staff member
     lacks, which a `LIMIT 1` cannot count, and a grant held through two
     backends would count twice.
   - Keep grants per request. Each request reads them once already, so
     an impersonation token gains nothing.
   - Read only the grants that count: none for a superuser, who holds
     every one; for staff, none of a permission they hold on the whole
     model, left out in the query; and the staff member's own only when
     some remain.
4. **The export's count.** A count that stops at `max_export + 1`, or a
   read of `max_export + 1` ids. The second sorts and sends them all.

## Decision

- **Keyset pages start at the position.** `following()` puts an
  inclusive bound on the first field ahead of the alternatives: `>=`
  ascending, `<=` descending, a money field's currency. It selects no
  row more or fewer. Where no one range holds what follows, it is left
  out: a missing first value, and a present one ascending on a field that
  may be missing, since missing values sort last and follow it. An
  ordering of the id alone keeps its one range. The row comparison is
  not taken: it would serve only orderings of one direction with no
  missing value, and the bound already starts the scan at the position.
- **`migrate` makes the user lookups' indexes on PostgreSQL.** Auth's
  `on_migrated` hook makes, on the user table, a project's own too, where
  missing (`tables.lookup_indexes`):
  - `ix_<table>_<EMAIL_FIELD>_lower` and `ix_<table>_<USERNAME_FIELD>_lower`,
    on `lower()` of each, where the column holds text (a `String`, or a
    `TypeDecorator` over one) and is not encrypted: `lower()` of bytes
    does not exist, and would fail the whole hook;
  - `ix_<table>_<field>_id`, on the field the admin API lists users by
    (`listing_field`: the first of `AUTH.IDENTIFIERS` not encrypted, else
    the `USERNAME_FIELD`), then the id, where the table has an `id` and
    that column is not unique (a unique one has an index of its own).

  It reads the table's index names from the catalog first, and runs
  `CREATE INDEX IF NOT EXISTS` only for a missing one: the statement
  locks the table against writes before it looks for the index, so a
  `migrate` with nothing to make would otherwise wait behind every open
  write, and hold the writes after it. The names are the table's
  `unmanaged_index_names`, matched exactly, so a revision never drops one
  and still sees a project's index whose name starts the same way
  (`unmanaged_indexes` stays a list of prefixes, for the audit trail's).
  SQLite gets none. The queries are left as they were:
  each compares `lower(column)` with a value, so the planner uses the
  index on that expression ([PostgreSQL: indexes on
  expressions](https://www.postgresql.org/docs/16/indexes-expressional.html)).
- **The admin bound reads the grants that count.** `may_change` and a
  group's change read no grant for a superuser, or while a backend does
  not list its grants (only superusers pass then). For staff they ask
  `object_grants(holder, beyond=<the keys they hold on models>)`, and
  `may_hold` reads the staff member's grants only if some remain. A
  backend leaves those grants out itself in
  `BaseBackend.object_grants_beyond(holder, permissions)`, whose default
  filters what `object_grants` answers. It is asked only of a class that
  defines both methods, or neither, so a subclass that overrides
  `object_grants` alone keeps every grant it adds.
  `ObjectPermissionBackend` leaves them out in SQL, and reads the grants'
  columns (`values`) instead of loading them. The refusal, its count, and
  what `object_grants(holder)` answers are unchanged.
- **Counts stop where told.** `Repository.count(..., at_most=n)` answers
  at most `n`. SQL counts a subquery with `LIMIT n`; a repository of
  one's own counts all and stops (`_count_at_most`, whose default takes
  the smaller of `_count` and `n`), and so does a `SqlAlchemyRepository`
  whose subclass overrides `_count`. An export counts at most
  `max_export + 1`; a repository whose `count` keeps 2.8's signature is
  asked as before, and counts every match.
- Flags' `GET /settings` reads the stored values with one `get_many`
  (A28-PERF-07), not one `find` per declared setting.

## Consequences

Measured on PostgreSQL 16 (`EXPLAIN ANALYZE`, best of 5, 300,000 rows):
a middle page by `name, id` took 13.5 ms and now 0.03 ms; by
`-made_at, id`, 14.5 and 0.05 ms; by `-stock, id` (missing values),
20.2 and 0.10 ms. A login by address took 45 ms and now 0.01 ms; the
taken-address check 47 and 0.01 ms; a free username 53 and 0.01 ms; the
admin's first page of users 33 and 0.01 ms, and its middle page 26 and
0.02 ms. An admin change of a user of 10,000 grants costs 0.5 ms of CPU
by a superuser (138 before) and 6 ms by staff who hold the permission on
the model (117 before); a refusal still reads the grants that count,
49 ms (104 before). An impersonated request costs 11 to 17 ms of CPU
(131 to 133 before). Refusing an export of 1,000,000 rows took 35 to
57 ms and now 1 to 7 ms. `GET /settings` with 100 settings made 100
statements in 76 ms, and now one in 9 ms.

What still reads more than a page:

- A page ordered ascending by a field that may be missing reads the
  index from its start, as before: the default user's `username` is one.
  A page within a large tie group of the first field reads the group up
  to the position (a third of 300,000 rows: 22.7 ms before, 7.9 after).
  A row comparison would serve the second where every field sorts one
  way; it can be added later behind the same `following()`. A money
  field is bounded by its currency alone: in a project of one currency,
  a page deep in an ordering by money reads from the start.
- SQLite has no index on `lower()`: logins there still read the whole
  table. SQLite serves development and tests.
- An allowed export still counts every match when fewer than
  `max_export` match: the subquery must read them all to know.

To watch:

- Building an index holds writes to the users table while it runs
  ([PostgreSQL: CREATE
  INDEX](https://www.postgresql.org/docs/16/sql-createindex.html)). On a
  large table, make them first with `CREATE INDEX CONCURRENTLY` under the
  same names; `migrate` then finds them in the catalog, builds none, and
  takes no lock on the table. A concurrent
  build that fails leaves an invalid index of that name, which `migrate`
  takes as made: drop it and build it again.
- A change of `EMAIL_FIELD`, `USERNAME_FIELD`, or the first of
  `AUTH.IDENTIFIERS` makes the new indexes.
  The old ones are no longer the table's unmanaged indexes, so the next
  revision written on PostgreSQL drops them, a drop that fails on a
  database that never had them (SQLite): drop them by hand instead, and
  take the drop out of the revision.
- A backend that overrides `object_grants_beyond` must leave out exactly
  the grants of the permissions given, or the admin API's bound
  (AUTH-14) misses grants.

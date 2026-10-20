# 0026. Grant permissions on single objects, beside the objects, within the granter's own

- Status: Accepted
- Date: 2026-10-20

## Context

Model permissions say what a user may do to every product; many APIs
need "Ada may edit this product", and lists that show each caller only
what they were given. Backends could already answer about objects
(`has_perm(user, perm, obj)`), but nothing stored such grants, and a
backend cannot filter a list: asking it about each row of a page breaks
counts and cursors, and leaks that hidden rows exist.

django-guardian stores grants by content type and object id as text, and
filters querysets by joining the grants. Our ids are UUIDs, integers, or
text, stored in their own column types, and domains may live in
different databases. Granting is also an escalation path: staff who can
hand out a permission they lack can hand it to themselves.

## Decision

- `domainkit.contrib.objectpermissions` stores grants as an aggregate,
  `ObjectPermission`: content type, permission key, the object's id, and
  one user or one group. Granting and revoking record events, so the audit
  trail shows who gave what to whom.
- The id goes in the column of its type (`object_uuid`, `object_int`,
  `object_text`), so the database compares it with the object's id column
  as it is: no casts, which differ by dialect (a UUID is 32 hex digits on
  SQLite and hyphenated text on PostgreSQL).
- Grants live in the database of their objects. The table is created in
  every hosting database, as the outbox and the audit trail are, and the
  services open their unit of work on the objects' routing target, so
  filtering is one query.
- `VisibleTo(permission, user, groups)` is a `Criteria`. The SQL compiler
  takes compilers for criteria of other modules (`register_compiler`),
  and compiles it to `EXISTS` on the grants, correlated with the row's id;
  in memory it reads the grants it is given. `visible_to` returns no
  criteria for users who hold the permission on the model, so they see
  everything; given the request, it first asks the credentials' scopes,
  as `HasPermissions` does, and matches nothing when they leave the
  permission out.
- Grants go with their object. A listener on every mapper's
  `after_delete` deletes the grants about a row the ORM deletes, in the
  same transaction, while the app is installed. A database foreign key
  cannot do it (the grants table refers to rows of every table), and a
  domain event would arrive after the commit, too late for ids the
  database reuses.
- Ceilings, as in the auth admin: granting or revoking a permission needs
  the permission itself (on the model or on that object) and the model
  permission to manage grants. No one grants to themselves, which would
  turn a permission held through a group into one of their own.

## Consequences

Lists filtered by grants are exact, in pages and counts alike, and cost
one indexed `EXISTS`. User and group ids are not foreign keys, since auth
may be in another database: a deleted user's grants stay, unused. Objects
of sharded domains are not supported yet, and a grant is about one object,
not about a query. Tests with fake repositories pass the grants to
`visible_scope` themselves. Bulk deletes (`delete_rows`) bypass the ORM
and leave the grants of their rows: projects that delete objects in bulk
delete the grants too, in the same transaction. A security review found
that scoped credentials saw what their user may, and that grants
outlived their objects; both have regression tests.

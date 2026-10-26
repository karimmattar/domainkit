# 0025. Serve aggregates as resources; change them by commands, on the version the client read

- Status: Accepted; amended 2026-10-26, twice (at the end)
- Date: 2026-10-20

## Context

Every listing and read was written by hand: the generated domain's routes
(about 120 lines for one aggregate), and the auth admin, which had become
a resource router of its own (cursor pages, search, a `PATCH` allowlist,
grant ceilings). Three copies of the OpenAPI error entries existed. No
answer carried an `ETag`, and though aggregates had a `version`, no
request could send it back, so a client saving a stale copy silently
overwrote the change someone else had stored meanwhile.

DRF answers with `ModelViewSet`: generic list, retrieve, create, update,
and destroy over a model, permissions by HTTP method (a custom `POST`
action needs `add`), and no conditional requests. We want the reads to be
as cheap to declare, but the writes to stay use cases that the domain
decides on.

For `If-Match`, the version check must happen in the transaction that
changes the aggregate: a check in a transaction of its own leaves a window
in which a third change is overwritten. The options were a field on every
command (`expected_version`), which each service must remember to pass to
its repository, or an expectation the repository checks by itself.

## Decision

- `domainkit.resources.ResourceRouter`, an `APIRouter`, declares one
  aggregate's list (`FilterSet`, `Pagination`) and read routes, and
  `command_route` adds routes that build a command from the body and the
  path and await `service(command, uow)`. Generic writes (`crud=True`)
  are opt-in, for reference data, and record `RecordCreated`,
  `RecordChanged`, and `RecordDeleted`. They keep to the field policy (a
  create as a partial change does; a field hidden from the caller is not
  theirs to set) and to the caller's scope: the written aggregate is read
  again, in the same transaction, with the scope, and a write it would
  leave is refused before the commit.
- Rules are per route action: `ModelActions(Model)` asks for the model
  permission of the route's action (`view`, `add`, `change`, `delete`),
  not of its method. A resource without a rule declares none, and the
  generated endpoints live outside the framework's own modules, so the
  deny-by-default check lists them. Each route keeps its own rules (the
  resource's for its action, then its `rule=`), and all of them are asked
  about the aggregate when one looks at objects; a query answering
  several leaves out those refused.
- An aggregate's `ETag` is weak, `W/"<version>-<digest>"`, where the
  digest is of the representation sent: callers who see different fields
  get different tags. `If-None-Match` is compared weakly, by the same
  function `@cache_page` uses, so the two schemes agree.
- `If-Match` is compared by the version a tag carries (RFC 9110 asks for
  strong comparison; our tags are weak because representations vary by
  caller, but the version they carry is what a write is conditional on).
  The route holds the expected versions in a context variable
  (`domainkit.ddd.preconditions`) around the service, and the base
  `Repository` checks an aggregate the first time `find` or `filter`
  hands it out: a stale one is `PreconditionFailed`, 412. Optimistic
  locking still guards the commit. A service that never loaded the
  aggregate fails the request rather than succeed unchecked: the unit of
  work refuses to commit while an expected aggregate is not loaded, so
  nothing is written. Resources may require `If-Match` (428 without).
- Partial changes are `PatchModel`s whose fields default to `UNSET`, so
  absent and null differ (RFC 7396), and commands take `UNSET` for what
  was not sent.
- Queries get routes too: `query_route` builds a CQRS `Query` from the
  query string and answers with what its handler returns, an aggregate
  or a list of them, each checked again against the caller's scope and
  the parent, since a handler is free to ignore them.
- The generic admin (`domainkit.contrib.admin`) is a registry of
  resources, not a second kit: its actions are commands, its `/meta`
  is computed per caller from the same rules the routes check, and it
  refuses auth's models, whose own admin keeps rules (grant ceilings,
  the last superuser) that generic commands could not: users of any user
  model, groups, and permissions, by class, and labels that name another
  model than the class's. `schema_for` never shows a password hash.
- The generators (`startaggregate`, `addcommand`, `addquery`,
  `addevent`) write onto this kit and edit existing files only where
  their syntax tree shows what the generators made: every edit is planned
  in memory first, and one that does not fit refuses the whole run.

## Consequences

Services need nothing to honour `If-Match`, and none can forget it; the
price is an implicit check in the repository, documented with it. The
first load decides, so a use case may change and reload the aggregate.
Representations are rendered by the resource, not by FastAPI's response
model, which still documents them. The auth admin's listings and reads
moved onto the kit (and gained tags) without other changes to their
answers; the webhooks admin keeps its listing, whose answer is a bare
array, not a page. Clients that build tags from a body's `version` get
the same checks as those that echo the `ETag`.

The kit asks more of generic writes than a command route does: a write
that leaves the caller's scope is read back before the commit, one query
more, and a create may not set a field the policy guards. A security
review found these gaps (route rules not asked about objects, scope and
policy not held by generic writes, a commit before the `If-Match` check);
each has a regression test.

Because the generated code is small and regular, the generators add to
it without a templating language of their own. Hand-edited files stay
safe: an edit that cannot find its place is refused, never guessed.

## Amendment (2026-10-26): route kinds, and one unit of work for a scoped command

`ResourceRouter` had grown into one module of 1,793 lines whose routes
read their values from a string-keyed dict (DSGN-01 of the 0.9.0 design
audit). In 0.10 `domainkit.resources.routing` is a package: the router
is a facade over one class per kind of route (`listing`, `reading`,
`commands`, `batching`, `queries`, `crud`), each with `declare()`, which
describes the route (`RouteSpec`) and adds it with
`declaring.add_route`, and `handle(context)`, which answers a request from
a frozen, typed `RouteContext`. What the kinds share is `reach` (scope,
parent, object rules, `If-Match`) and `presenting` (schemas, tags,
links). The constructor, every `*_route` signature, and every answer and
OpenAPI document are unchanged.

- **The route-kind extension point.** A new kind of route (a `PUT` that
  replaces, a bulk read) is a module of that shape and a method on the
  facade; nothing else changes. The kinds, `RouteSpec` and `RouteContext`
  are internal for 0.10: a project adds routes of its own with `fetch()`,
  as before, and the shape becomes public once a kind outside the
  framework needs it.
- **One unit of work for a scoped command.** A command or a query on one
  aggregate whose resource has a scope or a parent, and no rule that looks
  at objects, reads the aggregate within reach and runs the service in
  the same unit of work, lent open (`reach.Joined`): the service's enter
  and exit do nothing, its commit and rollback are the unit of work's, and
  the route closes it. One transaction, one connection, and the service
  finds the aggregate the check read: the `resource-command` rung went from
  2 transactions and 4,546 calls to 1 and 3,766. Where a rule looks at the
  aggregate, the check keeps a unit of work of its own, closed before the
  rule runs: a rule may query (object permission grants), and a request
  must not wait for a second connection while it holds one.

## Amendment (2026-10-26, 0.10): admin APIs that are not resources

"The generic admin is a registry of resources, not a second kit" holds for
aggregates served as resources. The flags and webhooks admin APIs are
routers of their own, guarded by `contrib.auth.admin.staff`, the same
staff rules (`IsUnscoped`, staff or superuser, the model permission of
each action) that the auth admin and the generic admin's model actions
check. They stay so, and this record says so, rather than the code
changing: of their 16 routes, 7 serve what the generic admin cannot
(dynamic settings declared in code rather than stored as aggregates, 3;
delivery records and attempts read as rows, 2; a secret shown once when it
is rotated; a replay that queues work), and porting the other 9 (flags and
subscriptions) would split each app's API between two kits for no rule
gained. The rule stands for
new apps: what is an aggregate with commands is registered with the
generic admin; a router of its own needs a reason of that kind.

# 0025. Serve aggregates as resources; change them by commands, on the version the client read

- Status: Accepted
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
  `RecordChanged`, and `RecordDeleted`.
- Rules are per route action: `ModelActions(Model)` asks for the model
  permission of the route's action (`view`, `add`, `change`, `delete`),
  not of its method. A resource without a rule declares none, and the
  generated endpoints live outside the framework's own modules, so the
  deny-by-default check lists them.
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
  aggregate fails the request rather than succeed unchecked. Resources
  may require `If-Match` (428 without).
- Partial changes are `PatchModel`s whose fields default to `UNSET`, so
  absent and null differ (RFC 7396), and commands take `UNSET` for what
  was not sent.

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

# 0102. Hold `If-Match` on the unit of work that changes the aggregate

- Status: Accepted
- Date: 2026-10-28
- Amends: [0025](0025-resources-commands-and-expected-versions.md)

## Context

[0025](0025-resources-commands-and-expected-versions.md) checks `If-Match`
in the repository, the first time it hands the aggregate out, from an
expectation the route held in a context variable around the service
(`expect_version`). Every repository and every unit of work of the
context was held to it (DSGN-14 of the 0.9.0 design audit). A unit of work
the service opened for something else refused to commit until the
aggregate was loaded, as this PoC on 0.11.1 shows: the service writes an
audit record first, in a unit of work of its own, then changes the
aggregate.

```text
ambient: the unrelated unit of work was refused: The service did not load
Gadget 740aef48-... through its repository before committing, so If-Match
could not be checked; nothing was written.
```

Two places could take the expectation explicitly:

- **The repository call.** The service passes it where it loads:
  `await uow.orders.get(command.order_id, expect=command.versions)`. The
  route has to put the versions in the command, and every service has to
  pass them on. The generic routes (`command_route`, `crud`, batches) run
  a project's own commands and services: they cannot add a field to the
  command, and a service that forgets the argument is not checked. 0025
  turned this down for the same reason.
- **The unit of work.** The route holds the expectation on the unit of
  work it hands the service, and that unit of work's repositories check:

  ```python
  with uow.expect(Order, order_id, expected_versions(if_match)) as expected:
      await services.cancel_order(CancelOrder(order_id=order_id), uow)
  ```

  The service needs nothing, as before, and reads the same; the route
  says which unit of work is held. The PoC above, with it:

  ```text
  explicit: committed; checked = True
  ```

## Decision

- `UnitOfWork.expect(model, id_, versions)` is a context manager that
  yields the `Expectation` (now public, `domainkit.ddd.preconditions`).
  For the block, the unit of work lends its expectations to its
  repositories (when it is entered, and when `expect` is called, so a
  unit of work that makes its repositories on entering is covered), and
  they check each aggregate they load, as before; `commit` refuses while
  one is not loaded. Units of work it did not lend to, and repositories
  used on their own, are not held to it.
- The resource routes (commands, batches, and the generic writes) use it,
  on the unit of work of the request; the lent forms (`Joined`,
  `Enlisted`) pass it on to the unit of work they lend.
- `expect_version` keeps working in 0.12, for the whole context as
  before, and warns `DomainkitDeprecationWarning`; 0.13 removes it and the
  context variable.

## Consequences

- A service may open other units of work inside a checked request, to
  record or read something first, without being refused.
- A service that writes the aggregate through another unit of work than
  the one it was given is not held to `If-Match`: the route still fails
  the request (the expectation was never checked), but that write stands,
  where 0.11 refused it. The route hands the service the unit of work to
  change aggregates through; `docs/resources.md` says so.
- A unit of work with no expectation does what it did: entering it lends
  nothing.
- Repositories still check, in the base `Repository`'s `find` and
  `filter`; a repository that overrides them must call `check_expected`
  with its `_expected`, as it had to call it before.

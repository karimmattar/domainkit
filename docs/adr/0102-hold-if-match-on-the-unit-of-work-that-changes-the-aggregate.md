# 0102. Hold `If-Match` on the unit of work that changes the aggregate

- Status: Accepted; amended 2026-10-28 and 2026-10-29 (at the end)
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

## Amendment (2026-10-28, 0.12): each transaction checks again

The security review of 0.12 found that an expectation, checked once, did
not hold the unit of work's later transactions (SEC-12a-6, in code of
[0025](0025-resources-commands-and-expected-versions.md) that this record
kept). A service that read in one `async with uow:` block, called out, and
wrote in the next loaded the aggregate afresh, at a version another client
stored meanwhile, unchecked, and wrote over it: the lost update `If-Match`
refuses. `Joined` gives every block after the first a transaction of its
own, and a plain command route hands the service the unit of work itself.

- Entering the unit of work renews its expectations: the first load of
  the aggregate in each transaction is checked again against the
  client's versions, and later loads in it are not (the use case's own
  changes move the version on).
- After a commit, the version this unit of work's own commit gave the
  aggregate is accepted too: a later block may load what the service
  wrote, but not what another client wrote.

The other way, comparing each later load with the version the first one
saw, would refuse the service's own committed change, or need the same
record of what it committed. `checked` still means "loaded at least once",
which the route asks after the service; the deprecated `expect_version`
is unchanged.

## Amendment (2026-10-29, 0.13): `expect_version` is gone

0.13 removes `expect_version` and the context variable it set, which
every repository load and every commit read. `UnitOfWork.expect` is the
only way to hold an expectation; a load and a commit check only their
unit of work's own. Importing the name fails as for any missing name
(`ImportError: cannot import name 'expect_version'`). The 0.12 amendment
above stands: each transaction of the unit of work checks the aggregate
again (SEC-12a-6), and its test is unchanged.

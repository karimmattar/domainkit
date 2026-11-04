# How-to recipes

A recipe solves one task, from a new project to tests that pass. Each
one has the same parts: the problem, the solution in steps, and how it
works. Use a recipe when you have one goal; use the guides when you want
to understand a whole part of domainkit.

!!! note
    The tests run every command and every code change of each recipe, as
    written and in order (`domainkit_tests/test_recipes.py`), as they do
    for the tutorial. If a step fails for you, that is a bug. Please
    report it.

## Money and data

- [A payment that runs again when it loses a race](recipes/payments.md):
  you get a payment between two wallets that locks both rows in id order,
  and runs again, whole, when the database reports a conflict.
- [Work that commits once per message](recipes/exactly-once.md): you get
  a task marked `@inbox` whose database work commits once, and a test
  that runs its message twice.
- [Reading from a replica](recipes/read-replica.md): you get a resource
  whose reads go to a read replica, while its writes still go to the
  main database.

## Security and access

- [Replacing the key that signs JSON Web Tokens](recipes/jwt-keys.md):
  you get a new signing key, rolled out with `PREVIOUS_KEYS`, so that no
  client is logged out.
- [Running behind a proxy](recipes/behind-a-proxy.md): you get callers'
  real addresses and request ids, taken from your proxies only
  (`TRUSTED_PROXIES`).
- [Service accounts with API keys](recipes/api-keys.md): you get an
  authentication class of your own that logs in another service by its
  `X-API-Key` header, with the keys kept only as digests.
- [A permission rule of your own](recipes/permission-rules.md): you get
  a `BasePermission` that asks whether the caller verified their email
  address, on a route of your project.

## Your own backends

- [An SMS provider of your own](recipes/sms-backend.md): you get an
  `SmsBackend` for a provider's JSON API, and a test with its calls
  replaced by stubs.
- [A cache store of your own](recipes/cache-store.md): you get a cache
  `Store` in a SQLite file, named by `CACHES[...]["BACKEND"]`.
- [A broker of your own](recipes/broker.md): you get a `PullBroker` over
  a SQLite file, named by `BROKERS[...]["BACKEND"]`.

## Before you start

Each recipe starts from `domainkit init`. [The tutorial](tutorial.md)
explains what `init` makes, and the [deployment checklist](deploying.md)
says what to do before the first deployment.

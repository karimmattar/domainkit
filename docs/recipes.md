# How-to recipes

Short pages, each one task from a new project to tests that pass. Every
command and code change on them is run as written, in order, by the tests
(`domainkit_tests/test_recipes.py`), as the tutorial's are; if one fails
for you, that is a bug: please report it.

- [A payment that runs again when it loses a race](recipes/payments.md):
  one `SERIALIZABLE` transaction over two wallets, rows locked in id
  order, and `uow.run(..., retry=Retry(...))`.
- [Work that commits once per message](recipes/exactly-once.md): a task
  marked `@inbox`, and a test that runs its message twice.
- [Replacing the key that signs JSON Web Tokens](recipes/jwt-keys.md):
  `PREVIOUS_KEYS` through a rollout, and a test of the tokens either key
  signed.
- [Running behind a proxy](recipes/behind-a-proxy.md): `TRUSTED_PROXIES`,
  addresses and request ids from the proxy only.
- [Reading from a replica](recipes/read-replica.md): read-only units of
  work for a resource's reads, and the replica's settings.

Each starts from `domainkit init`; [the tutorial](tutorial.md) explains
what that makes, and [deploying.md](deploying.md) what to do before the
first deployment.

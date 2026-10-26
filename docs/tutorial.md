# Getting started

Five short parts take you from `pip install` to a small bank API: accounts
with a balance and the rules that guard it, users who may do only what
they were granted, a reaction that runs in a worker, and the tests that
hold it all. Each part ends with a project that works, and each takes
about a quarter of an hour. None of them needs a reference page.

1. [Install, and a first resource](tutorial/1-first-resource.md): a
   project, a domain, a user, and the first requests with `curl`.
2. [An aggregate with rules](tutorial/2-rules.md): a balance, a deposit,
   and rules written by hand; a migration for the new column.
3. [Users and permissions](tutorial/3-permissions.md): a clerk who may read
   accounts and nothing more.
4. [Events and the outbox](tutorial/4-events.md): a reaction to each
   deposit, stored with it and run by the worker.
5. [Tests, and getting ready for production](tutorial/5-testing-and-deploying.md):
   tests of the rules, and the checks production runs at startup.

You need Python 3.13 or 3.14, and `curl`. Work through the parts in order,
in one directory: each starts where the one before ended.

Every command and every code change on these pages is run as written, in
that order, by the tests (`domainkit_tests/test_walkthroughs.py`). If one
fails for you, that is a bug: please report it.

After the tutorial:

- [operations.md](operations.md): running it in production, and
  [runbook.md](runbook.md) for when an alarm fires.
- [security.md](security.md): the hardening checklist.
- [upgrading.md](upgrading.md): moving a project to a new minor version.
- [api.md](api.md): every public name; the documentation site renders
  each one's docstring under "API reference".

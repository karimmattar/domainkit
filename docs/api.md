# Public API

This is the public API of domainkit, by module: every name a module lists
in `__all__`, one line each, on the pages below. A name listed there is
covered by the compatibility policy of [versioning.md](versioning.md): a
patch release never removes or breaks it, and a minor release removes it
only after deprecating it ([ADR 0076](adr/0076-public-api-is-docs-api-md.md)).

Anything else is internal, even when it can be imported: names starting
with `_`, modules without `__all__`, and names a module defines but does
not list. Import public names from the module that lists them; a name
listed in a package and in one of its modules is the same object.

Besides these names, settings and their defaults ([reference.md](reference.md)),
management commands and their options, the generated domain layout, and
the contrib apps' table schemas are public too.

`domainkit_tests/test_api.py` checks that these pages and the code agree:
every name listed imports from the module it is listed under, and every
name in an `__all__` is listed, once. On the documentation site, the API
reference renders each name's docstring, module by module.

| Page | Modules |
| --- | --- |
| [Setup, settings, and the process](api/core.md) | `domainkit`, `apps`, `architecture`, `checks`, `clock`, `conf`, `deadlines`, `debug`, `deprecation`, `encryption`, `exceptions`, `i18n`, `lifecycle`, `log`, `management`, `reporting`, `signing`, `tasks`, `testing`, `tracing`, `utils` |
| [Domains, data, and resources](api/data.md) | `ddd`, `money`, `db`, `resources`, `ledger`, `cache`, `storage`, `broker` |
| [HTTP](api/http.md) | `http` and its modules |
| [Users and permissions](api/auth.md) | `contrib.auth` and its modules |
| [The other optional apps](api/contrib.md) | `contrib.accounting`, `admin`, `audit`, `contenttypes`, `evidence`, `flags`, `idempotency`, `limits`, `mail`, `messaging`, `notifications`, `objectpermissions`, `privacy`, `webhooks` |

Until 0.13 every module was listed on this one page; each keeps its
heading, and its anchor (`#domainkitcache`), on its new page.

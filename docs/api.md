# Public API

This page tells you which names of domainkit are public, and where they
are listed. A public name is one you may import and rely on: a patch
release never removes or breaks it, and a minor release removes it only
after it deprecates it ([versions and compatibility](versioning.md),
[ADR 0076](adr/0076-public-api-is-docs-api-md.md)).

## What is public

A name is public when a module lists it in its `__all__`. The pages
below list every such name, module by module, each with the first line
of its docstring.

Everything else is internal, even when Python lets you import it:

1. names that start with `_`;
2. modules that have no `__all__`;
3. names that a module defines but does not list in `__all__`.

Import a public name from the module that lists it. When a package and
one of its modules both list a name, the two are the same object.

Some public parts of domainkit are not Python names:

1. the settings and their defaults ([reference](reference.md));
2. the management commands and their options
   ([commands](reference/commands.md));
3. the layout of the domains that the generators write
   ([generators](resources.md#generators));
4. the table schemas of the optional apps ([optional apps](contrib.md)).

## The pages

| Page | What you find there | Modules |
| --- | --- | --- |
| [Setup, settings, and the process](api/core.md) | Setting up domainkit, installed domains and their hooks, settings, checks, the clock and deadlines, logs and traces, translations, keys and signing, cloud credentials, commands and generators, tasks, multi-tenancy, and the test tools. | `domainkit`, `apps`, `architecture`, `checks`, `clock`, `cloud`, `conf`, `deadlines`, `debug`, `deprecation`, `encryption`, `exceptions`, `i18n`, `keys`, `lifecycle`, `log`, `management`, `reporting`, `signing`, `tasks`, `tenancy`, `testing`, `tracing`, `utils` |
| [Domains, data, and resources](api/data.md) | The domain building blocks, money, the database layer, resources, ledgers, search, caches, storages, and brokers. | `ddd`, `money`, `db`, `resources`, `ledger`, `search`, `cache`, `storage`, `broker` |
| [HTTP](api/http.md) | The application, access rules, middleware, errors and responses, pages and filters, live connections, uploads, and outgoing calls. | `http` and its modules |
| [Users and permissions](api/auth.md) | Users, groups, permissions, logins, tokens, second factors, identity providers, passkeys and devices, phone numbers, and sign-in approvals. | `contrib.auth`, `contrib.identity`, `contrib.devices`, `contrib.phones`, `contrib.ciba`, and their modules |
| [The other optional apps](api/contrib.md) | Every other optional app. | `contrib.accounting`, `admin`, `approvals`, `audit`, `contenttypes`, `evidence`, `flags`, `idempotency`, `keys`, `limits`, `mail`, `messaging`, `notifications`, `objectpermissions`, `privacy`, `webhooks` |

On the documentation site, "API reference" shows the full docstring of
each listed name, module by module. It is built from these pages, so it
shows exactly the public names.

## How the tests keep this list true

`domainkit_tests/test_api.py` checks that these pages and the code
agree:

1. every listed name imports from the module it is listed under;
2. every name in an `__all__` is listed once, in the same order;
3. every `domainkit.module.Name` that another page of the documentation
   shows is a listed name.

!!! note
    Until 0.12, this one page listed every module. Each module kept its
    heading, and its anchor (such as `#domainkitcache`), on its new page.

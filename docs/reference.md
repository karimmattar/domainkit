# domainkit reference

The reference pages list facts to find quickly: every setting, check,
command, middleware, header, and error code. They do not teach; the
guides do. Use this page to find the reference page you need.

## Settings and configuration

- [Settings](reference/settings.md): how a settings module loads, and
  every core setting with its default and limits, including the
  [layer rules](reference/settings.md#layer-rules),
  [languages and time zones](reference/settings.md#languages-and-time-zones),
  and the [debug toolbar](reference/settings.md#debug-toolbar).
- [Databases, caches, storages, and brokers](reference/backends.md):
  the settings of the services that keep your data and your work:
  `DATABASES`, `LEDGERS`, `SHARDS`, `CACHES`, `STORAGES`, and `BROKERS`,
  and how to [size the pools](reference/backends.md#sizing-pools).
- [HTTP](reference/http.md): the
  [HTTP settings](reference/http.md#http),
  [outgoing calls](reference/http.md#outgoing-http-calls)
  (`HTTP_CLIENT`), [live connections](reference/http.md#live-connections),
  the [middleware](reference/http.md#middleware) and its order, and the
  [security headers](reference/http.md#security-headers).
- [Server limits](reference/servers.md): the options of uvicorn and
  Granian, and what to set in the proxy in front of them.

## Checks and errors

- [Deployment checks](reference/checks.md): every check id, what staging
  and production refuse, and what `check --deploy` warns about.
- [Errors, JSON responses, and the API surface](reference/responses.md):
  the [error body](reference/responses.md#errors), how
  [JSON is written](reference/responses.md#json-responses),
  [operation ids](reference/responses.md#operation-ids),
  [versions and deprecations](reference/responses.md#versions-and-deprecations),
  and [included routers](reference/responses.md#included-routers).
- [Problem codes](reference/problems.md): every `code` an error answer
  can carry, its HTTP status, and what raises it.

## Tools

- [Commands](reference/commands.md): every `manage.py` command and its
  options, the [operator](reference/commands.md#operators) recorded in
  the audit trail, [fixtures, flush, and seeds](reference/commands.md#data-fixtures-flush-and-seeds),
  and [squashing migrations](reference/commands.md#squashing-migrations).
- [The test plugin](reference/test-plugin.md): how to turn on
  `domainkit.testing` in a project. [Testing](testing.md) explains what
  it brings.
- [Signing](reference/signing.md): `domainkit.signing` for values you
  hand out and take back unchanged, and
  [encryption](reference/signing.md#encryption).
- [Observability](reference/observability.md): the log lines, the
  metrics and their labels, and the traces.
- [Extension points](reference/extension-points.md): what a project can
  implement (a backend, a store, a rule), and where it registers it.

## Elsewhere

Some facts live with the feature they belong to:

- the settings of each optional app (`MESSAGING`, `AUDIT`, `AUTH`, and
  the others) are on the app's guide, listed in
  [optional apps](contrib.md);
- databases, shards, and ledgers are explained in
  [databases](databases.md), and caches in [caches](cache.md);
- every public name is in [the public API](api.md);
- releases and deprecations are in
  [versions and compatibility](versioning.md).

!!! note
    Until 0.12, these were sections of this one page. A link to one of
    them, such as `reference.md#server-limits`, now opens this page. Each
    section kept its name and its anchor on its new page.

`domainkit_tests/test_docs.py` fails when a setting or a command is
missing from these pages, or when a table's default or limits differ
from the code's.

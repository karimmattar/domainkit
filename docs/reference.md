# domainkit reference

Settings, checks, middleware, and commands, a page for each area. For the
optional apps (messaging and its broker, audit, idempotency) see
[contrib.md](contrib.md); for databases, shards, and ledgers see
[databases.md](databases.md); for caches see [cache.md](cache.md); for
the public names see [api.md](api.md); for releases and deprecations see
[versioning.md](versioning.md).

| Page | What to look up there |
| --- | --- |
| [Settings](reference/settings.md) | How settings load; the core settings, [layer rules](reference/settings.md#layer-rules), [languages and time zones](reference/settings.md#languages-and-time-zones), and the [debug toolbar](reference/settings.md#debug-toolbar) |
| [Databases, caches, storages, and brokers](reference/backends.md) | [`DATABASES`, `LEDGERS`, `SHARDS`](reference/backends.md#databases-and-migrations) and [sizing pools](reference/backends.md#sizing-pools); [`CACHES`](reference/backends.md#caches), [`STORAGES`](reference/backends.md#storages), [`BROKERS`](reference/backends.md#brokers) |
| [HTTP](reference/http.md) | The [HTTP settings](reference/http.md#http), [outgoing calls](reference/http.md#outgoing-http-calls) (`HTTP_CLIENT`), [live connections](reference/http.md#live-connections), the [middleware](reference/http.md#middleware) and its order, and the [security headers](reference/http.md#security-headers) |
| [Server limits](reference/servers.md) | uvicorn's and Granian's options, and what to set in front of them |
| [Deployment checks](reference/checks.md) | Every check id: what staging and production refuse, and what `check --deploy` warns about |
| [Errors, JSON responses, and the API surface](reference/responses.md) | [Error bodies and codes](reference/responses.md#errors), [JSON responses](reference/responses.md#json-responses), [operation ids](reference/responses.md#operation-ids), [versions and deprecations](reference/responses.md#versions-and-deprecations), and [included routers](reference/responses.md#included-routers) |
| [Signing](reference/signing.md) | `domainkit.signing`, and [encryption](reference/signing.md#encryption) |
| [Observability](reference/observability.md) | Logs, metrics and their labels, and traces |
| [The test plugin](reference/test-plugin.md) | Enabling `domainkit.testing`; [testing.md](testing.md) covers what it brings |
| [Commands](reference/commands.md) | Every command and its options, [operators](reference/commands.md#operators), [fixtures, flush, and seeds](reference/commands.md#data-fixtures-flush-and-seeds), and [squashing migrations](reference/commands.md#squashing-migrations) |
| [Extension points](reference/extension-points.md) | What a project implements, and where it registers it |

Until 0.13 these were the sections of this one page; a link to one of
them (`reference.md#server-limits`) now opens this list, and each keeps
its name and its anchor on its new page.

`domainkit_tests/test_docs.py` fails when a setting or a command is missing
from these pages, or when a table's default or limits differ from the code's.

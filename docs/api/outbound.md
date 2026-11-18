# Public API: outgoing calls

`domainkit.outbound` and its modules: the outgoing HTTP client, and the
guard that decides which URLs a server may call. Until 2.9 they were
`domainkit.http.client` and `domainkit.http.guard`, which still import
every name they had, the same objects
([HTTP](http.md#domainkithttpclient),
[ADR 0195](../adr/0195-move-the-outgoing-client-below-the-adapters-and-hold-module-state-to-its-resets.md)).
Part of the [public API](../api.md), under its compatibility policy.

## `domainkit.outbound.client`

Outgoing HTTP calls, bounded by the request's deadline (`domainkit[httpclient]`).

- `CallTimeout`: A call took longer than its `timeout`, retries included.
- `CircuitBreaker`: Circuits per host: open after failures in a row, then one trial at a time.
- `CircuitOpen`: Calls to a host fail at once while its circuit is open.
- `HttpClient`: Outgoing calls, pooled, bounded, retried, and broken off per host.
- `ResponseTooLarge`: An answer was longer than `MAX_RESPONSE_BYTES`, as sent or decompressed.
- `UnsafeURL`: A URL that may not be called; the message never quotes it.
- `client`: A client with `HTTP_CLIENT`'s options, for calls that need no others.
- `close_all`: Close every client's connections on the running event loop.
- `reset_circuits`: Close every client's circuits, forgetting their failures (New in 2.9).
- `stub`: Send every call made in the block (and tasks it starts) to `handler`.

## `domainkit.outbound.guard`

Which URLs a server may call: the guard against server-side request forgery.
Public since 2.9, for an app that takes URLs from its users.

- `MAX_URL_LENGTH`: The longest URL `checked_url` takes; a longer one is refused.
- `Address`: An address a host resolves to.
- `Network`: A network the checks let through (`allowed`), such as a private one.
- `UnsafeURL`: A URL that may not be called; the message never quotes it.
- `checked_address`: The address to connect to for `url` (see `checked_host`).
- `checked_host`: The address to connect to for `host`, once all of its addresses pass.
- `checked_url`: Parse `url` and check everything that does not need a lookup.
- `lookup`: Every address `host` resolves to, on the guard's threads (`resolver`).
- `lookups_for`: Count the guard's lookups in the block, and tasks it starts, as `owner`'s.
- `tls_context`: A client TLS context for calls to URLs someone else chose, built once.

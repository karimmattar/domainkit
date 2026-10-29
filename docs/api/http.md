# Public API: HTTP

`domainkit.http` and its modules: the application, access rules,
middleware, responses, routing, live connections, uploads, and outgoing
calls. Part of the [public API](../api.md), under its compatibility
policy.

## `domainkit.http`

FastAPI integration: application factory, middleware, and error mapping.

- `CURSOR_PATTERN`: Cursors are base64url; use as `Query(pattern=CURSOR_PATTERN)`.
- `MAX_CURSOR_LENGTH`: Longest cursor accepted; use as `Query(max_length=MAX_CURSOR_LENGTH)`.
- `APIError`: An HTTP error outside the domain (a bad cursor, an oversized body).
- `ErrorResponse`: Body of every error response; also the schema documented in OpenAPI.
- `FieldError`: One invalid part of a request; the submitted value is left out.
- `Filter`: One filtered field of a `FilterSet` and the lookups it allows.
- `FilterSet`: Declarative filters, ordering, and search for one aggregate's listing.
- `InvalidCursor`: The cursor was not issued by this API, or was altered.
- `InvalidFilter`: The query string asks for a filter the endpoint does not offer.
- `JSONResponse`: Starlette's `JSONResponse`, rendered by `encode_json`: the same bytes.
- `PageOut`: A page of items: pass `next_cursor` back as `cursor` for the next.
- `PageRequest`: The page a request asks for; `Pagination` makes it from the query string.
- `Pagination`: A FastAPI dependency that reads a `PageRequest` from the query string.
- `cursor_length`: The longest cursor `encode_position` makes for `ordering` over `model`.
- `decode_cursor`: Decode a cursor made by `encode_cursor` with `size` values.
- `encode_cursor`: Encode a sort key as an opaque, URL-safe cursor.
- `error_response`: Build an error body that carries the request id for support cases.
- `error_responses`: OpenAPI `responses` for the error statuses a route may answer with.
- `get_asgi_application`: Set up the framework and return the configured application.

## `domainkit.http.access`

Who may call an endpoint: access rules, for any authentication (ADR 0080).

- `MODEL_ACTIONS`: The permission each method needs from `ModelPermissions`.
- `SAFE_METHODS`: Methods that only read: `IsAuthenticatedOrReadOnly` lets anyone use them.
- `AllOf`: Every rule must allow.
- `AllowAny`: Anyone, logged in or not.
- `AllowScoped`: Principals who proved who they are, credentials limited to scopes too.
- `AnonymousPrincipal`: A caller who proved nothing: no id, no permissions, for any middleware.
- `AnyOf`: One rule must allow, for the request and for the object alike.
- `BasePermission`: Allows everything; subclasses override one hook or both.
- `CurrentUser`: The request's principal, or an anonymous one.
- `DenialRecorder`: An installed domain that keeps the record of refused requests.
- `HasObjectPermissions`: Principals granted every permission named on the object itself.
- `HasPermissions`: Principals granted every permission named (`<domain>.<codename>`).
- `HasScope`: Users whose credentials allow every scope named; unscoped ones allow all.
- `IsAuthenticated`: Principals who proved who they are (users who logged in).
- `IsAuthenticatedOrReadOnly`: Anyone may read (`GET`, `HEAD`, `OPTIONS`); users may write.
- `IsUnscoped`: Users whose credentials are not limited to scopes (a login, not a key).
- `ModelPermissions`: Django's model permissions, by method, for one model.
- `Not`: Allows what `rule` refuses.
- `NotAuthenticated`: The route needs a principal who proved who they are, and none did (401).
- `Principal`: Who sent a request, as the access rules read them (`scope["user"]`).
- `Public`: `dependencies=[Public]`: the route needs no rule (`REQUIRE_ROUTE_RULES`).
- `Require`: A FastAPI dependency: the rules must allow the request; returns the principal.
- `Rule`: A rule, or a rule class that takes no arguments.
- `acurrent_user`: `current_user` as a dependency FastAPI awaits, with no thread to hop to.
- `check_object_permissions`: Refuse the request unless every rule allows it on `obj`.
- `check_scopes_bounded`: Hold credentials limited to scopes to routes with a rule that checks them.
- `current_user`: The principal the authentication middleware found (anonymous if none).
- `mark_public`: Mark a mounted application or a Starlette endpoint public on purpose.
- `open_routes`: `METHOD /path` of every route that declares no access rule.
- `reads_principal`: Mark `dependency` as one that reads the principal, as `CurrentUser` does.
- `scopes_of`: The scopes the request's credentials are limited to; None: no limit.

## `domainkit.http.apidocs`

The API's documentation pages, from domainkit's own copies (`API_DOCS`).

- `REDOC_POLICY`: `/redoc`'s Content-Security-Policy: `/docs`'s, with inline styles and a `blob:` worker.
- `SWAGGER_POLICY`: `/docs`'s Content-Security-Policy: scripts and styles from the origin only.
- `docs_routes`: `/docs`, `/redoc`, and their assets, for an API called `title`.

## `domainkit.http.application`

Build the FastAPI application from settings (Django's `get_asgi_application`).

- `Application`: The application `get_asgi_application` builds: telemetry off below it too.
- `get_asgi_application`: Set up the framework and return the configured application.

## `domainkit.http.blocking`

Find code that blocks the event loop before production does.

- `allow_sync`: Mark a sync route handler or dependency as sync on purpose.

## `domainkit.http.client`

Outgoing HTTP calls, bounded by the request's deadline (`domainkit[httpclient]`).

- `CallTimeout`: A call took longer than its `timeout`, retries included.
- `CircuitBreaker`: Circuits per host: open after failures in a row, then one trial at a time.
- `CircuitOpen`: Calls to a host fail at once while its circuit is open.
- `HttpClient`: Outgoing calls, pooled, bounded, retried, and broken off per host.
- `ResponseTooLarge`: An answer was longer than `MAX_RESPONSE_BYTES`, as sent or decompressed.
- `UnsafeURL`: A URL that may not be called; the message never quotes it.
- `client`: A client with `HTTP_CLIENT`'s options, for calls that need no others.
- `close_all`: Close every client's connections on the running event loop.
- `stub`: Send every call made in the block (and tasks it starts) to `handler`.

## `domainkit.http.deadlines`

Request deadlines over HTTP: `REQUEST_TIMEOUT`, and `RequestTimeout` per route.

- `DeadlineMiddleware`: Cancel HTTP requests that run past their deadline, and answer 503.
- `RequestTimeout`: A dependency that gives a route its own deadline (`Depends`).

## `domainkit.http.live`

Long-lived connections: WebSockets and event streams, bounded and re-checked.

- `Closed`: Raised by an event source to end its connection, saying why.
- `Closure`: Why the framework ended a connection: the close code and its reason.
- `LiveLimits`: A FastAPI dependency: a route's own limits for its connections.

## `domainkit.http.live_events`

Live committed events: domain events streamed to the clients that may see them.

- `live_events`: The process's live events, fed by the global `event_bus`.

## `domainkit.http.locale`

`LocaleMiddleware`: answer in the language the caller asks for.

- `LocaleMiddleware`: Make the language `Accept-Language` asks for the request's language.

## `domainkit.http.metrics`

Prometheus metrics (`domainkit[metrics]`).

- `MetricsMiddleware`: Count requests and time them, by method, route template, and status.
- `refreshers_kept`: Put the scrape's refreshers back as they were when the block ends.

## `domainkit.http.middleware`

Pure-ASGI middleware enforcing the HTTP security baseline.

- `AllowedHostsMiddleware`: Reject requests whose Host header is not in `ALLOWED_HOSTS`.
- `CORSMiddleware`: Starlette's CORS middleware configured from `CORS_*` settings.
- `ProxyHeadersMiddleware`: Take the client address and scheme from trusted proxies' headers.
- `RequestBodyLimitMiddleware`: Refuse bodies over the limit, or too slow to arrive.
- `RequestContextMiddleware`: Bind request context, echo the request id, and write the access log.
- `SecurityHeadersMiddleware`: Add security headers unless the endpoint already set them.
- `body_limit`: Give a route a body limit of its own: `size` bytes, above or below the global.
- `request_path`: The request's path within the application: without its `root_path`.

## `domainkit.http.ratelimit`

Request rate limiting (`RATE_LIMIT = "300/minute"`).

- `FALLBACK_KEYS`: Callers the local fallback of `RedisBackend` counts at most (`MemoryBackend`).
- `Backend`: Where rate-limit state lives.
- `Decision`: Whether a request may proceed, and the numbers for the headers.
- `MemoryBackend`: GCRA state in this process, for at most `max_keys` callers.
- `RateLimitMiddleware`: Refuse callers over `RATE_LIMIT`; place it after RequestContextMiddleware.
- `RedisBackend`: GCRA in Redis or Valkey, shared by every replica (`RATE_LIMIT_CACHE`).
- `RefundingBackend`: A backend that can give a counted request back.
- `Throttle`: A FastAPI dependency: at most `rate` requests per caller to a route.

## `domainkit.http.responses`

JSON responses encoded in Rust, byte for byte as Starlette encodes them.

- `BuiltJSONResponse`: JSON bodies built of JSON's own values (`model_dump(mode="json")`): no check.
- `JSONResponse`: Starlette's `JSONResponse`, rendered by `encode_json`: the same bytes.
- `json_native`: Whether `to_json` writes `content` as `json.dumps` does (see above).

## `domainkit.http.routing`

Serve the routes of included routers from the top of the application.

- `RouteIndex`: Serves a router's requests trying only the routes their path can reach.
- `flatten_routes`: Put the routes of each router included in `router` in its place.
- `index_routes`: Serve `router`'s requests through a `RouteIndex`; whether it does.
- `serves_itself`: Whether `router` has its own version of a method that serves requests.

## `domainkit.http.shedding`

Load shedding: a cap on the HTTP requests an event loop works on at once.

- `ConcurrencyLimitMiddleware`: Refuse HTTP requests past `MAX_CONCURRENT_REQUESTS` with 503.

## `domainkit.http.sse`

Server-Sent Events: a response that streams events to a browser's `EventSource`.

- `Event`: What a stream yields: an event, or text sent as one event's data.
- `EventSourceResponse`: Stream `events` as Server-Sent Events (`text/event-stream`).
- `ServerSentEvent`: One event of a stream: its data, and optionally a type, id, and retry.
- `comment`: `text` as a comment, which `EventSource` ignores.
- `last_event_id`: The id of the last event a reconnecting client saw (`Last-Event-ID`).

## `domainkit.http.uploads`

Receiving uploaded files, and serving stored ones, safely.

- `GIF`: A type of file a route accepts, and how to recognize its first bytes.
- `IMAGES`: Images browsers show; SVG is not one of them, as it can carry scripts.
- `JPEG`: A type of file a route accepts, and how to recognize its first bytes.
- `PDF`: A type of file a route accepts, and how to recognize its first bytes.
- `PNG`: A type of file a route accepts, and how to recognize its first bytes.
- `TEXT`: UTF-8 text, CSV included; it cannot be told apart by its bytes.
- `WEBP`: A type of file a route accepts, and how to recognize its first bytes.
- `ZIP`: Also Office documents (`.docx`, `.xlsx`), which are zip files.
- `FileType`: A type of file a route accepts, and how to recognize its first bytes.
- `sanitize_filename`: A client's file name, safe to show and to put in `Content-Disposition`.
- `save_upload`: Store `upload` if it is one of `types` and at most `max_size` bytes.
- `serve`: Answer with the stored file `name`, streamed, as `content_type`.

## `domainkit.http.versioning`

API versions by URL prefix, and deprecation headers (RFC 9745, RFC 8594).

- `Deprecation`: When routes were deprecated, when they go, and where to read why.
- `versioned_router`: A router for one API version, served under `/<version>`.

## `domainkit.http.websockets`

`WebSocketLimitsMiddleware`: every WebSocket bounded, and authorized for life.

- `WebSocketLimitsMiddleware`: Cap, queue, rate-limit, time, and re-authorize every WebSocket.

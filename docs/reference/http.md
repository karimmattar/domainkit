# HTTP: settings, middleware, and live connections

These settings decide what the application accepts and how it answers:
the hosts it serves, the size and time limits of a request, the headers
of every response, rate limits, and the proxies it trusts. Middleware
holds every request to them. This page lists each setting, the middleware
in the order to keep, the defaults of outgoing calls, and the limits of
WebSockets and event streams.

On this page you find:

- the HTTP settings, grouped by subject;
- the defaults of outgoing HTTP calls (`HTTP_CLIENT`);
- the limits of live connections;
- the middleware and its order, and the security headers of every
  response.

## A first example

The application that `init` makes answers a health check like this:

```python
from domainkit.http import get_asgi_application
from domainkit.testing import TestClient

with TestClient(get_asgi_application(), base_url="http://localhost") as client:
    answer = client.get("/health")
    print(answer.status_code, answer.text)
    for name, value in answer.headers.items():
        print(f"{name}: {value}")
```

```text
200 {"status":"ok"}
content-length: 15
content-type: application/json
server-timing: db;dur=0.0;desc="0 queries", app;dur=0.7
x-request-id: f1a4b4976bad7884fcd39d41b775b9d4
cache-control: no-store
x-frame-options: DENY
x-content-type-options: nosniff
referrer-policy: no-referrer
content-security-policy: default-src 'none'; frame-ancestors 'none'
permissions-policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()
cross-origin-opener-policy: same-origin
content-language: en
vary: Accept-Language
```

Each header comes from a setting on this page: the request id from
`ACCEPT_REQUEST_ID`, the security headers from the `SECURE_*` settings,
and the language from `LANGUAGE_CODE`. `server-timing` is the
[debug toolbar](settings.md#debug-toolbar)'s, in local settings only.

## HTTP

### Hosts and the API's documentation

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost`, `127.0.0.1`, `[::1]` | The `Host` values the application accepts; `*.example.com` matches subdomains. A `Host` that is not a name, an IPv4 address, or a bracketed IPv6 address (with an optional port) is refused before matching (400 `invalid_host`). A wildcard `*` is refused in staging and production. |
| `API_DOCS` | `None` | Serves `/docs`, `/redoc`, and `/openapi.json`. `None` follows `DEBUG`, so a deployment serves them only with `API_DOCS = True`. The pages load Swagger UI and ReDoc from domainkit's own copies under `/docs/assets/`, never a CDN, with a Content-Security-Policy of their own ([Security headers](#security-headers)). `check` warns when it is on in staging and production. |
| `OPENAPI_OPERATION_IDS` | `"route_name"` | Each operation id is the route's name, stable when routers move (see [The API surface](responses.md#the-api-surface)). `"fastapi"`: FastAPI's ids (name, path, and method), the default until 1.0. |
| `ERROR_FORMAT` | `"problem+json"` | Errors are RFC 9457 problem details (see [Errors](responses.md#errors)). `"domainkit"`: the `{"detail", "code", "request_id"}` body alone, as `application/json`, the default until 1.0. |
| `ERROR_TYPE_BASE_URL` | `None` | With problem details, each code's `type` is this absolute URL plus the code: `https://errors.example.com/` gives `https://errors.example.com/not_found`. Unset, it is `about:blank`. |

### Request bodies, deadlines, and load

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_REQUEST_BODY_SIZE` | `1048576` | Bytes. A larger body gets 413, whether its size is declared or streamed. A larger WebSocket message, text counted in UTF-8 bytes, closes the connection with 1009. A route with `body_limit(size)` (1 byte to 1 GiB) is held to its own limit instead, above or below this one ([uploads.md](../uploads.md#body-limits-per-route)). |
| `REQUEST_BODY_TIMEOUT` | `10.0` | Seconds (at most 3600) the application may wait for a request's body, all its chunks together (`RequestBodyLimitMiddleware`). Past it, the request is answered 408 `request_timeout`, and the connection is closed. Time spent working between chunks, or after the response starts, does not count. |
| `REQUEST_TIMEOUT` | `30.0` | Seconds (at most 3600) an HTTP request may take until its response starts (`DeadlineMiddleware`). Past it, the work is cancelled and answered 503 `deadline_exceeded`. A route sets its own with `RequestTimeout`. `None`: no deadline ([runtime.md](../runtime.md#request-deadlines)). Keep it below `IDEMPOTENCY.LEASE_SECONDS` where idempotency is installed. |
| `MAX_CONCURRENT_REQUESTS` | `1000` | HTTP requests each event loop works on at once (`ConcurrencyLimitMiddleware`), at most 100,000. A request that waits for its body is not counted meanwhile. `None`: no cap ([runtime.md](../runtime.md#load-shedding)). |
| `MAX_QUEUED_REQUESTS` | `100` | Requests past the cap that may wait for a slot (at most 100,000). The next ones are answered 503 `overloaded` at once. |
| `REQUEST_QUEUE_TIMEOUT` | `1.0` | Seconds (at most 60) a request past the cap waits for a slot, before it is answered 503 `overloaded`. |
| `CONCURRENCY_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted against the cap, so probes and scrapes are never refused. They are compared without the root path the application is mounted at (`--root-path`). |
| `GUARD_LOOKUP_THREADS` | `16` | Threads (1 to 256) that look host names up for guarded calls and webhooks, in each process. One owner (a request's user or address, or a webhook delivery's subscription) may hold a quarter of them, so hosts that one owner chose and that never answer leave the rest to others ([runtime.md](../runtime.md#outgoing-http-calls)). |
| `HTTP_CLIENT` | see [Outgoing HTTP calls](#outgoing-http-calls) | The defaults of outgoing calls through `domainkit.http.client`. |

### Compression

| Setting | Default | Meaning |
| --- | --- | --- |
| `GZIP_MINIMUM_SIZE` | `500` | Bytes (0 or more) a response needs before `GZipMiddleware` compresses it. |
| `GZIP_LEVEL` | `6` | `GZipMiddleware`'s compression level, from 1 (fastest) to 9 (smallest). |

### Readiness

`/ready` answers 200 when every readiness check passes, and 503 when one
fails ([operations.md](../operations.md)).

| Setting | Default | Meaning |
| --- | --- | --- |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take (at most 60). |
| `READINESS_CACHE_SECONDS` | `1.0` | How long a `/ready` result is reused, from when its checks ended. Probes that come while checks run wait for them, so probe floods cannot load the database. |
| `READINESS_DETAILS` | `False` | Names failing components in `/ready` responses (they are always logged). A group of checks that raises instead of reporting is named as its group (`domains`, `databases`, `ledgers`, `caches`), logged, and kept like any failed round: a 503, never a 500. |

### The access log

| Setting | Default | Meaning |
| --- | --- | --- |
| `ACCESS_LOG_QUIET_PATHS` | `/health`, `/ready` | Successful requests to these paths are logged at DEBUG. They are compared without the root path (`--root-path`). |
| `ACCESS_LOG_SAMPLE_RATE` | `1.0` | The share of access lines written, from 0 to 1: `0.1` writes about one request in ten, each drawn on its own. These are always written: responses from 400 up (client errors, refusals, and server errors), requests without a response, and requests that take `ACCESS_LOG_SLOW_DURATION` or longer. So only successes and redirects are sampled. `domainkit.security` and the audit trail are never sampled ([runtime.md](../runtime.md#sampling-the-access-log)). |
| `ACCESS_LOG_SLOW_DURATION` | `1.0` | Seconds (at most 3600). A request this slow or slower has its access line written, whatever `ACCESS_LOG_SAMPLE_RATE` says. |
| `ACCESS_LOG_PATH` | `"template"` | The path that access lines, the request middleware's error lines, error reports, and the request context show: the matched route's template (`/accounts/{account_id}`), or `unmatched`. `"raw"` shows the path as sent, ids and tokens included ([runtime.md](../runtime.md#paths-in-logs)). |

### Proxies and request ids

See [Running behind a proxy](../recipes/behind-a-proxy.md) for a complete
setup.

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRUSTED_PROXIES` | `()` | Networks (`10.0.0.0/8`) whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed, and `"unix"` for the proxy on the other end of a unix socket (`uvicorn --uds`, `granian --uds`), which has no address (uvicorn reports no client, Granian an empty host). The client is found by walking `X-Forwarded-For` from the right, across every line of the header, through trusted proxies: it is the first hop that is not one, and nothing left of it is read. Hops may carry a port. A hop there that is not an address (a proxy's `unknown`) counts the client as the last proxy passed, logged on `domainkit.security`. The scheme is the `PROXY_SCHEME_HEADER` value at the client's place from the right, or the right-most one when there are fewer. Empty: the socket peer is the client. |
| `PROXY_SCHEME_HEADER` | `"X-Forwarded-Proto"` | The header in which the trusted proxies say the client's scheme (a header name). `None`: the scheme is never taken from a header. Set it to `None` when the proxies do not set it (HAProxy's `option forwardfor` alone, an NLB); otherwise a client's own `X-Forwarded-Proto` would be read, and choose whether cookies are `Secure`. `None` holds only where the server reads no proxy header itself: uvicorn, unless run with `--no-proxy-headers`, sets the scheme from `X-Forwarded-Proto` before domainkit sees the request, for every request from `FORWARDED_ALLOW_IPS` (`127.0.0.1` by default: a proxy on the same host). Granian reads none. |
| `ACCEPT_REQUEST_ID` | `"proxies"` | Whose `X-Request-ID` names a request (in log lines, error reports, audit rows, and outgoing calls). `"proxies"`: only a trusted proxy's (`TRUSTED_PROXIES`, or a unix socket's peer with `"unix"`), as `ProxyHeadersMiddleware` judged the connection. `"anyone"`: any caller's, as up to 0.10. `"never"`: nobody's. Of several lines, the last is read: the nearest proxy's. The proxy must set the header, over a client's (nginx: `proxy_set_header X-Request-ID $request_id;`), or add its line after it (HAProxy's `unique-id-header`); otherwise a client's own passes through it: set `"never"` if it does neither. Up to 128 letters, digits, `.`, `_`, `:`, or `-`, starting with a letter or a digit. Another value, or none, gets a new id (32 hex digits). A caller's value is dropped, not logged ([operations.md](../operations.md#request-ids)). |

### Rate limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `RATE_LIMIT` | `None` | Requests per caller, such as `"300/minute"` (`second`, `minute`, `hour`, `day`). `None` turns it off. |
| `RATE_LIMIT_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted, compared without the root path (`--root-path`). |
| `RATE_LIMIT_BACKEND` | `domainkit.http.ratelimit.MemoryBackend` | Where counts live. The default counts per process, and keeps the 100,000 callers seen most recently; one seen longer ago starts again from zero. Behind several replicas, use `domainkit.http.ratelimit.RedisBackend`: one count per caller in Redis, on the server's clock. If Redis fails, or refuses the count (out of memory, a read-only replica after a failover), requests are let through, and Redis is skipped for the cache's `FAILURE_COOLDOWN`. Password and code checks are refused meanwhile instead, in staging and production (`AUTH.THROTTLE_FAIL_CLOSED`), which also refuse to start with attempts counted in each process (`auth.E105`) ([auth.md](../auth/logging-in.md#logging-in-and-out)). |
| `RATE_LIMIT_CACHE` | `"default"` | The Redis cache that `RedisBackend` counts in. Keys are `<KEY_PREFIX>:ratelimit:<caller>`. |
| `RATE_LIMIT_FALLBACK_REPLICAS` | `None` | 1 to 1024. While Redis is down, `RedisBackend` counts each caller in its own process, at `RATE_LIMIT` divided by this many replicas (at most `FALLBACK_KEYS`, 10,000, callers; a caller who still owes requests is never forgotten, and while all of them do, new callers are refused). `None` lets requests through, as before. |
| `RATE_LIMIT_IPV6_PREFIX` | `64` | IPv6 clients are counted per network of this prefix length (1 to 128), because one customer usually holds a whole /64. Login throttling counts the same way. IPv4 clients are counted per address. |

The project template lists `RateLimitMiddleware` already; it counts
nothing until `RATE_LIMIT` is set:

```python
RATE_LIMIT = "300/minute"
RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"
RATE_LIMIT_FALLBACK_REPLICAS = 4
```

A caller over the limit gets 429 `rate_limited`, with `Retry-After`.

### Metrics

| Setting | Default | Meaning |
| --- | --- | --- |
| `METRICS_TOKEN` | `None` | The bearer token for `/metrics`. Without it, `/metrics` answers 403 in staging and production ([observability.md](observability.md)). |

### Response headers

| Setting | Default | Meaning |
| --- | --- | --- |
| `SECURE_HSTS_SECONDS` | `0` | The `Strict-Transport-Security` max-age. 0 leaves the header out. |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `False` | Adds `includeSubDomains`. |
| `SECURE_CONTENT_TYPE_NOSNIFF` | `True` | Sends `X-Content-Type-Options: nosniff`. |
| `SECURE_REFERRER_POLICY` | `"no-referrer"` | `Referrer-Policy`: one of its eight policies (`no-referrer`, `same-origin`, `strict-origin-when-cross-origin`, ...), or several, comma-separated. Anything else, a line break included, is refused at startup. `None` leaves it out. |
| `X_FRAME_OPTIONS` | `"DENY"` | Or `"SAMEORIGIN"`. |
| `SECURE_CONTENT_SECURITY_POLICY` | `"default-src 'none'; frame-ancestors 'none'"` | The `Content-Security-Policy` of every response that sets none: nothing an answer holds may load, run, or be framed. Printable ASCII on one line, at most 4096 characters. `None` leaves it out. |
| `SECURE_PERMISSIONS_POLICY` | `"accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"` | The `Permissions-Policy` of every response that sets none: those powerful browser features off. Bounded as above. `None` leaves it out. |
| `SECURE_CROSS_ORIGIN_OPENER_POLICY` | `"same-origin"` | `Cross-Origin-Opener-Policy`; or `"same-origin-allow-popups"`, `"unsafe-none"`. `None` leaves it out. |

### CORS

CORS lets a web page on another origin call the API from a browser.
`CORSMiddleware` answers it; the project template does not list it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CORS_ALLOWED_ORIGINS` | `()` | Origins allowed to call the API from a browser: `*`, or a scheme and host (and port), without a path, a space, or a comma. |
| `CORS_ALLOWED_METHODS` | `GET POST PUT PATCH DELETE` | Methods, or `*`: HTTP tokens of at most 64 characters, with no space or line break. |
| `CORS_ALLOWED_HEADERS` | `Authorization Content-Type X-Request-ID If-Match If-None-Match Idempotency-Key` | Request headers a browser may send: header names, or `*`. HTTP tokens of at most 64 characters, with no space or line break. Add the CSRF header (`X-CSRF-Token`) for cookie sessions. |
| `CORS_EXPOSE_HEADERS` | `X-Request-ID ETag Location Retry-After RateLimit-Limit RateLimit-Remaining RateLimit-Reset Idempotent-Replayed Content-Language Deprecation Sunset Link` | Response headers a script on an allowed origin may read; browsers hide the others. Header names, bounded as above. |
| `CORS_MAX_AGE` | `600` | Seconds a browser may keep a preflight's answer, 0 to 86,400. |
| `CORS_ALLOW_CREDENTIALS` | `False` | Lets the browser send cookies. It cannot be combined with origin `*`. |

```python
MIDDLEWARE = list(MIDDLEWARE)
MIDDLEWARE.insert(
    MIDDLEWARE.index("domainkit.http.middleware.RequestContextMiddleware"),
    "domainkit.http.middleware.CORSMiddleware",
)
CORS_ALLOWED_ORIGINS = ["https://shop.example.com"]
```

A preflight from that origin then gets:

```text
access-control-allow-methods: GET, POST, PUT, PATCH, DELETE
access-control-max-age: 600
access-control-allow-headers: Accept, Accept-Language, Authorization, Content-Language, Content-Type, Idempotency-Key, If-Match, If-None-Match, X-Request-ID
access-control-allow-origin: https://shop.example.com
```

Keep `CORSMiddleware` at its place in the [middleware order](#middleware):
after `SecurityHeadersMiddleware`, before `RequestContextMiddleware`.

## Outgoing HTTP calls

`HTTP_CLIENT` holds the defaults of every
`domainkit.http.client.HttpClient` (the `httpclient` extra). A client may
change its own timeout and retries. See
[runtime.md](../runtime.md#outgoing-http-calls).

| `HTTP_CLIENT` key | Default | Meaning |
| --- | --- | --- |
| `TIMEOUT` | `10.0` | Seconds (at most 3600) one call may take, its retries included, and never past the deadline of the work it serves. |
| `CONNECT_TIMEOUT` | `5.0` | Seconds to open a connection. |
| `RETRIES` | `2` | Retries (0 to 10) of a call that failed in a way worth trying again. |
| `BACKOFF` | `0.1` | Seconds before the first retry. The bound doubles for each next retry, and the wait is random below it. |
| `MAX_BACKOFF` | `2.0` | The longest wait before a retry (at most 600 seconds). A longer `Retry-After` ends the retries. |
| `BREAKER_FAILURES` | `5` | Failures in a row that open a host's circuit: its calls then fail at once with `CircuitOpen`. |
| `BREAKER_COOLDOWN` | `30.0` | Seconds (at most 3600) a host's circuit stays open; then one call at a time tries it. A client made with `breaker=False` keeps no circuits. |
| `MAX_CONNECTIONS` | `100` | Connections per client and event loop. |
| `MAX_KEEPALIVE_CONNECTIONS` | `20` | Idle connections each client keeps for the next calls. |
| `MAX_RESPONSE_BYTES` | `10485760` | The largest answer read (at most 1 GiB), counted as it comes and after decompression. A longer one raises `ResponseTooLarge`. |
| `USER_AGENT` | `"domainkit"` | The `User-Agent` of every call (printable ASCII, 1 to 256 characters). |
| `CA_BUNDLE` | `None` | The CA certificates of the servers called; the system's (or httpx2's) when unset. Guarded clients never trust the CAs that `SSL_CERT_FILE` or `SSL_CERT_DIR` name, unless they are listed here. |

```python
HTTP_CLIENT = {"TIMEOUT": 5.0, "RETRIES": 1, "USER_AGENT": "shop/1.4"}
```

## Live connections

WebSockets and Server-Sent Events streams are capped, queued, timed, and
checked again while they stay open; see [live.md](../live.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `LIVE_MAX_CONNECTIONS` | `1000` | Open WebSockets and event streams per process (at most 1,000,000). One more is refused with 503 and `Retry-After` (a WebSocket where the server cannot answer a handshake with a status gets a 1013 close). |
| `LIVE_MAX_CONNECTIONS_PER_CALLER` | `10` | The same, per user, or per address for anonymous callers (IPv6 per `RATE_LIMIT_IPV6_PREFIX` network). At most `LIVE_MAX_CONNECTIONS` (and 1,000,000). |
| `LIVE_SEND_QUEUE` | `64` | Messages or events that may wait for one client (1 to 10,000); a sender waits while they do. Live events count only those the client may see; 16 times as many may wait for its rule ([live.md](../live.md#live-committed-events)). |
| `LIVE_SEND_TIMEOUT` | `10.0` | Seconds one message may take to reach the client. A slower client is closed (1013). |
| `LIVE_REAUTH_INTERVAL` | `60.0` | Seconds between checks of an open connection's credentials and its route's rules (each one between three quarters of it and all of it; at most 3,600). The connection is closed with 1008 when they fail. `None` turns the checks off; `check` warns about that in staging and production. What the checks cannot do: [live.md](../live.md#what-re-checks-cannot-do). |
| `WEBSOCKET_IDLE_TIMEOUT` | `300.0` | Seconds without a message either way before a WebSocket is closed (1001). |
| `WEBSOCKET_MAX_LIFETIME` | `3600.0` | Seconds a WebSocket may stay open (1001); at most 86,400. |
| `WEBSOCKET_MESSAGE_RATE` | `"20/second"` | Messages a client may send per connection, counted in the process; one more closes it (1008). `None`: any number. |
| `SSE_KEEPALIVE` | `15.0` | Seconds without an event before an event stream sends a keep-alive comment. |
| `SSE_MAX_LIFETIME` | `3600.0` | Seconds an event stream may stay open; at most 86,400. The client then connects again. |

## Middleware

The order matters. This table is the order to keep, outermost first. The
project template installs eleven of these, in this order (not the
metrics, CORS, compression, idempotency, or debug toolbar middleware), and
`DEFAULT_MIDDLEWARE` eight (below):

| Middleware | Why here |
| --- | --- |
| `domainkit.http.middleware.ProxyHeadersMiddleware` | First, so everything after it sees the real client address and scheme. It remembers the latest 4,096 addresses it parsed, and whether each is a trusted proxy's. |
| `domainkit.http.metrics.MetricsMiddleware` | Counts every response, including those refused below. |
| `domainkit.http.locale.LocaleMiddleware` | Picks the request's language from `Accept-Language`, before anything below answers, so their errors are translated. Adds `Content-Language` and `Vary: Accept-Language` ([i18n.md](../i18n.md)). |
| `domainkit.http.middleware.SecurityHeadersMiddleware` | Adds the `SECURE_*` headers (and `X-Frame-Options`, a Content-Security-Policy, a Permissions-Policy, and COOP) to every response that does not set them. |
| `domainkit.http.middleware.CORSMiddleware` | Answers preflights before any work. |
| `domainkit.http.middleware.RequestContextMiddleware` | The request id (`X-Request-ID`: a trusted proxy's valid one, per `ACCEPT_REQUEST_ID`, else 32 lowercase hex digits), the access log, and the context that logs and audit rows read. WebSocket connections get one too: method `WS`, the request id sent with the accept, and one access log line when the connection ends (101 if accepted, 403 if closed first). |
| `domainkit.http.deadlines.DeadlineMiddleware` | Starts the request's deadline (`REQUEST_TIMEOUT`) before any work, and answers 503 `deadline_exceeded` when it passes. The deadline is lifted once the response starts ([runtime.md](../runtime.md#request-deadlines)). |
| `domainkit.http.shedding.ConcurrencyLimitMiddleware` | Sheds requests past `MAX_CONCURRENT_REQUESTS` with 503 `overloaded`, before authentication looks anything up; never the probes ([runtime.md](../runtime.md#load-shedding)). |
| `domainkit.http.compression.GZipMiddleware` | Compresses responses of `GZIP_MINIMUM_SIZE` bytes or more, for clients that accept gzip (Django's); not listed by default. Keep it off responses that hold a secret beside text the caller chose (BREACH). |
| `domainkit.http.middleware.AllowedHostsMiddleware` | Refuses unknown `Host` headers with 400. |
| `domainkit.contrib.auth.middleware.AuthenticationMiddleware` | Finds the user from the request's token and makes it the actor, so what follows counts users (auth; see [auth.md](../auth.md)). |
| `domainkit.http.ratelimit.RateLimitMiddleware` | Counts per actor, or per client address (IPv6: per /64), worked out once per request and remembered for the latest 4,096 addresses. A WebSocket handshake counts once; over the limit it gets the 429, or a 1013 close where the server cannot send one. |
| `domainkit.http.websockets.WebSocketLimitsMiddleware` | Caps open WebSockets per process and per caller, queues what they send, limits their messages, closes idle and old ones, and checks their credentials and rules again ([live.md](../live.md)). It comes after the two above, so it counts users, and only handshakes that the rate limit let through. |
| `domainkit.http.middleware.RequestBodyLimitMiddleware` | Refuses bodies over `MAX_REQUEST_BODY_SIZE`, or a route's own `body_limit`, with 413 as they arrive, and bodies slower than `REQUEST_BODY_TIMEOUT` with 408. Closes WebSockets with 1009 on a larger message. |
| `domainkit.contrib.idempotency.middleware.IdempotencyMiddleware` | Last: it reads whole bodies, which the limit above has already bounded. It holds a keyed request's body, and a response it keeps, in memory up to `IDEMPOTENCY.SPOOL_BYTES` (1 MiB), and past that in a temporary file ([idempotency.md](../contrib/idempotency.md)). |
| `domainkit.debug.DebugToolbarMiddleware` | Development only: times each request's SQL, from where it is listed inwards (see [Debug toolbar](settings.md#debug-toolbar)). |

The application also wraps its routes in a middleware of its own,
innermost, that adds the headers of [deprecated
routes](responses.md#versions-and-deprecations).

`DEFAULT_MIDDLEWARE` applies when a settings module sets no `MIDDLEWARE`.
It holds eight: proxy headers, security headers, request context,
deadlines, the concurrency cap, allowed hosts, WebSocket limits, and the
body limit. To add a middleware, list them all, with the new one at its
place in the table. Here, the defaults with CORS:

```python
MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.CORSMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.deadlines.DeadlineMiddleware",
    "domainkit.http.shedding.ConcurrencyLimitMiddleware",
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
```

A middleware of your own is an ASGI class that takes `app`
([Extension points](extension-points.md)).

### Security headers

`SecurityHeadersMiddleware` adds these headers to every response that
does not set them itself:

```text
Cache-Control: no-store
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()
Cross-Origin-Opener-Policy: same-origin
```

It adds `Strict-Transport-Security` once `SECURE_HSTS_SECONDS` is set.

An API answers data, so its policy lets nothing in an answer load, run,
or be framed. Should a browser ever render an answer (a content type
mistaken, a route that answers HTML), no script runs. A route that
answers a page sets its own policy, which is kept:
`domainkit.http.uploads.serve` sandboxes the files it serves, and the
documentation pages have their own policies.

`/docs` and `/redoc` (`API_DOCS`) load Swagger UI 5.33.1 and ReDoc 2.5.4
from copies in the package, under `/docs/assets/`, never from a CDN. A
page that runs a CDN's script in the API's origin would hand that CDN
whatever the origin holds. `/docs` allows scripts and styles from the
origin only, and no inline script. `/redoc` adds the inline styles ReDoc
sets and the `blob:` worker its search runs in
(`domainkit.http.apidocs.SWAGGER_POLICY`, `REDOC_POLICY`). The Redocly
logo in ReDoc's footer, which ReDoc fetches from Redocly's CDN, is
refused. The pages are on with `DEBUG`, and off in a deployment unless
`API_DOCS = True` (ADR 0086).

## See also

- [Server limits](servers.md): the limits the server itself must keep.
- [Errors and responses](responses.md): what the errors above look like.
- [Checks](checks.md): the warnings about these settings.
- [Runtime](../runtime.md): deadlines, load shedding, and outgoing calls.

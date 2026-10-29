# HTTP: settings, middleware, and live connections

What the application accepts and answers, and the middleware that holds it to that.

## HTTP

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost`, `127.0.0.1`, `[::1]` | Accepted `Host` values; `*.example.com` matches subdomains. A `Host` that is not a name, an IPv4 address, or a bracketed IPv6 address (with an optional port) is refused before matching. A wildcard `*` is refused in staging and production. |
| `API_DOCS` | `None` | Serve `/docs`, `/redoc`, and `/openapi.json`; `None` follows `DEBUG`, so a deployment serves them only with `API_DOCS = True`. The pages load Swagger UI and ReDoc from domainkit's own copies under `/docs/assets/`, never a CDN, with a Content-Security-Policy of their own ([Security headers](#security-headers)). `check` warns when it is on in staging and production. |
| `OPENAPI_OPERATION_IDS` | `"fastapi"` | `"route_name"`: each operation id is the route's name, stable when routers move (see [The API surface](responses.md#the-api-surface)). `init` sets it for new projects. |
| `ERROR_FORMAT` | `"domainkit"` | `"problem+json"`: errors are RFC 9457 problem details (see [Errors](responses.md#errors)). |
| `ERROR_TYPE_BASE_URL` | `None` | With problem details, each code's `type` is this absolute URL plus the code (`https://errors.example.com/` gives `https://errors.example.com/not_found`); unset, it is `about:blank`. |
| `MAX_REQUEST_BODY_SIZE` | `1048576` | Bytes; larger bodies get 413, whether declared or streamed. A larger WebSocket message, text counted in UTF-8 bytes, closes the connection with 1009. A route with `body_limit(size)` (1 byte to 1 GiB) is held to its own limit instead, above or below this one ([uploads.md](../uploads.md#body-limits-per-route)). |
| `REQUEST_BODY_TIMEOUT` | `10.0` | Seconds (at most 3600) the application may wait for a request's body, all its chunks together (`RequestBodyLimitMiddleware`); past it the request is answered 408 `request_timeout` and the connection closed. Time spent working between chunks, or after the response starts, does not count. |
| `REQUEST_TIMEOUT` | `30.0` | Seconds (at most 3600) an HTTP request may take until its response starts (`DeadlineMiddleware`); past it the work is cancelled and answered 503 `deadline_exceeded`. A route sets its own with `RequestTimeout`. `None`: no deadline ([runtime.md](../runtime.md#request-deadlines)). Below `IDEMPOTENCY.LEASE_SECONDS` where idempotency is installed. |
| `MAX_CONCURRENT_REQUESTS` | `1000` | HTTP requests each event loop works on at once (`ConcurrencyLimitMiddleware`), at most 100,000; a request waiting for its body is not counted meanwhile. `None`: no cap ([runtime.md](../runtime.md#load-shedding)). |
| `MAX_QUEUED_REQUESTS` | `100` | Requests past the cap that may wait for a slot (at most 100,000); the next are answered 503 `overloaded` at once. |
| `REQUEST_QUEUE_TIMEOUT` | `1.0` | Seconds (at most 60) a request past the cap waits for a slot before it is answered 503 `overloaded`. |
| `CONCURRENCY_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted against the cap, so probes and scrapes are never refused; compared without the root path the application is mounted at (`--root-path`). |
| `HTTP_CLIENT` | see [Outgoing HTTP calls](#outgoing-http-calls) | Defaults of outgoing calls through `domainkit.http.client`. |
| `GUARD_LOOKUP_THREADS` | `16` | Threads (1 to 256) that look host names up for guarded calls and webhooks, in each process. One owner (a request's user or address, or a webhook delivery's subscription) may hold a quarter of them, so hosts one owner chose that never answer leave the rest to others ([runtime.md](../runtime.md#outgoing-http-calls)). |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take (at most 60). |
| `READINESS_CACHE_SECONDS` | `1.0` | How long a `/ready` result is reused, from when its checks ended; probes that come while checks run wait for them, so probe floods cannot load the database. |
| `READINESS_DETAILS` | `False` | Name failing components in `/ready` responses (always logged). A group of checks that raises instead of reporting is named as its group (`domains`, `databases`, `ledgers`, `caches`), logged, and kept like any failed round: a 503, never a 500. |
| `ACCESS_LOG_QUIET_PATHS` | `/health`, `/ready` | Successful requests to these paths are logged at DEBUG; compared without the root path (`--root-path`). |
| `ACCESS_LOG_SAMPLE_RATE` | `1.0` | Share of access lines written, from 0 to 1: `0.1` writes about one request in ten, each drawn on its own. Responses from 400 up (client errors, refusals, and server errors), requests without a response, and requests that take `ACCESS_LOG_SLOW_DURATION` or longer are always written, so only successes and redirects are sampled; `domainkit.security` and the audit trail are never sampled ([runtime.md](../runtime.md#sampling-the-access-log)). |
| `ACCESS_LOG_SLOW_DURATION` | `1.0` | Seconds (at most 3600): a request this slow or slower has its access line written whatever `ACCESS_LOG_SAMPLE_RATE` says. |
| `ACCESS_LOG_PATH` | `"template"` | The path access lines, the request middleware's error lines, error reports, and the request context show: the matched route's template (`/accounts/{account_id}`), or `unmatched`; `"raw"` shows the path as sent, ids and tokens included ([runtime.md](../runtime.md#paths-in-logs)). |
| `TRUSTED_PROXIES` | `()` | Networks (`10.0.0.0/8`) whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed, and `"unix"` for the proxy on the other end of a unix socket (`uvicorn --uds`, `granian --uds`), which has no address (uvicorn reports no client, Granian an empty host). The client is found by walking `X-Forwarded-For` from the right, across every line of the header, through trusted proxies: it is the first hop that is not one, and nothing left of it is read. Hops may carry a port. A hop there that is no address (a proxy's `unknown`) counts the client as the last proxy passed, logged on `domainkit.security`. The scheme is the `PROXY_SCHEME_HEADER` value at the client's place from the right, or the right-most one when there are fewer. Empty: the socket peer is the client. |
| `PROXY_SCHEME_HEADER` | `"X-Forwarded-Proto"` | The header in which the trusted proxies say the client's scheme (a header name). `None`: the scheme is never taken from a header. Set it to `None` when they do not set it (HAProxy's `option forwardfor` alone, an NLB), or a client's own `X-Forwarded-Proto` would be read, and choose whether cookies are `Secure`. `None` holds only where the server reads no proxy header itself: uvicorn, unless run with `--no-proxy-headers`, sets the scheme from `X-Forwarded-Proto` before domainkit sees the request, for every request from `FORWARDED_ALLOW_IPS` (`127.0.0.1` by default: a proxy on the same host). Granian reads none. |
| `ACCEPT_REQUEST_ID` | `"proxies"` | Whose `X-Request-ID` names a request (in log lines, error reports, audit rows, and outgoing calls): `"proxies"`, only a trusted proxy's (`TRUSTED_PROXIES`, a unix socket's peer with `"unix"`), as `ProxyHeadersMiddleware` judged the connection; `"anyone"`, any caller's, as up to 0.10; `"never"`, nobody's. Of several lines, the last is read: the nearest proxy's. The proxy must set the header, overwriting a client's (nginx: `proxy_set_header X-Request-ID $request_id;`) or adding its line after it (HAProxy's `unique-id-header`), or a client's own passes through it: set `"never"` if it does neither. Up to 128 letters, digits, `.`, `_`, `:`, or `-`, starting with a letter or digit; another value, or none, gets a new id (32 hex digits). A caller's value is dropped, not logged ([operations.md](../operations.md#request-ids)). |
| `RATE_LIMIT` | `None` | Requests per caller, such as `"300/minute"` (`second`, `minute`, `hour`, `day`). `None` disables it. |
| `RATE_LIMIT_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted; compared without the root path (`--root-path`). |
| `RATE_LIMIT_BACKEND` | `domainkit.http.ratelimit.MemoryBackend` | Where counts live. The default is per process, and keeps the 100,000 most recently seen callers; one seen longer ago starts afresh. Behind several replicas, use `domainkit.http.ratelimit.RedisBackend`: one count per caller in Redis, on the server's clock. If Redis fails, or refuses the count (out of memory, a read-only replica after a failover), requests are let through, and Redis is skipped for the cache's `FAILURE_COOLDOWN`; password and code checks are refused meanwhile instead, in staging and production (`AUTH.THROTTLE_FAIL_CLOSED`), which also refuse to start with attempts counted in each process (`auth.E105`) ([auth.md](../auth/logging-in.md#logging-in-and-out)). |
| `RATE_LIMIT_CACHE` | `"default"` | The Redis cache `RedisBackend` counts in; keys are `<KEY_PREFIX>:ratelimit:<caller>`. |
| `RATE_LIMIT_FALLBACK_REPLICAS` | `None` | 1 to 1024: while Redis is down, `RedisBackend` counts each caller in its own process at `RATE_LIMIT` divided by this many replicas (at most `FALLBACK_KEYS`, 10,000, callers; a caller who still owes requests is never forgotten, and while all of them do, new callers are refused); `None` lets requests through, as before. |
| `RATE_LIMIT_IPV6_PREFIX` | `64` | IPv6 clients are counted per network of this prefix length (1 to 128), since one customer usually holds a whole /64; login throttling counts the same way. IPv4 clients are counted per address. |
| `METRICS_TOKEN` | `None` | Bearer token for `/metrics`. Without it, `/metrics` answers 403 in staging and production. |
| `SECURE_HSTS_SECONDS` | `0` | `Strict-Transport-Security` max-age; 0 omits the header. |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `False` | Adds `includeSubDomains`. |
| `SECURE_CONTENT_TYPE_NOSNIFF` | `True` | `X-Content-Type-Options: nosniff`. |
| `SECURE_REFERRER_POLICY` | `"no-referrer"` | `Referrer-Policy`: one of its eight policies (`no-referrer`, `same-origin`, `strict-origin-when-cross-origin`, ...), or several, comma-separated; anything else, a line break included, is refused at startup. `None` omits it. |
| `X_FRAME_OPTIONS` | `"DENY"` | Or `"SAMEORIGIN"`. |
| `SECURE_CONTENT_SECURITY_POLICY` | `"default-src 'none'; frame-ancestors 'none'"` | `Content-Security-Policy` of every response that sets none: nothing an answer holds may load, run, or be framed. Printable ASCII on one line, at most 4096 characters; `None` omits it. |
| `SECURE_PERMISSIONS_POLICY` | `"accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"` | `Permissions-Policy` of every response that sets none: those powerful browser features off. Bounded as above; `None` omits it. |
| `SECURE_CROSS_ORIGIN_OPENER_POLICY` | `"same-origin"` | `Cross-Origin-Opener-Policy`; or `"same-origin-allow-popups"`, `"unsafe-none"`; `None` omits it. |
| `CORS_ALLOWED_ORIGINS` | `()` | Origins allowed to call the API from a browser: `*`, or a scheme and host (and port), without a path, a space, or a comma. |
| `CORS_ALLOWED_METHODS` | `GET POST PUT PATCH DELETE` | Methods, or `*`: HTTP tokens of at most 64 characters, no space or line break. |
| `CORS_ALLOWED_HEADERS` | `Authorization Content-Type X-Request-ID` | Header names, or `*`: HTTP tokens of at most 64 characters, no space or line break. |
| `CORS_ALLOW_CREDENTIALS` | `False` | Cannot be combined with origin `*`. |

## Outgoing HTTP calls

`HTTP_CLIENT` keys: the defaults of every `domainkit.http.client.HttpClient`
(the `httpclient` extra), which may change its own timeout and retries. See
[runtime.md](../runtime.md#outgoing-http-calls).

| `HTTP_CLIENT` key | Default | Meaning |
| --- | --- | --- |
| `TIMEOUT` | `10.0` | Seconds (at most 3600) one call may take, its retries included, and never past the deadline of the work it serves. |
| `CONNECT_TIMEOUT` | `5.0` | Seconds to open a connection. |
| `RETRIES` | `2` | Retries (0 to 10) of a call that failed in a way worth trying again. |
| `BACKOFF` | `0.1` | Seconds before the first retry; the bound doubles for each next one, and the wait is random below it. |
| `MAX_BACKOFF` | `2.0` | The longest wait before a retry (at most 600 seconds); a longer `Retry-After` ends the retries. |
| `BREAKER_FAILURES` | `5` | Failures in a row that open a host's circuit: its calls then fail at once with `CircuitOpen`. |
| `BREAKER_COOLDOWN` | `30.0` | Seconds (at most 3600) a host's circuit stays open; then one call at a time tries it. A client made with `breaker=False` keeps no circuits. |
| `MAX_CONNECTIONS` | `100` | Connections per client and event loop. |
| `MAX_KEEPALIVE_CONNECTIONS` | `20` | Idle connections each client keeps for the next calls. |
| `MAX_RESPONSE_BYTES` | `10485760` | The largest answer read (at most 1 GiB), counted as it comes and after decompression; a longer one raises `ResponseTooLarge`. |
| `USER_AGENT` | `"domainkit"` | The `User-Agent` of every call (printable ASCII). |
| `CA_BUNDLE` | `None` | CA certificates of the servers called; the system's (or httpx2's) when unset. Guarded clients never trust the CAs `SSL_CERT_FILE` or `SSL_CERT_DIR` name, unless listed here. |

## Live connections

WebSockets and Server-Sent Events streams are capped, queued, timed, and
re-authorized while they stay open; see [live.md](../live.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `LIVE_MAX_CONNECTIONS` | `1000` | Open WebSockets and event streams per process (at most 1,000,000); one more is refused with 503 and `Retry-After` (a WebSocket where the server cannot answer a handshake with a status: a 1013 close). |
| `LIVE_MAX_CONNECTIONS_PER_CALLER` | `10` | The same per user, or per address for anonymous callers (IPv6 per `RATE_LIMIT_IPV6_PREFIX` network). At most `LIVE_MAX_CONNECTIONS` (and 1,000,000). |
| `LIVE_SEND_QUEUE` | `64` | Messages or events that may wait for one client (1 to 10,000); a sender waits while they do. Live events count only those the client may see; 16 times as many may wait for its rule ([live.md](../live.md#live-committed-events)). |
| `LIVE_SEND_TIMEOUT` | `10.0` | Seconds one message may take to reach the client; a slower client is closed (1013). |
| `LIVE_REAUTH_INTERVAL` | `60.0` | Seconds between re-checks of an open connection's credentials and its route's rules (each between three quarters of it and all of it; at most 3,600); closed with 1008 when they fail. `None` turns them off; `check` warns about that in staging and production. What re-checks cannot do: [live.md](../live.md#what-re-checks-cannot-do). |
| `WEBSOCKET_IDLE_TIMEOUT` | `300.0` | Seconds without a message either way before a WebSocket is closed (1001). |
| `WEBSOCKET_MAX_LIFETIME` | `3600.0` | Seconds a WebSocket may stay open (1001). |
| `WEBSOCKET_MESSAGE_RATE` | `"20/second"` | Messages a client may send per connection, counted in the process; one more closes it (1008). `None`: any. |
| `SSE_KEEPALIVE` | `15.0` | Seconds without an event before an event stream sends a keep-alive comment. |
| `SSE_MAX_LIFETIME` | `3600.0` | Seconds an event stream may stay open. |

## Middleware

The order matters; this is the order to keep, outermost first. The project
template installs eleven of these, in this order (not the metrics, CORS,
idempotency, or debug toolbar middleware), and `DEFAULT_MIDDLEWARE` eight
(below):

| Middleware | Why here |
| --- | --- |
| `domainkit.http.middleware.ProxyHeadersMiddleware` | First, so everything after it sees the real client address and scheme. Remembers the latest 4,096 addresses it parsed, and whether each is a trusted proxy's. |
| `domainkit.http.metrics.MetricsMiddleware` | Counts every response, including ones refused below. |
| `domainkit.http.locale.LocaleMiddleware` | Picks the request's language from `Accept-Language`, before anything below answers, so their errors are translated; adds `Content-Language` and `Vary: Accept-Language` ([i18n.md](../i18n.md)). |
| `domainkit.http.middleware.SecurityHeadersMiddleware` | Adds the `SECURE_*` headers (and `X-Frame-Options`, a Content-Security-Policy, a Permissions-Policy, and COOP) to every response that does not set them. |
| `domainkit.http.middleware.CORSMiddleware` | Answers preflights before any work. |
| `domainkit.http.middleware.RequestContextMiddleware` | Request id (`X-Request-ID`: a trusted proxy's valid one, per `ACCEPT_REQUEST_ID`, else 32 lowercase hex digits), access log, the context that logs and audit rows read. WebSocket connections get one too: method `WS`, the request id sent with the accept, one access log line when the connection ends (101 if accepted, 403 if closed first). |
| `domainkit.http.deadlines.DeadlineMiddleware` | Starts the request's deadline (`REQUEST_TIMEOUT`) before any work, and answers 503 `deadline_exceeded` when it passes; lifted once the response starts ([runtime.md](../runtime.md#request-deadlines)). |
| `domainkit.http.shedding.ConcurrencyLimitMiddleware` | Sheds requests past `MAX_CONCURRENT_REQUESTS` with 503 `overloaded`, before authentication looks anything up; never the probes ([runtime.md](../runtime.md#load-shedding)). |
| `domainkit.http.middleware.AllowedHostsMiddleware` | Refuses unknown `Host` headers with 400. |
| `domainkit.contrib.auth.middleware.AuthenticationMiddleware` | Finds the user from the request's token and makes it the actor, so what follows counts users (auth; see [auth.md](../auth.md)). |
| `domainkit.http.ratelimit.RateLimitMiddleware` | Counts per actor, or per client address (IPv6: per /64), worked out once per request and remembered for the latest 4,096 addresses. A WebSocket handshake counts once; over the limit it gets the 429, or a 1013 close where the server cannot send one. |
| `domainkit.http.websockets.WebSocketLimitsMiddleware` | Caps open WebSockets per process and per caller, queues what they send, limits their messages, closes idle and old ones, and re-checks their credentials and rules ([live.md](../live.md)). After the two above, so it counts users and only handshakes the rate limit let through. |
| `domainkit.http.middleware.RequestBodyLimitMiddleware` | Refuses bodies over `MAX_REQUEST_BODY_SIZE`, or a route's own `body_limit`, with 413 as they arrive, and bodies slower than `REQUEST_BODY_TIMEOUT` with 408; closes WebSockets with 1009 on a larger message. |
| `domainkit.contrib.idempotency.middleware.IdempotencyMiddleware` | Last: reads whole bodies, which the limit above has already bounded. Holds a keyed request's body, and a response it keeps, in memory up to `IDEMPOTENCY.SPOOL_BYTES` (1 MiB), past them in a temporary file ([idempotency.md](../contrib/idempotency.md)). |
| `domainkit.debug.DebugToolbarMiddleware` | Development only: times each request's SQL, from where it is listed inwards (see [Debug toolbar](settings.md#debug-toolbar)). |

The application also wraps its routes in a middleware of its own,
innermost, that adds the headers of [deprecated
routes](responses.md#versions-and-deprecations).

`DEFAULT_MIDDLEWARE` (proxy headers, security headers, request context,
deadlines, the concurrency cap, allowed hosts, WebSocket limits, body
limit) applies when a settings module sets no `MIDDLEWARE`.

### Security headers

`SecurityHeadersMiddleware` adds these to every response that does not
set them itself:

```
Cache-Control: no-store
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()
Cross-Origin-Opener-Policy: same-origin
```

and `Strict-Transport-Security` once `SECURE_HSTS_SECONDS` is set. An API
answers data, so its policy lets nothing in an answer load, run, or be
framed: should a browser ever render one (a content type mistaken, a route
that answers HTML), no script runs. A route that answers a page sets its
own policy, which is kept: `domainkit.http.uploads.serve` sandboxes the
files it serves, and the documentation pages have theirs.

`/docs` and `/redoc` (`API_DOCS`) load Swagger UI 5.33.1 and ReDoc 2.5.4
from copies in the package, under `/docs/assets/`, never from a CDN: a
page that runs a CDN's script in the API's origin would hand that CDN
whatever the origin holds. `/docs` allows scripts and styles from the
origin only, and no inline script; `/redoc` adds the inline styles ReDoc
sets and the `blob:` worker its search runs in
(`domainkit.http.apidocs.SWAGGER_POLICY`, `REDOC_POLICY`). The Redocly logo
in ReDoc's footer, which it fetches from Redocly's CDN, is refused. The
pages are on with `DEBUG`, and off in a deployment unless `API_DOCS =
True` (ADR 0086).

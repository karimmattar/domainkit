# Performance

## The HTTP stack does each piece of work once

A request through the middleware stack used to pay for its routers twice
at each level of nesting, parse the client address two to four times, add
security headers through `MutableHeaders`, look metric labels up four
times, build a UUID for its id, and set each response header through a
fresh `MutableHeaders`. Now:

- **Routes are matched once, at the top.** `get_asgi_application`
  flattens the routers `ROOT_ROUTER` includes (`FLATTEN_ROUTERS`, ADR
  0060): FastAPI kept each `include_router` call nested, matched a request
  twice per level, and built the route's handler again for every request.
  Everything the routers gave their routes is kept, and the OpenAPI
  document does not change ([reference.md](reference.md#included-routers)).
- **The client address is parsed once per request**, for the
  refused-credential hold and the rate limit together
  (`scope["domainkit.address"]`), and the groups of the latest 4,096
  addresses are remembered. Only IPv6 addresses read
  `RATE_LIMIT_IPV6_PREFIX`. `ProxyHeadersMiddleware` remembers the latest
  4,096 addresses it parsed, and whether each is a trusted proxy's.
- **Headers are set raw.** Security headers are added in one append, and
  the request id and `RateLimit-*` headers without `MutableHeaders`; the
  bytes sent are the same, in the same order.
- **Request ids come from `os.urandom(16).hex()`**: the 32 lowercase hex
  digits of `uuid4().hex`, at a quarter of the cost.
- **`MetricsMiddleware` keeps each series' handle**, for at most 4,096
  series, instead of calling `labels()` four times per request.

Each cache is bounded, holds what a pure function of its key gives (an
address's group, a proxy's trust under its own networks, a series'
handle), and never answers for another key; addresses longer than 64
characters (an IPv6 zone can be any length) are never kept.

### What it saves

Python calls per request, counted with cProfile (exact), `GET /hello`
answering `{"status": "ok"}` in process, against 0.8.0 (`a686177`), on
FastAPI 0.141.1 and on 0.143.1, the release this branch ships with. "T7"
is the re-audit's `init` stack of 0.6 (seven middleware); "init" is the
eleven `domainkit init` writes now, with the template's routes in front
of `/hello` (health, auth, and the auth admin).

| Stack and request | 0.141.1: 0.8.0 | routers flattened | all changes | 0.143.1: 0.8.0 | all changes |
| --- | ---: | ---: | ---: | ---: | ---: |
| No middleware, template routes, `/hello` (1 level) | 1,638 | 434 | 434 | 1,674 | 500 |
| No middleware, template routes, `/v1/hello` (2 levels) | 1,760 | 442 | 442 | 1,796 | 508 |
| 5 middleware (the re-audit's default) | 388 | 274 | 226 | 424 | 292 |
| `DEFAULT_MIDDLEWARE` (8) | 476 | 362 | 314 | 512 | 380 |
| T7 | 749 | 509 | 405 | 785 | 471 |
| T7, behind a trusted proxy | 844 | 604 | 427 | 880 | 493 |
| T7, an IPv6 client | 923 | – | 409 | 959 | 475 |
| init (11) | 2,019 | 815 | 711 | 2,055 | 777 |
| init, behind a trusted proxy | 2,114 | 910 | 733 | 2,150 | 799 |
| init, an IPv6 client | 2,193 | – | 715 | 2,229 | 781 |
| init, `/v1/hello` (2 levels) | 2,141 | 823 | 719 | 2,177 | 785 |
| init, `/auth/me` without a token (401, 2 levels) | 1,729 | 1,161 | 1,059 | 1,745 | 1,105 |

Time per request, the same requests: CPU time of the process, best of
fifteen runs of 10,000 in three rounds of fresh processes, 0.8.0 and this
branch alternating, with the range of each process's best; on a shared
4-core machine at a load of 5 to 8, so the absolute numbers are high (the
re-audit measured T7 at 166 µs on a quiet one) and wall time spread more.

| Stack and request | 0.8.0 µs | now µs | change |
| --- | ---: | ---: | ---: |
| 5 middleware | 92.7 (93–103) | 62.2 (62–64) | −33% |
| `DEFAULT_MIDDLEWARE` (8) | 125.2 (125–139) | 90.4 (90–97) | −28% |
| T7 | 171.4 (171–188) | 107.3 (107–111) | −37% |
| T7, behind a trusted proxy | 192.2 (192–205) | 116.1 (116–121) | −40% |
| T7, an IPv6 client | 242.8 (243–258) | 106.6 (107–120) | −56% |
| init (11) | 301.0 (301–324) | 170.8 (171–192) | −43% |
| init, behind a trusted proxy | 339.1 (339–359) | 185.6 (186–192) | −45% |
| init, `/v1/hello` (2 levels) | 332.5 (333–343) | 173.9 (174–181) | −48% |
| init, `/auth/me` without a token (401) | 421.1 (421–477) | 321.8 (322–340) | −24% |

One route nested in FastAPI alone (the re-audit's `nest_bench.py`, adapted
to compare each depth nested and flattened in one process):

| Depth | FastAPI 0.141.1, nested | flattened | 0.143.1 and 0.158.2, nested | flattened |
| --- | ---: | ---: | ---: | ---: |
| 0 | 168 calls | 168 | 234 | 234 |
| 1 | 280 | 168 | 316 | 234 |
| 2 | 370 | 168 | 406 | 234 |
| 3 | 492 | 168 | 528 | 234 |
| 1, behind 20 sibling routes | 1,078 | 320 | 1,114 | 386 |
| 2, behind 20 sibling routes | 1,605 | 320 | 1,641 | 386 |

On 0.141.1, the best of seven alternating rounds of 20,000 requests took
72.7 µs nested and 39.8 µs flattened at depth 2, and 176.0 and 67.9 µs
behind 20 sibling routes (wall time; medians 85.9 and 48.3, 204.4 and
80.1 µs).

What the pieces cost alone (`timeit`, best of 5, at 0.8.0):
`MutableHeaders` security headers 4.3 µs, raw 0.45 µs; `uuid4().hex`
1.96 µs, `os.urandom(16).hex()` 0.45 µs; `labels()` 0.86 µs, a kept
handle 0.05 µs; `client_group` of an IPv4 address 2.5 µs and of an IPv6
one 25 µs, remembered 0.06 µs; the proxy's trust of an address 2.6 µs.

### Measuring it

The harness feeds hand-built scopes straight into the application, so no
server or network is timed: 1,000 warm-up requests, then five runs of
10,000; the best run counts, and cProfile counts calls over 2,000. To
compare two versions, run each in fresh processes, alternating which goes
first, and read the call counts before the times: on shared machines run
times swing by 15% or more.

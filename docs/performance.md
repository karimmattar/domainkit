# Performance

## JSON responses and logs

0.9 encodes JSON responses with pydantic's encoder and writes the JSON access
log with less work, without changing a byte of either; and a busy service can
sample its access log (ADR 0063). Measured on domainkit 0.8.0 and this
release side by side, with the re-audit's harness (`GET /hello` fed straight
into the application, 4 shared vCPUs).

**Responses.** `domainkit.http.JSONResponse` gives the bytes Starlette's
gave, for 41% to 52% fewer instructions. Resource pages, items, and
batches, error bodies, and readiness answers use it.

| Payload | Instructions, 0.8.0 | Instructions, 0.9 | Time, 0.8.0 | Time, 0.9 |
| --- | ---: | ---: | ---: | ---: |
| `{"status": "ok"}` | 21,879 | 10,678 | 2.0 µs | 0.9 µs |
| an error body (211 bytes) | 38,678 | 22,438 | 3.6 µs | 1.8 µs |
| one resource (419 bytes) | 60,021 | 29,000 | 5.7 µs | 2.3 µs |
| a page of 50 resources (20 KB) | 1,939,596 | 991,051 | 179 µs | 69 µs |
| 200 orders with lines (159 KB) | 15,992,955 | 9,488,955 | 1,259 µs | 620 µs |

A request that answers the page of 50, with no middleware, costs 1,302,835
instructions instead of 2,155,451: 40% fewer. One that answers an error body
costs 382,839 instead of 400,160.

**Access log.** The JSON access line costs about 154,000 instructions instead
of 181,000, on the default middleware and on the re-audit's seven-middleware
`init` template stack alike (15% fewer; a light request is about
470,000). Of that, the formatter
takes 45,278 instructions a line instead of 69,900, and the copy for the
logging queue 16,302 instead of 26,844; the rest is the standard library's
`LogRecord`, the handler's lock, the queue, and the thread. A line the
logger's level drops is no longer built: with the access log off, a
request costs 468,844 instructions instead of 477,115.

**Sampling.** `ACCESS_LOG_SAMPLE_RATE = 0.1` writes about one ordinary
request in ten, and every error, refusal, and slow request
([runtime.md](runtime.md#sampling-the-access-log)):

| Default middleware | Instructions a request |
| --- | ---: |
| 0.8.0, every access line | 659,160 |
| 0.9, every access line | 623,077 |
| 0.9, `ACCESS_LOG_SAMPLE_RATE = 0.1` | 486,281 |
| 0.9, no access log | 468,844 |

The timer sees sampling's gain, not the formatter's: in two interleaved
series of 20 runs on a busy shared machine, a request with every line took
160 to 170 µs at best on either version, 112 to 117 µs sampling at 0.1,
and 91 to 111 µs without the log. Over HTTP (one uvicorn worker with
uvloop and httptools on one core, 64 connections, best of three runs at a
load average of 5 to 7), the default middleware served 3,643 requests a
second with every line on 0.8.0, 3,904 on 0.9, and 5,802 sampling at 0.1.

These counts are on FastAPI 0.141.1. On FastAPI 0.143.1 and SQLAlchemy
2.1.4 a whole request costs about 3% more (FastAPI's telemetry hooks), and
the savings hold within 2%: with every access line a request costs 674,619
instructions on 0.8.0 and 643,267 on 0.9 (the line 181,741 and 156,844),
505,391 sampling at 0.1, and 486,423 without the log; the page of 50 costs
1,314,338 instead of 2,168,067. FastAPI 0.143.1 still chooses between
pydantic's encoder and `jsonable_encoder` as 0.141.1 does, so the bytes are
the same on both.

### How it was measured

- **Instructions** are counted by valgrind's callgrind, every thread
  included: each scenario runs with 100 and with 600 requests after the
  same warm-up, and the difference, divided by 500, is one request
  (`PYTHONHASHSEED=0`; two runs of one scenario differed by 0.25%).
  Instructions do not move with the machine's load; times do.
- **Times** are the best of 20 runs of 5,000 requests, the versions
  interleaved in fresh processes; on a shared machine runs of one version
  spread by 15% or more, so differences under that are noise.
- **Encodings** are of the dicts `model_dump(mode="json")` gives, best of 7
  with `timeit`, and counted with callgrind the same way.

### Making it faster

- Sample the access log when it costs more than it tells: at 0.1 its cost
  nearly disappears, and the lines that matter stay.
- Keep the access log, and other loggers, at a level that drops what you do
  not read: a dropped access line costs nothing.
- Declare a response model (a return annotation) on routes that return
  plain dicts: FastAPI then encodes them with pydantic in Rust and skips
  `jsonable_encoder`, which costs about 1 ms for 50 resources. Mind that
  `jsonable_encoder` makes a float of a bare `Decimal`, and pydantic a
  string. Where the bytes must stay as they are, give the route
  `response_class=JSONResponse` instead ([reference.md](reference.md#json-responses)).

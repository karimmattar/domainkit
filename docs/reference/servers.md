# Server limits

The application limits request bodies, WebSocket messages, request rates,
how long a request may take (`REQUEST_TIMEOUT`), and how many requests it
works on at once (`MAX_CONCURRENT_REQUESTS`). But some requests must be
stopped before the application sees them: slow ones and oversized ones.
That is the job of the server, and of the proxy in front of it. This page
gives the server options to set, and why.

On this page you find:

- `manage.py serve`, which starts either server with these options;
- the HTTP versions Granian speaks, and a worker that fails as it starts;
- the command lines for uvicorn and Granian;
- which option limits what, on each server;
- how the server's limits and the application's fit together;
- what changes from uvicorn to Granian;
- WebSocket message sizes, proxy headers, and unix sockets.

## The proxy in front

Run the server behind a reverse proxy or a load balancer. Let it limit
the size of headers, and the time a client takes to send headers and
body. With nginx, these are `large_client_header_buffers`,
`client_header_timeout`, and `client_body_timeout`. Then set the server's
own limits, as below.

Keep the proxy's idle timeout for its connections to the server below
the time the server keeps an idle connection open (`SERVE["KEEP_ALIVE"]`,
uvicorn's `--timeout-keep-alive`, Granian's `--http1-header-read-timeout`).
Otherwise the proxy now and then sends a request on a connection the
server is closing, and answers it with a 502. The proxy's setting is
nginx's `keepalive_timeout` in the `upstream` block (60 seconds unless
set), an AWS Application Load Balancer's idle timeout (60 seconds unless
set), Envoy's `idle_timeout` for a cluster (an hour unless set), or
Traefik's `idleConnTimeout` (90 seconds unless set). Google Cloud's
external Application Load Balancer keeps idle connections to the server
600 seconds, which cannot be changed, and asks for 620 on the server:
Granian keeps one 60 seconds at most, so serve with uvicorn there.

Set `PROXY_IDLE_TIMEOUT` to the proxy's idle timeout, in seconds. In
staging and production, `check` then warns (`servers.W003`) when the
server does not keep an idle connection longer, with both numbers and
what to change:

```text
  Warning: servers.W003: The proxy keeps an idle connection to the server open 60 seconds (PROXY_IDLE_TIMEOUT), and Granian closes it after 60: the proxy may send a request on a connection as Granian closes it, and answer that request 502.
    Hint: Granian keeps an idle connection 60 seconds at most: lower the proxy's idle timeout below 60 seconds.
```

`check` finds the server as for [WebSocket message
size](#websocket-message-size). A server its variables name keeps an
idle connection `UVICORN_TIMEOUT_KEEP_ALIVE` seconds (5 unless set),
`GRANIAN_HTTP1_HEADER_READ_TIMEOUT` milliseconds (30,000 unless set), or,
under gunicorn's uvicorn worker, gunicorn's `--keep-alive` seconds in
`GUNICORN_CMD_ARGS` (2 unless set). Where no variable names a server,
`check` judges the server `serve` starts, with `SERVE["KEEP_ALIVE"]`.

domainkit runs on [uvicorn](https://www.uvicorn.org/) or
[Granian](https://github.com/emmett-framework/granian)
(`pip install 'domainkit[granian]'`). [runtime.md](../runtime.md#servers)
compares them.

## The `serve` command

`python manage.py serve` starts the production server for
`ASGI_APPLICATION`, with the options of the command lines below:
Granian where the `granian` extra is installed, else uvicorn. It never
reloads; `runserver` is the development server. It listens on
`0.0.0.0:8000` unless given an address, or a unix socket (`--uds`)
([commands.md](commands.md#serve-serve-the-project-in-production)).
`SERVE` sets the server and its limits:

```python
SERVE = {"WORKERS": 4, "KEEP_ALIVE": 75, "SERVER": "uvicorn"}
PROXY_IDLE_TIMEOUT = 60  # an AWS load balancer's, unless set
```

| `SERVE` key | Default | Meaning |
| --- | --- | --- |
| `SERVER` | `"auto"` | `"granian"`, `"uvicorn"`, or `"auto"`: Granian where the `granian` extra is installed, else uvicorn. |
| `WORKERS` | `None` | Worker processes, from 1 to 1024: one per core. `None` reads `WEB_CONCURRENCY`, as uvicorn does, else starts one. |
| `MAX_CONNECTIONS` | `1200` | Connections each worker takes at once, idle ones included, at most 100,000: uvicorn's `--limit-concurrency` (past it, 503), Granian's `--backpressure` (past it, the next connection waits in the backlog). Keep it above `MAX_CONCURRENT_REQUESTS` plus `MAX_QUEUED_REQUESTS` ([How the limits fit together](#how-the-limits-fit-together)). |
| `BACKLOG` | `2048` | Connections waiting to be accepted, for all the workers, from 128 to 65,535. |
| `KEEP_ALIVE` | `None` | Seconds an idle connection stays open, from 1 to 3600: uvicorn's `--timeout-keep-alive`, Granian's `--http1-header-read-timeout`. `None`: 5 seconds on uvicorn; on Granian, 60, its most, past which it refuses. Keep it above the proxy's idle timeout ([The proxy in front](#the-proxy-in-front)). |
| `GRACEFUL_TIMEOUT` | `10` | Seconds the requests in flight get to finish once the server is told to stop, from 1 to 600: uvicorn's `--timeout-graceful-shutdown`. Granian kills its workers this plus `SHUTDOWN_TIMEOUT` seconds after (`--workers-kill-timeout`, 25 by default), as their application's shutdown runs in its time. |
| `MAX_HEADER_SIZE` | `16384` | Bytes of a request line and headers, from 8192 to 1,048,576: Granian's `--http1-buffer-size`, which answers 431 past it. uvicorn's `--h11-max-incomplete-event-size`, which its h11 parser applies to a header block still arriving only; its httptools parser, which `uvicorn[standard]` installs and uvicorn prefers, has no such limit, so let the proxy limit headers. |
| `WEBSOCKETS` | `True` | Accept WebSockets. uvicorn reads a message up to `MAX_REQUEST_BODY_SIZE`; Granian reads it whole, at any size ([WebSocket message size](#websocket-message-size)). Set `False` when the API takes none. |
| `HTTP` | `"auto"` | The HTTP versions Granian speaks (`--http`): `"auto"` takes HTTP/1.1, and HTTP/2 without TLS (h2c) from a client that opens with it; `"1"` HTTP/1.1 only, as uvicorn; `"2"` h2c only, for a proxy that speaks it, with `WEBSOCKETS = False` (Granian carries none over HTTP/2). uvicorn speaks HTTP/1.1 only: it refuses `"2"`. *New in 2.5* ([HTTP versions](#http-versions)). |
| `EXIT_ON_FAILED_START` | `False` | Under Granian, a worker that fails as it starts (the application cannot be imported, or its startup fails) stops the server, which exits 3, instead of being started again. A worker that dies once it served is still started again. uvicorn stops so whatever this says. *New in 2.5* ([A worker that fails as it starts](#a-worker-that-fails-as-it-starts)). |

What `serve` sets itself:

- the loop: `EVENT_LOOP`'s, uvloop where installed unless set;
- no proxy header read, and, under uvicorn, no `Server` header
  ([Proxy headers](#proxy-headers), [`SERVER_HEADER`](http.md#the-server-header));
- no access log: `domainkit.access` writes one line per request;
- under uvicorn, `--lifespan on`: a lifespan that raises stops the
  worker, as a startup that fails does. uvicorn's own default, `auto`,
  logs the error at `INFO` as "lifespan unsupported", then serves every
  request a 500. `runserver` passes it too. *New in 2.9*;
- `LOGGING` for the server's own lines, in its main process too, as
  `manage.py logconfig` writes it ([The server's own lines](../runtime.md#the-servers-own-lines));
- under Granian, a worker that dies is started again
  (`--respawn-failed-workers`), and a request's headers are bounded by
  `MAX_HEADER_SIZE` over HTTP/2 as over HTTP/1.1
  (`--http2-max-headers-size`, 16 MiB unless set).

Only the settings are loaded: each worker imports the application, as
under the server's own command line. A deployed application started by
`serve` knows the server's limits from `SERVE`, and logs `servers.W001`
and `servers.W002` as it starts, as under the server's own command line.
`check --deploy`, in a container of its own, reads `SERVE` from the same
settings.

## The command lines

`serve` gives the server these options. To run the server yourself,
uvicorn, one process per core:

```bash
uvicorn config.asgi:application --workers 4 --loop uvloop \
  --limit-concurrency 1200 --backlog 2048 --timeout-keep-alive 5 \
  --timeout-graceful-shutdown 10 \
  --h11-max-incomplete-event-size 16384 --ws-max-size 1048576 \
  --no-proxy-headers --no-server-header --lifespan on
```

Granian, the same, with `LOGGING` written for its own process first:

```bash
python manage.py logconfig --output /tmp/logging.json
granian --interface asgi config.asgi:application --host 0.0.0.0 --port 8000 \
  --workers 4 --loop uvloop --backpressure 1200 --backlog 2048 \
  --http1-header-read-timeout 60000 --http1-buffer-size 16384 \
  --http2-max-headers-size 16384 \
  --workers-kill-timeout 25 --respawn-failed-workers --no-ws \
  --log-config /tmp/logging.json
```

`serve` starts the first with `SERVE = {"SERVER": "uvicorn", "WORKERS":
4}`, and the second with `SERVE = {"WORKERS": 4, "WEBSOCKETS": False}`,
where Granian is installed. Tests hold the lines to what it starts.
[Granian](#granian) says why each option is there.

The `Dockerfile` that `init` writes runs `python manage.py serve`:
uvicorn, or Granian with `init --server granian`, which installs it.
`WEB_CONCURRENCY` sets the number of processes.

## What each option limits

| Concern | `SERVE` key | uvicorn | Granian |
| --- | --- | --- | --- |
| Requests at once, per process | `MAX_CONNECTIONS` | `--limit-concurrency` (more get 503) | `--backpressure` connections, idle ones included (more wait, unanswered) |
| Idle keep-alive connections | `KEEP_ALIVE` | `--timeout-keep-alive` seconds | `--http1-header-read-timeout` milliseconds (30,000 unless set, 60,000 at most) |
| Slow headers | | the proxy | `--http1-header-read-timeout` milliseconds |
| Request line and headers | `MAX_HEADER_SIZE` | `--h11-max-incomplete-event-size` (16 KiB), with h11 only | `--http1-buffer-size` bytes (about half a megabyte unless set; past it, 431) |
| WebSocket message | `WEBSOCKETS` | `--ws-max-size` bytes (16 MiB unless set; `serve`: `MAX_REQUEST_BODY_SIZE`) | no option: `--no-ws` if unused, else the proxy |
| Graceful shutdown | `GRACEFUL_TIMEOUT` | `--timeout-graceful-shutdown` seconds, then open connections are closed, then the application shuts down | no limit unless `--workers-kill-timeout` seconds, the application's shutdown included; then workers are killed |
| A worker that dies | `EXIT_ON_FAILED_START` | `--workers` starts another; one that fails as it starts stops the server | every worker stops and Granian exits 1, unless `--respawn-failed-workers` (`serve` sets it), which starts another, and exits 1 only when it dies within 5.5 seconds of that |
| HTTP versions | `HTTP` | HTTP/1.1 | `--http`: HTTP/1.1, and h2c from a client that opens with it, unless set |
| Headers over HTTP/2 | `MAX_HEADER_SIZE` | | `--http2-max-headers-size` bytes (16 MiB unless set) |
| The `Server` header | | `--no-server-header` leaves it out (`serve` sets it) | no option: `Server: granian` unless the answer has one (`SERVER_HEADER`) |

## How the limits fit together

**Shutdown.** Keep the server's shutdown timeout below the orchestrator's
grace period (30 seconds on Kubernetes), less the time the application
takes to close its resources (`SHUTDOWN_TIMEOUT`). Then a stop never ends
in `SIGKILL`. Granian's `--workers-kill-timeout` counts the application's
shutdown too: give it both, `25` for 10 seconds of requests and the 15 of
`SHUTDOWN_TIMEOUT`.

**Requests at once.** Keep `--limit-concurrency` (Granian:
`--backpressure`) above `MAX_CONCURRENT_REQUESTS` plus
`MAX_QUEUED_REQUESTS`, plus the WebSockets you expect, and, for Granian,
the idle connections the proxy keeps open. It is the backstop. The
application refuses first, with a JSON 503 and `Retry-After`, and it
never refuses the probes in `PROBE_PATHS`. uvicorn's own
503 is plain text, and it refuses probes too.

**Slow bodies.** A request whose body is slow to arrive holds no slot of
`MAX_CONCURRENT_REQUESTS` while it waits. It is answered 408 after
`REQUEST_BODY_TIMEOUT`. But it still holds a connection, the server's
place in `--limit-concurrency`, and what it has sent so far. A buffering
proxy in front is still the better choice: nginx reads the whole body
before it passes the request on (`proxy_request_buffering on`, its
default), within `client_body_timeout` and `client_max_body_size`. So
slow clients cost the proxy, not the application.

**Deadlines.** Keep `REQUEST_TIMEOUT` below the load balancer's idle
timeout (60 seconds on most). Then a slow request gets the application's
503, not a 504 from the balancer.

## Granian

Granian's options are not uvicorn's, and some of uvicorn's have no
counterpart. What follows was checked with Granian 2.8.4, option by
option, from uvicorn's command line above:

- **`--timeout-keep-alive 5`.** Granian keeps an idle connection open as
  long as it waits for the next request's headers:
  `--http1-header-read-timeout`, in milliseconds, 30,000 unless set. It
  refuses more than 60,000. A proxy that keeps idle connections longer
  (Traefik, 90 seconds unless set) finds some of them closed: lower the
  proxy's below Granian's ([The proxy in front](#the-proxy-in-front)).
  The command above gives Granian its most, 60,000, so the proxy's
  defaults need only a small change; the proxy in front limits slow
  headers.
- **`--limit-concurrency 1200`.** `--backpressure` bounds a worker's
  connections, not its requests, and an idle keep-alive connection holds
  its place. Past it, a new connection waits in the backlog, unanswered,
  until a place is free: no 503. Count the proxy's idle connections in
  it ([How the limits fit together](#how-the-limits-fit-together)).
- **`--timeout-graceful-shutdown 10`.** On `SIGTERM` Granian stops each
  worker, and a worker finishes its requests in flight, then the
  application's shutdown, with no limit unless `--workers-kill-timeout`
  says one. A worker still running then is killed: the application's
  shutdown does not run, or stops where it is, so log lines and error
  reports still queued are lost, and Granian still exits 0. Give it the
  requests' time and `SHUTDOWN_TIMEOUT` together: `25`.
- **`--no-server-header`.** No option: Granian writes `Server: granian`
  on every answer that has no `Server` header. Set `SERVER_HEADER` to
  give every answer one of yours instead
  ([The server header](http.md#the-server-header)).
- **`--ws-max-size 1048576`.** No option: Granian reads each WebSocket
  message whole, at any size, before `MAX_REQUEST_BODY_SIZE` refuses it.
  Turn WebSockets off (`--no-ws`) when the API has none, or limit the
  messages in the proxy ([WebSocket message size](#websocket-message-size)).
- **`--h11-max-incomplete-event-size 16384`.** Granian's
  `--http1-buffer-size` bounds the request line and headers, and answers
  431 past it. Unless set (417,792), it let header blocks of over 500 KB
  through. 16384 keeps uvicorn's 16 KiB, which the application's limits
  count on.
- **One process.** `--workers` (`GRANIAN_WORKERS`, `SERVE["WORKERS"]`)
  starts that many worker processes under a main one. When a worker
  dies, Granian stops the others, as on `SIGTERM`, and exits 1, unless
  `--respawn-failed-workers` starts another in its place
  ([A worker that fails as it starts](#a-worker-that-fails-as-it-starts)).
- **HTTP/1.1.** uvicorn speaks nothing else. Granian also speaks HTTP/2
  without TLS (h2c) to a client that opens with it, unless `--http 1`
  ([HTTP versions](#http-versions)).
- **`--no-proxy-headers`.** Nothing to set: Granian reads no proxy
  header ([Proxy headers](#proxy-headers)).
- **The log.** Granian's main process never imports the application, so
  `LOGGING` reaches it only as a JSON file, `--log-config`, which
  `manage.py logconfig` writes
  ([The server's own lines](../runtime.md#the-servers-own-lines)). Its
  access log stays off unless `--access-log`: leave it off, as
  `domainkit.access` writes one line per request.

`check` sees Granian's options only as its variables (`GRANIAN_*`), as
[below](#websocket-message-size), or as `SERVE` where `serve` starts it.

It counts the worker processes the same way, for `live.W003` (live
routes served by several processes without a fan-out,
[live.md](../live.md#every-processs-events)): `--workers` in the
server's own process, `SERVE["WORKERS"]` (else `WEB_CONCURRENCY`) in
`serve`'s; elsewhere `WEB_CONCURRENCY` (which uvicorn and gunicorn
read), `UVICORN_WORKERS`, `GRANIAN_WORKERS`, `-w` in
`GUNICORN_CMD_ARGS`, or `SERVE["WORKERS"]`.

## HTTP versions

uvicorn speaks HTTP/1.1 only. Granian, unless told, also speaks HTTP/2
without TLS (h2c) to a client that opens a connection with HTTP/2's
preface (`--http auto`). Without TLS there is no ALPN to agree on a
version, so a client must know beforehand; an HTTP/1.1 client never
gets HTTP/2. `SERVE["HTTP"]` sets it:

- `"1"`: HTTP/1.1 only, as under uvicorn. Choose it unless the proxy in
  front is set to speak h2c to the server.
- `"auto"` (the default, as in 2.4): both. Keep it while a proxy in front
  sends h2c (Envoy and Traefik can be set to) and something else still
  sends HTTP/1.1, such as a health check.
- `"2"`: h2c only. HTTP/1.1 clients, health checks included, are not
  answered, and Granian carries no WebSocket over HTTP/2: set
  `SERVE["WEBSOCKETS"] = False`, or `SERVE` refuses it.

`KEEP_ALIVE` is HTTP/1.1's (`--http1-header-read-timeout`); over HTTP/2
the proxy keeps its own connections. `serve` gives Granian
`MAX_HEADER_SIZE` for HTTP/2's headers too (`--http2-max-headers-size`),
where Granian's HTTP/2 takes 16 MiB of headers unless told.

## A worker that fails as it starts

Granian starts again a worker that died (`--respawn-failed-workers`,
which `serve` sets), unless it died within 5.5 seconds of being started
again: then it stops, and exits 1. A startup that takes longer to fail,
such as one that waits for a database that is not there, is started
again for ever: the container stays up, serves nothing, and its
orchestrator never sees it fail.

`SERVE["EXIT_ON_FAILED_START"] = True` stops the server the first time a
worker fails as it starts: the application cannot be imported, or its
startup fails (`lifespan.startup.failed`). `serve` then exits 3, which
is what uvicorn and gunicorn exit with when a worker cannot start, and
the orchestrator restarts the container or reports it. A worker that dies
once it served (killed for its memory, say) is still started again.

```python
SERVE = {"EXIT_ON_FAILED_START": True}
```

```text
[ERROR] ASGI lifespan startup failed
[INFO] Granian shutdown completed, see ya!
Error: A worker failed as it started, so the server stopped (SERVE["EXIT_ON_FAILED_START"]); its log above says why.
```

Each worker loads the application through `serve`, which signals the
main process (`SIGUSR1`) when the start fails; the main process stops
Granian as `SIGTERM` does. So, with the option on, `SIGUSR1` to the main
process means a failed start: send it nothing else. It needs POSIX
signals: not on Windows. A worker killed before its startup ends (out of
memory, say) sends nothing, and is started again as before; so does one
interrupted (Ctrl+C). Granian's free-threaded build loads the
application once, in the main process, before it serves: a failed import
is logged there, and `serve` exits 3 too. As Granian's own loader does,
`serve` imports the application from the working directory first. uvicorn already stops when a
worker fails before it serves: with one worker, it exits 3; with
several, its main process stops. [ADR 0169](../adr/0169-granians-http-versions-failed-starts-fcm-dry-runs-write-once-probes-clamd-over-tls-and-dropped-files.md)
records the choices.

## WebSocket message size

`--ws-max-size` stops a WebSocket message before it is read whole.
`MAX_REQUEST_BODY_SIZE` then closes the connection on a message that got
through.

In staging and production, `check` warns (`servers.W001`) when an
application that serves WebSockets runs under a server that reads
messages larger than `MAX_REQUEST_BODY_SIZE`: uvicorn without
`--ws-max-size` (or with a larger one), or Granian with WebSockets on. It
reads the server's environment variables: `UVICORN_WS_MAX_SIZE` and
`GRANIAN_WEBSOCKETS`, which both servers accept in place of options, and
`GUNICORN_CMD_ARGS`. A deployed application logs the same warning when it
starts, from the server's own command line. A value it cannot read is
taken as unknown, and never stops the application or `check`.

Under `serve`, the server's limits are `SERVE`'s: uvicorn reads messages
up to `MAX_REQUEST_BODY_SIZE`, and Granian, with `SERVE["WEBSOCKETS"]`
on, any size. A deployed application with WebSocket routes that `serve`
started logs the warning as it starts, from `SERVE`.

`check` runs in a process of its own, often in another container (a
deployment job's `check --deploy`): it sees the variables set where it
runs, not the server's command line. Options given as arguments
(`granian --no-ws`, `uvicorn --ws-max-size`, `--uds`) are not seen
there. It then says nothing of a server it cannot find, or judges one by
variables that the arguments override. To have `check --deploy` judge
the server, give the server its options as variables
(`GRANIAN_WEBSOCKETS=false`, `UVICORN_WS_MAX_SIZE`, `GRANIAN_UDS`), and
set the same variables where `check` runs.

uvicorn started by gunicorn (`-k uvicorn.workers.UvicornWorker`, or
`uvicorn_worker.UvicornWorker` from the `uvicorn-worker` package), or by
`fastapi run`, reads neither `--ws-max-size` nor `UVICORN_WS_MAX_SIZE`. It
always reads messages of up to 16 MiB, and `check` warns about it. Under
gunicorn, give a worker class of your own the limit, and start gunicorn
with `-k config.workers.LimitedWorker`:

```python
from uvicorn_worker import UvicornWorker


class LimitedWorker(UvicornWorker):
    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, "ws_max_size": 1_048_576}
```

`check` does not look inside such a class, so it says nothing about it.
For `fastapi run`, run uvicorn itself.

Some servers are not recognised, so `check` says nothing about them, and
you set their limits yourself:

- Hypercorn, which also reads up to 16 MiB (`websocket_max_message_size`,
  in its configuration file);
- gunicorn with a worker class named only in its configuration file.

## Proxy headers

`--no-proxy-headers` leaves `X-Forwarded-*` to `TRUSTED_PROXIES` and
`PROXY_SCHEME_HEADER`. Without it, uvicorn reads these headers itself,
from `127.0.0.1` or from whatever `FORWARDED_ALLOW_IPS` allows. `*` there
lets any client choose its address, and so its rate limit and its login
throttle. uvicorn also sets the scheme from `X-Forwarded-Proto` before
domainkit runs, so `PROXY_SCHEME_HEADER = None` does not stop a client's
own header from choosing it. Granian reads none of these headers.
`runserver` runs this way, with either server. See
[Running behind a proxy](../recipes/behind-a-proxy.md).

## Unix sockets

Behind a proxy on a unix socket (`serve --uds`, `uvicorn --uds`,
`granian --uds`, `gunicorn --bind unix:...`), no caller has an address. Without `"unix"`
in `TRUSTED_PROXIES`, every caller shares one rate limit and one hold on
refused credentials, and one anonymous flood of bad tokens holds back
every user of a token. Add it, with the proxy's networks if it has more
hops:

```python
TRUSTED_PROXIES = ["unix"]  # nginx: proxy_pass http://unix:/run/api.sock;
```

In staging and production, `check` warns (`servers.W002`) when the
server's variables (`UVICORN_UDS`, `GRANIAN_UDS`, `GUNICORN_CMD_ARGS`) put
it on a unix socket, and `TRUSTED_PROXIES` lacks `"unix"`. A deployed
application logs it at start, from its own command line.

!!! warning
    With `"unix"` listed, any connection that the server reports without
    an address counts as the proxy's. List `"unix"` only where the server
    listens on nothing else without an address.

## See also

- [HTTP settings](http.md): the application's own limits.
- [Checks](checks.md): `servers.W001`, `servers.W002`, and `servers.W003`.
- [ADR 0160](../adr/0160-serve-in-production-from-the-settings.md): why
  `serve` starts the server from the settings, and checks the proxy's
  idle timeout.
- [Deploying](../deploying.md) and [runtime.md](../runtime.md#servers).
- [The server's own lines](../runtime.md#the-servers-own-lines): `LOGGING`
  for Granian's main process.

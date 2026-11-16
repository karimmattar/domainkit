# Server limits

The application limits request bodies, WebSocket messages, request rates,
how long a request may take (`REQUEST_TIMEOUT`), and how many requests it
works on at once (`MAX_CONCURRENT_REQUESTS`). But some requests must be
stopped before the application sees them: slow ones and oversized ones.
That is the job of the server, and of the proxy in front of it. This page
gives the server options to set, and why.

On this page you find:

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
the time the server keeps an idle connection open (uvicorn's
`--timeout-keep-alive`, Granian's `--http1-header-read-timeout`).
Otherwise the proxy now and then sends a request on a connection the
server is closing, and answers it with a 502. The proxy's setting is
nginx's `keepalive_timeout` in the `upstream` block (60 seconds unless
set), or Traefik's `idleConnTimeout` (90 seconds unless set).

domainkit runs on [uvicorn](https://www.uvicorn.org/) or
[Granian](https://github.com/emmett-framework/granian)
(`pip install 'domainkit[granian]'`). [runtime.md](../runtime.md#servers)
compares them.

## The command lines

uvicorn, one process per core:

```bash
uvicorn config.asgi:application --workers 4 --loop uvloop \
  --limit-concurrency 1200 --backlog 2048 --timeout-keep-alive 5 \
  --timeout-graceful-shutdown 10 \
  --h11-max-incomplete-event-size 16384 --ws-max-size 1048576 \
  --no-proxy-headers --no-server-header
```

The `Dockerfile` that `init` writes runs this command, with
`--app-dir src`, `--host 0.0.0.0`, and `--port 8000`. It sets the number
of processes with `WEB_CONCURRENCY`, not `--workers`.

Granian, the same, with `LOGGING` written for its own process first:

```bash
python manage.py logconfig --output /tmp/logging.json
granian --interface asgi config.asgi:application --host 0.0.0.0 --port 8000 \
  --workers 4 --loop uvloop --backpressure 1200 --backlog 2048 \
  --http1-header-read-timeout 60000 --http1-buffer-size 16384 \
  --workers-kill-timeout 25 --respawn-failed-workers --no-ws \
  --log-config /tmp/logging.json
```

`init --server granian` writes a `Dockerfile` that runs these two
commands, with `--working-dir src`. It sets the number of processes with
`GRANIAN_WORKERS`, not `--workers`. [Granian](#granian) says why each
option is there.

## What each option limits

| Concern | uvicorn | Granian |
| --- | --- | --- |
| Requests at once, per process | `--limit-concurrency` (more get 503) | `--backpressure` connections, idle ones included (more wait, unanswered) |
| Idle keep-alive connections | `--timeout-keep-alive` seconds | `--http1-header-read-timeout` milliseconds (30,000 unless set, 60,000 at most) |
| Slow headers | the proxy | `--http1-header-read-timeout` milliseconds |
| Request line and headers | `--h11-max-incomplete-event-size` (16 KiB) | `--http1-buffer-size` bytes (about half a megabyte unless set; past it, 431) |
| WebSocket message | `--ws-max-size` bytes (16 MiB unless set) | no option: `--no-ws` if unused, else the proxy |
| Graceful shutdown | `--timeout-graceful-shutdown` seconds, then open connections are closed, then the application shuts down | no limit unless `--workers-kill-timeout` seconds, the application's shutdown included; then workers are killed |
| A worker that dies | `--workers` starts another | every worker stops and Granian exits 1, unless `--respawn-failed-workers` |
| The `Server` header | `--no-server-header` leaves it out | no option: `Server: granian` unless the answer has one (`SERVER_HEADER`) |

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
never refuses the probes in `CONCURRENCY_EXEMPT_PATHS`. uvicorn's own
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
- **One process.** `--workers` (or `GRANIAN_WORKERS`) starts that many
  worker processes under a main one. When a worker dies, Granian stops
  the others, as on `SIGTERM`, and exits 1, unless
  `--respawn-failed-workers` starts another in its place.
- **`--no-proxy-headers`.** Nothing to set: Granian reads no proxy
  header ([Proxy headers](#proxy-headers)).
- **The log.** Granian's main process never imports the application, so
  `LOGGING` reaches it only as a JSON file, `--log-config`, which
  `manage.py logconfig` writes
  ([The server's own lines](../runtime.md#the-servers-own-lines)). Its
  access log stays off unless `--access-log`: leave it off, as
  `domainkit.access` writes one line per request.

`check` sees Granian's options only as its variables (`GRANIAN_*`), as
[below](#websocket-message-size).

It counts the worker processes the same way, for `live.W003` (live
routes served by several processes without a fan-out,
[live.md](../live.md#every-processs-events)): `--workers` in the
server's own process; elsewhere `WEB_CONCURRENCY` (which uvicorn and
gunicorn read), `UVICORN_WORKERS`, `GRANIAN_WORKERS`, or `-w` in
`GUNICORN_CMD_ARGS`.

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

Behind a proxy on a unix socket (`uvicorn --uds`, `granian --uds`,
`gunicorn --bind unix:...`), no caller has an address. Without `"unix"`
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
- [Checks](checks.md): `servers.W001` and `servers.W002`.
- [Deploying](../deploying.md) and [runtime.md](../runtime.md#servers).
- [The server's own lines](../runtime.md#the-servers-own-lines): `LOGGING`
  for Granian's main process.

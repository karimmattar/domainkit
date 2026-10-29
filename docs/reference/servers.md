# Server limits

The application limits bodies, WebSocket messages, request rates, how long
a request may take (`REQUEST_TIMEOUT`), and how many it works on at once
(`MAX_CONCURRENT_REQUESTS`). Slow and oversized requests are the server's
to stop, before the application sees them. Run the server behind a reverse
proxy or load balancer that limits header size and the time to send
headers and body (nginx:
`large_client_header_buffers`, `client_header_timeout`,
`client_body_timeout`), and set the server's own limits. domainkit runs on
[uvicorn](https://www.uvicorn.org/) or [Granian](https://github.com/emmett-framework/granian)
(`pip install 'domainkit[granian]'`); [runtime.md](../runtime.md#servers)
compares them.

uvicorn, one process per core:

```bash
uvicorn config.asgi:application --workers 4 --loop uvloop \
  --limit-concurrency 1200 --backlog 2048 --timeout-keep-alive 5 \
  --timeout-graceful-shutdown 10 \
  --h11-max-incomplete-event-size 16384 --ws-max-size 1048576 \
  --no-proxy-headers --no-server-header
```

Granian, the same:

```bash
granian --interface asgi config.asgi:application --host 0.0.0.0 --port 8000 \
  --workers 4 --loop uvloop --backpressure 1200 --backlog 2048 \
  --http1-header-read-timeout 5000 --workers-kill-timeout 10 \
  --respawn-failed-workers --no-ws
```

| Concern | uvicorn | Granian |
| --- | --- | --- |
| Requests at once, per process | `--limit-concurrency` (more get 503) | `--backpressure` |
| Idle keep-alive connections | `--timeout-keep-alive` seconds | `--http1-keep-alive` / `--no-http1-keep-alive` |
| Slow headers | the proxy | `--http1-header-read-timeout` milliseconds |
| Request line and headers | `--h11-max-incomplete-event-size` (16 KiB) | `--http1-buffer-size` |
| WebSocket message | `--ws-max-size` bytes (16 MiB unless set) | no setting: `--no-ws` if unused, else the proxy |
| Graceful shutdown | `--timeout-graceful-shutdown` seconds, then open connections are closed | `--workers-kill-timeout` seconds, then workers are killed |

Keep the shutdown timeout below the orchestrator's grace period (30
seconds on Kubernetes) less the time the application takes to close its
resources, so a stop never ends in `SIGKILL`.

Keep `--limit-concurrency` (Granian: `--backpressure`) above
`MAX_CONCURRENT_REQUESTS` plus `MAX_QUEUED_REQUESTS`, and the WebSockets
you expect: it is the backstop.

A request whose body is slow to arrive holds no slot of
`MAX_CONCURRENT_REQUESTS` while it waits, and is answered 408 after
`REQUEST_BODY_TIMEOUT`; but it still holds a connection, the server's
place in `--limit-concurrency`, and what it has sent so far. A buffering
proxy in front is still recommended: nginx reads the whole body before it
passes the request on (`proxy_request_buffering on`, its default), within
`client_body_timeout` and `client_max_body_size`, so slow clients cost
the proxy, not the application.
The application refuses first, with a JSON 503 and `Retry-After`, and never
refuses the probes in `CONCURRENCY_EXEMPT_PATHS`; uvicorn's 503 is plain, and
refuses probes too. Keep `REQUEST_TIMEOUT` below the load balancer's idle
timeout (60 seconds on most), so a slow request gets the application's 503,
not a 504 from the balancer.

`--ws-max-size` stops a WebSocket message before it is read whole;
`MAX_REQUEST_BODY_SIZE` then closes the connection on one that got through.
`check` warns, in staging and production, when an application that serves
WebSockets runs under a server that reads messages larger than
`MAX_REQUEST_BODY_SIZE`: uvicorn without
`--ws-max-size` (or with a larger one), or Granian with WebSockets on. It
reads the server's environment variables (`UVICORN_WS_MAX_SIZE`,
`GRANIAN_WEBSOCKETS`, which both servers accept in place of options, and
`GUNICORN_CMD_ARGS`); a deployed application logs the same warning when it
starts, from the server's own command line. A value it cannot read is
taken as unknown, and never stops the application or `check`.

uvicorn started by gunicorn (`-k uvicorn.workers.UvicornWorker`, or
`uvicorn_worker.UvicornWorker` from the `uvicorn-worker` package) or by
`fastapi run` reads neither `--ws-max-size` nor `UVICORN_WS_MAX_SIZE`: it
always reads messages of up to 16 MiB, and `check` warns about it. Under
gunicorn, give a worker class of your own the limit, and start gunicorn
with `-k config.workers.LimitedWorker`:

```python
from uvicorn_worker import UvicornWorker


class LimitedWorker(UvicornWorker):
    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, "ws_max_size": 1_048_576}
```

`check` does not look inside such a class, so it says nothing about it. For
`fastapi run`, run uvicorn itself. Hypercorn, which reads up to 16 MiB as
well (`websocket_max_message_size`, in its configuration file), and
gunicorn with a worker class named only in its configuration file, are not
recognised: `check` says nothing about them, so set their limits yourself.

`--no-proxy-headers` leaves `X-Forwarded-*` to `TRUSTED_PROXIES` and
`PROXY_SCHEME_HEADER`: uvicorn otherwise reads them itself, from
`127.0.0.1` or from whatever `FORWARDED_ALLOW_IPS` allows (`*` lets any
client choose its address, and so its rate limit and login throttle), and
sets the scheme from `X-Forwarded-Proto` before domainkit runs, so
`PROXY_SCHEME_HEADER = None` does not stop a client's own header from
choosing it. Granian reads none. `runserver` runs this way, with either
server.

Behind a proxy on a unix socket (`uvicorn --uds`, `granian --uds`,
`gunicorn --bind unix:...`), no caller has an address: without `"unix"`
in `TRUSTED_PROXIES` every caller shares one rate limit and one hold on
refused credentials, and one anonymous flood of bad tokens holds every
token user back. Add it, with the proxy's networks if it has more hops:

```python
TRUSTED_PROXIES = ["unix"]  # nginx: proxy_pass http://unix:/run/api.sock;
```

`check` warns where deployed when the server's variables (`UVICORN_UDS`,
`GRANIAN_UDS`, `GUNICORN_CMD_ARGS`) put it on a unix socket and
`TRUSTED_PROXIES` lacks `"unix"`; a deployed application logs it at start,
from its own command line. Any connection the server reports without an
address is then the proxy's: list `"unix"` only where the server listens
on nothing else without one.

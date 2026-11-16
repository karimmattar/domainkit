# 0160. Serve in production from the settings

- Status: Accepted
- Date: 2026-11-16

## Context

A project reported (S1) that moving from uvicorn to Granian meant
translating uvicorn's options by hand, and that several have no
counterpart. 2.4 gave Granian a section of "Server limits", an image
(`init --server granian`), and `manage.py logconfig`, which writes
`LOGGING` for Granian's main process. Two parts stayed open:

- **No production command.** `runserver` is a development server: it
  takes an address and a reload switch. Production ran each server's own
  command line, written out in the image and in the docs, so a project
  still kept the two in step by hand, and the Granian image wrote a file
  to `/tmp` before it started.
- **Nothing checked the idle timeouts.** Granian keeps an idle
  connection as long as it waits for the next request's headers
  (`--http1-header-read-timeout`, 30 seconds unless set) and refuses
  more than 60 seconds. A proxy that keeps idle connections longer sends
  a request now and then on a connection the server is closing, and
  answers it 502. The docs said so; nothing warned.

What the servers and the proxies say:

- Granian is run from Python with the same options as its command line,
  `granian.Granian(...).serve()` (its command line does exactly that),
  with workers, hooks, and `log_dictconfig`; its embedded server
  (`granian.server.embed`) runs one worker as a task of the caller's
  loop, and is experimental [1]. uvicorn is run with `uvicorn.run(...)`,
  whose `workers` default to `WEB_CONCURRENCY` [2].
- uvicorn's docs keep `--reload` for development and `--workers` (or
  gunicorn) for production [3]. FastAPI's CLI splits the two the same
  way: `fastapi dev` reloads on `127.0.0.1`, `fastapi run` does not, on
  `0.0.0.0`, "how you would normally run it in production, for example,
  in a container" [4]. Django's `runserver` says "DO NOT USE THIS SERVER
  IN A PRODUCTION SETTING", and leaves production to an ASGI server
  pointed at `asgi.py` [5, 6].
- AWS: an Application Load Balancer keeps an idle connection 60 seconds
  unless set, and "we also recommend that you configure the idle timeout
  of your application to be larger than the idle timeout configured for
  the load balancer", else it may send a request on a connection the
  application closed, and answer 502 [7]. Google Cloud's external
  Application Load Balancer keeps idle connections to the server 600
  seconds, fixed, and asks for 620 on the server [8]. nginx keeps an
  idle upstream connection 60 seconds unless `keepalive_timeout` says
  otherwise [9]; Envoy an hour [10]; Traefik 90 seconds [11]. gunicorn's
  keep-alive is 2 seconds unless set [12], and its uvicorn worker passes
  it on as uvicorn's (`uvicorn/workers.py`).

Options considered for the command:

1. **Docs only**, as 2.4 left it. Each project keeps the translation,
   and `check --deploy`, in a container of its own, sees the server's
   variables only, never its arguments (S3).
2. **A command that builds each server's command line and `exec`s it.**
   The server's own parser, and its `GRANIAN_*` and `UVICORN_*`
   variables, would apply as before. `LOGGING` still has to be written
   to a file first, so the file system must be writable, and the
   variables could override what the settings say.
3. **A command that starts each server through its Python API**, with
   options from the settings. Granian's main process gets `LOGGING` as a
   mapping, the same one `logconfig` writes; the options are whatever
   the settings say, and `check` reads the same settings.
4. **gunicorn as the process manager.** An extra dependency, no help for
   Granian, and uvicorn's own `uvicorn.workers` is deprecated [3].

For the check, the proxy's idle timeout is not something the framework
can see: a setting has to say it.

## Decision

Option 3.

- `manage.py serve [address] [--uds PATH]` starts the server for
  `ASGI_APPLICATION`, on `0.0.0.0:8000` unless given another, as
  `fastapi run` does. It never reloads. It loads the settings only: each
  worker imports the application, as under the server's command line.
- `SERVE` (`ServeConfig`) sets it. `SERVER` is `"auto"`: Granian where
  the `granian` extra is installed, else uvicorn. `WORKERS` defaults to
  `WEB_CONCURRENCY`, then 1, as uvicorn and gunicorn read it.
  `MAX_CONNECTIONS` (1200), `BACKLOG` (2048), `KEEP_ALIVE`,
  `GRACEFUL_TIMEOUT` (10), `MAX_HEADER_SIZE` (16384), and `WEBSOCKETS`
  (on) map to each server's option. The defaults are "Server limits"'
  command lines, and two tests run those lines through uvicorn's and
  Granian's own parsers and compare what they would start with what
  `serve` starts.
- `KEEP_ALIVE` unset is 5 seconds on uvicorn, as before, and 60 on
  Granian, its most; Granian refuses more, and so does `SERVE` when it
  names Granian, or `serve` when `auto` chose it. Granian kills its
  workers `GRACEFUL_TIMEOUT` plus `SHUTDOWN_TIMEOUT` seconds after a
  stop (25 by default), as the application's shutdown runs in its time.
  uvicorn reads a WebSocket message up to `MAX_REQUEST_BODY_SIZE`.
  Neither reads proxy headers or writes an access log; uvicorn writes no
  `Server` header; Granian starts a worker that died again.
- `serve` passes `LOGGING` to the server as `logconfig` writes it, so
  its main process writes its lines as the application does, and a
  `LOGGING` that no file could hold is refused with its place.
- Under `serve`, `server_limits` and `listens_on_unix_socket` read the
  server from `SERVE` and the command line, so a deployed application
  still logs `servers.W001` and `servers.W002` as it starts.
- Both images `init` writes run `python manage.py serve`; the Granian
  one depends on the `granian` extra, so `auto` starts Granian, and no
  file is written to `/tmp` first.
- `PROXY_IDLE_TIMEOUT` (None unless set) is the proxy's idle timeout.
  In staging and production, `check` warns (`servers.W003`) when the
  server's keep-alive is not longer, with both numbers, and a hint that
  names the setting to raise and the value, or, past Granian's 60
  seconds, says to lower the proxy's. The server's keep-alive is read
  from its variables where they name one (`UVICORN_TIMEOUT_KEEP_ALIVE`,
  `GRANIAN_HTTP1_HEADER_READ_TIMEOUT`, gunicorn's `--keep-alive` in
  `GUNICORN_CMD_ARGS`), else from `SERVE`. Equal counts as too short: both
  sides then close at the same moment.

## Consequences

- A project sets the server's limits once, in its settings, for either
  server; moving to Granian is installing its extra. The image's command
  is one line, and needs no writable `/tmp`.
- `check --deploy` in a deployment job judges the server `serve` starts
  from the same settings, where it saw nothing of the server's arguments
  before. A project that runs a server's own command line with options
  as arguments is judged by `SERVE` unless it sets the server's
  variables where `check` runs, as for `servers.W001`.
- `SERVE["WEBSOCKETS"]` is on by default, so a project with WebSocket
  routes keeps them when `auto` moves it to Granian; with Granian, such
  a project logs `servers.W001` until it turns them off or limits them
  in the proxy. Granian reads no message of an application that accepts
  none.
- The check is opt-in: no project's `check --deploy` fails until it
  sets `PROXY_IDLE_TIMEOUT`. Behind Google Cloud's load balancer (600
  seconds, fixed), Granian always fails it: serve with uvicorn there.
- uvicorn's `MAX_HEADER_SIZE` is its h11 parser's limit, which counts
  only a header block still arriving; its httptools parser, which
  `uvicorn[standard]` installs and prefers, has none. The docs say so,
  and leave the header limit to the proxy, as before.
- uvicorn re-raises the signal that stopped it, so `serve` on uvicorn
  exits as killed by `SIGTERM` (143), as the uvicorn command does;
  Granian exits 0.

## References

1. Granian's README, "Embedding Granian in your project" and the CLI's
   options: <https://github.com/emmett-framework/granian/blob/master/README.md>
2. uvicorn's settings, `--workers` and `WEB_CONCURRENCY`:
   <https://uvicorn.dev/settings/>
3. uvicorn's deployment guide: <https://uvicorn.dev/deployment/>
4. FastAPI CLI, `fastapi dev` and `fastapi run`:
   <https://fastapi.tiangolo.com/fastapi-cli/>
5. Django's `runserver`:
   <https://docs.djangoproject.com/en/5.2/ref/django-admin/#runserver>
6. Django, how to deploy with ASGI:
   <https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/>
7. AWS, "Connection idle timeout" of an Application Load Balancer:
   <https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-load-balancer-attributes.html#connection-idle-timeout>
8. Google Cloud, troubleshooting external Application Load Balancers
   (backend keepalive 600 seconds; set the server's to 620):
   <https://docs.cloud.google.com/load-balancing/docs/https/troubleshooting-ext-https-lbs>
   and request distribution:
   <https://docs.cloud.google.com/load-balancing/docs/https/request-distribution>
9. nginx, `keepalive_timeout` of the upstream module:
   <https://nginx.org/en/docs/http/ngx_http_upstream_module.html#keepalive_timeout>
10. Envoy, connection timeouts:
    <https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts>
11. Traefik, `serversTransport` `forwardingTimeouts.idleConnTimeout`:
    <https://doc.traefik.io/traefik/v3.6/reference/routing-configuration/http/load-balancing/serverstransport/>
12. gunicorn's settings, `keepalive`:
    <https://gunicorn.org/reference/settings/#keepalive>

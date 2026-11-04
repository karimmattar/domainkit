# Live connections: WebSockets, event streams, and live events

A request lasts milliseconds, but a WebSocket or a Server-Sent Events
stream stays open for minutes or hours. During that time, the client may
lose its rights, send too much, or stop reading. domainkit keeps every
such *live connection* limited and authorized for its whole life, and it
can stream your committed [domain events](learning.md#words) to the
clients that may see them.

On this page you learn how to:

- serve a WebSocket, and know each close code a client can get;
- stream Server-Sent Events with `EventSourceResponse` and
  `ServerSentEvent`, and resume after a reconnect;
- give one route its own limits with `LiveLimits`;
- send committed domain events to clients with `live_events`;
- know what the re-checks of a connection's rights can and cannot do;
- test live connections with a frozen clock.

## A first example

A WebSocket route that answers each message in capital letters, and a
test that shows it:

```python
from fastapi import APIRouter, WebSocket

from domainkit.http.access import Public

router = APIRouter()


@router.websocket("/echo", dependencies=[Public])
async def echo(websocket: WebSocket) -> None:
    await websocket.accept()
    async for text in websocket.iter_text():
        await websocket.send_text(text.upper())
```

```python
from domainkit.testing import TestClient


def test_echo(client: TestClient) -> None:
    with client.websocket_connect("/echo") as socket:
        socket.send_text("hi")
        assert socket.receive_text() == "HI"
```

The route is like any FastAPI WebSocket route. What domainkit adds is in
`WebSocketLimitsMiddleware`, which the default `MIDDLEWARE` lists. It
gives every connection these limits:

- **Caps.** A process holds at most `LIVE_MAX_CONNECTIONS` live
  connections, and a caller (a user, or an address for anonymous callers)
  at most `LIVE_MAX_CONNECTIONS_PER_CALLER`. One more is refused at once,
  with 503 and `Retry-After`.
- **Limited sending.** At most `LIVE_SEND_QUEUE` messages wait for a
  client. A client that takes longer than `LIVE_SEND_TIMEOUT` seconds to
  receive one is too slow, and its connection is closed. The client then
  connects again.
- **Clocks.** A WebSocket ends after `WEBSOCKET_IDLE_TIMEOUT` seconds
  without a message either way, and after `WEBSOCKET_MAX_LIFETIME`
  seconds in all. An event stream ends after `SSE_MAX_LIFETIME`.
- **Re-authorization.** Every `LIVE_REAUTH_INTERVAL` seconds, the
  credentials of the handshake are checked again, and so are the access
  rules of the route (its `Require`). A token revoked or expired, a user
  deactivated, or a permission removed ends the connection. Checks that a
  handler makes itself, once, are not run again: put lasting conditions
  in the route's rules.

The design is in [ADR 0055](adr/0055-bound-live-connections.md) (the
limits), [ADR 0056](adr/0056-reauthorize-open-connections.md)
(re-authorization), and
[ADR 0057](adr/0057-live-committed-events-in-process.md) (live events).

## WebSockets

`WebSocketLimitsMiddleware` limits every WebSocket. New projects list it.
In an older project, add it to `MIDDLEWARE` after the authentication and
rate limit middleware. Then it counts users, and only handshakes that the
rate limit let through. With `domainkit.contrib.auth` installed, a project
that lists it before `AuthenticationMiddleware` does not start.

```python
MIDDLEWARE = [
    # ...
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
```

A WebSocket route needs an access rule, like any route. Nothing else
changes in it:

```python
from fastapi import Depends

from domainkit.http.access import HasPermissions, Require


@router.websocket("/chat", dependencies=[Depends(Require(HasPermissions("chat.join")))])
async def chat(websocket: WebSocket) -> None:
    await websocket.accept()
    async for text in websocket.iter_text():
        await websocket.send_text(text)
```

### Close codes

The middleware closes a WebSocket for these reasons. The client sees the
close code and the reason:

| When | What happens | Close |
| --- | --- | --- |
| The process or the caller is at its cap | The handshake is refused: 503 with `Retry-After` (5 to 15 seconds) where the server can answer a handshake with a status (uvicorn can), else a close | 1013 |
| The client sends more than `WEBSOCKET_MESSAGE_RATE` | Closed, and logged as a security event | 1008 `Too many messages.` |
| A message waits longer than `LIVE_SEND_TIMEOUT` to reach the client | Closed: the client is too slow | 1013 `Client too slow.` |
| No message either way for `WEBSOCKET_IDLE_TIMEOUT` | Closed | 1001 `Idle for too long.` |
| Open for `WEBSOCKET_MAX_LIFETIME` | Closed; the client connects again | 1001 `Connection lifetime reached.` |
| The credentials or the route's rules no longer hold | Closed | 1008 `Not authorized.` |
| They cannot be checked (the database is down, or the check takes over 10 seconds) | Closed | 1013 `Authorization not confirmed.` |
| A message larger than `MAX_REQUEST_BODY_SIZE` | Closed ([Message size](#message-size)) | 1009 |

When the framework closes a WebSocket, it cancels the route's handler, so
the handler never outlives its connection, whatever it was awaiting. Its
`finally` blocks run. A handler that sends faster than the client reads
waits while the queue is full, as it would on the network. The message
rate is counted per connection, in the process that holds it.

Keep-alive pings are the server's job. uvicorn pings every 20 seconds and
closes a connection whose client does not answer within 20 more seconds
(`--ws-ping-interval`, `--ws-ping-timeout`). So a client that went away
is found even when nothing is sent.

### A route's own limits

`LiveLimits` is a FastAPI dependency that sets a route's limits in place
of the settings. The caps on open connections stay the process's:

```python
from domainkit.http.live import LiveLimits


@router.websocket(
    "/cursor",
    dependencies=[
        Depends(Require()),
        Depends(
            LiveLimits(message_rate="60/second", idle_timeout=30, max_lifetime=600)
        ),
    ],
)
async def cursor(websocket: WebSocket) -> None: ...
```

| Key | Replaces | What it sets |
| --- | --- | --- |
| `max_lifetime` | `WEBSOCKET_MAX_LIFETIME`, `SSE_MAX_LIFETIME` | Seconds the connection may stay open; at most 86,400. |
| `idle_timeout` | `WEBSOCKET_IDLE_TIMEOUT` | Seconds without a message before a WebSocket ends; `None`: never idle. |
| `message_rate` | `WEBSOCKET_MESSAGE_RATE` | Messages a client may send, such as `"60/second"`; `None`: any. |
| `send_queue` | `LIVE_SEND_QUEUE` | Messages that may wait for the client (1 to 10,000). |
| `send_timeout` | `LIVE_SEND_TIMEOUT` | Seconds one message may take to reach the client. |
| `reauth_interval` | `LIVE_REAUTH_INTERVAL` | Seconds between re-checks; at most 3,600; `None`: never. |
| `keepalive` | `SSE_KEEPALIVE` | Event streams only: seconds without an event before a keep-alive comment. |

The values are checked when the route is defined. A wrong one raises
`ImproperlyConfigured` at import:

```text
domainkit.exceptions.ImproperlyConfigured: LiveLimits: 1 validation error for _Overrides
max_lifetime
  Input should be less than or equal to 86400 [type=less_than_equal, input_value=100000, input_type=int]
```

`LiveLimits` is not an access rule: the route still needs one.

### What re-checks cannot do

- **A route that opts out keeps what was revoked.** With
  `LiveLimits(reauth_interval=None)`, a connection keeps a revoked or
  expired token, a deactivated user, or a removed permission until it
  closes: at `max_lifetime` at the latest (a day at most). `check` warns
  about `LIVE_REAUTH_INTERVAL = None` (`security.W005`). Where deployed,
  it also warns about a route with an access rule that opts out
  (`live.W002`). Keep such routes few, and their lifetime short.
- **Credentials that work once fail their first re-check.** A re-check
  presents the handshake's credentials again. An authenticator built on
  single-use credentials (a one-time ticket in the URL, a nonce that it
  uses up) refuses them the second time. So the connection closes with
  1008 after its first interval. Use credentials that can be checked
  again, such as a token or a session, or opt the route out and accept
  the point above.
- **Blocking work on a thread is not cut off at 10 seconds.** The limit
  cancels what the check awaits. Work that an authenticator runs on a
  thread, with `anyio.to_thread.run_sync` or Starlette's
  `run_in_threadpool` (as FastAPI runs a sync dependency), does not obey
  it: the check waits until the thread is done, and its answer then
  counts. Meanwhile the connection stays open, and a connection that
  closes waits for the check. Work run with `loop.run_in_executor`, as the
  password hashing pool does, is left to finish on its own at the limit,
  and the check fails (1013).

A re-check costs what authenticating a request costs, once per connection
per interval, never per message: 1,000 open connections at the default
interval make about 17 checks a second. It reads through the
[principal cache](auth/authentication.md#the-principal-cache), as
requests do. A kept principal costs no query, and a change that drops it
is seen at the next re-check.

### Message size

`MAX_REQUEST_BODY_SIZE` also limits each WebSocket message: a larger one
closes the connection with 1009. By then the server has read the whole
message, up to its own limit, which is 16 MiB in uvicorn. Run the server
with the same limit, so that it refuses larger messages before it reads
them:

```bash
uvicorn config.asgi:application --ws-max-size 1048576 ...
```

- In staging and production, `check` warns when the application serves
  WebSockets and the server's limit is above `MAX_REQUEST_BODY_SIZE`
  (`servers.W001`). It reads the limit from the server's environment
  variables (`UVICORN_WS_MAX_SIZE`; 16 MiB when it is not set). A deployed
  application logs the same warning when it starts, from the server's own
  command line ([reference/servers.md](reference/servers.md)).
- uvicorn under gunicorn's uvicorn worker, or under `fastapi run`, always
  reads up to 16 MiB: neither passes it a limit.
  [reference/servers.md](reference/servers.md) shows how to set one.
- Hypercorn is not recognized, so its limit is yours to set.
- `check` also warns when `WebSocketLimitsMiddleware` is not in
  `MIDDLEWARE` (`live.W001`).

## Server-Sent Events

`EventSourceResponse` streams events to a browser's `EventSource`, or to
any client that reads `text/event-stream`. Return one from a route, over
an async iterator of `ServerSentEvent`s, or of text, which is sent as one
event's data:

```python
from collections.abc import AsyncIterator

from fastapi import Request
from pydantic import BaseModel

from domainkit.http.access import Public
from domainkit.http.sse import EventSourceResponse, ServerSentEvent, last_event_id


class Price(BaseModel):
    id: int
    sku: str
    amount: str


@router.get("/prices", dependencies=[Public])
async def prices(request: Request) -> EventSourceResponse:
    after = last_event_id(request)  # "2" after a reconnect, else None

    async def events() -> AsyncIterator[ServerSentEvent | str]:
        start = int(after) + 1 if after else 1
        for n in range(start, start + 2):
            price = Price(id=n, sku="LAMP-1", amount="19.90")
            yield ServerSentEvent.json(price, event="price", id=str(n))
        yield "done"

    return EventSourceResponse(events(), retry=3000)
```

The client receives this stream (headers first):

```text
content-type: text/event-stream; charset=utf-8
cache-control: no-store
x-accel-buffering: no

retry: 3000

event: price
id: 1
data: {"id":1,"sku":"LAMP-1","amount":"19.90"}

event: price
id: 2
data: {"id":2,"sku":"LAMP-1","amount":"19.90"}

data: done

```

A request with `Last-Event-ID: 2` gets the events with ids 3 and 4.

- `ServerSentEvent(data, event=..., id=..., retry=...)` is one event.
  `data` may hold several lines; each becomes a `data:` line. A type or an
  id with a line break, an id with a NUL character, and a negative retry
  raise `ValueError`.
- `ServerSentEvent.json(value, ...)` sends a value as compact JSON (UUIDs
  and dates as text).
- `event.encode()` returns the bytes that go on the wire:
  `ServerSentEvent("line one\nline two", event="note", id="7").encode()`
  is `b'event: note\nid: 7\ndata: line one\ndata: line two\n\n'`.
- `comment(text)` returns a comment, which `EventSource` ignores:
  `comment("keep-alive")` is `b': keep-alive\n\n'`.
- `Event` is the type of what a source yields: a `ServerSentEvent` or a
  `str`.
- `retry` (milliseconds) goes first. It tells the browser how long to
  wait before it connects again. A browser that connects again sends the
  last id it saw. `last_event_id(request)` reads it back, so the route can
  continue after it. It returns `None` when the header is absent, empty,
  longer than 256 characters, or holds control characters.
- The response has `Cache-Control: no-store` and `X-Accel-Buffering: no`,
  so nginx passes events on at once. `headers=` replaces them, whatever
  the case of their names. `status_code=` sets another status. A route's
  background tasks run once the stream ends.
- After `SSE_KEEPALIVE` seconds without an event, the stream sends a
  comment (`: keep-alive`), so proxies keep the connection open.
- The stream stops as soon as the client disconnects, and closes the
  source: an async generator's `finally` blocks run.

An event stream is limited as a WebSocket is: the caps (refused with 503
and `Retry-After`), `LIVE_SEND_QUEUE` and `LIVE_SEND_TIMEOUT`,
`SSE_MAX_LIFETIME`, and the re-checks of the credentials and the route's
rules. A stream that the framework ends finishes with a comment that says
why (`: Not authorized.`). The browser connects again, and a client that
is no longer allowed gets its 401 or 403 then.

Event streams last longer than any request deadline. The response marks
its scope with `domainkit.long_lived` and sends `text/event-stream`, so
deadlines can tell it apart.

### Ending a connection yourself: `Closed`

A source may end its connection by raising `Closed`, with a `Closure`
that holds the close code and the reason. The events it gave before are
sent first:

```python
from domainkit.http.live import Closed, Closure


async def events() -> AsyncIterator[ServerSentEvent | str]:
    yield "done"
    raise Closed(Closure(1000, "Market closed."))
```

The event stream then ends with the comment `: Market closed.`.
`Closure.code` is a WebSocket close code (RFC 6455); an event stream
ignores it. `EventSourceResponse` and `live_events.forward` handle
`Closed`. A WebSocket handler of your own does not get this help: close
the socket with `await websocket.close(code, reason)` instead.
`live_events` raises `Closed` itself for a client that is too slow, and
`forward` then closes the WebSocket with 1013 `Client too slow.`.

## Live committed events

`live_events` hears every event that the global `event_bus` publishes
after a commit. It passes each event to the open connections that
subscribed to its type, and whose rule allows it:

```python
from fastapi import Request, WebSocket
from starlette.requests import HTTPConnection

from domainkit.http.access import BasePermission, HasPermissions, Require
from domainkit.http.live_events import live_events
from domainkit.http.sse import EventSourceResponse


class IsCustomer(BasePermission):
    async def has_object_permission(
        self, request: HTTPConnection, user: object, obj: object
    ) -> bool:
        return getattr(obj, "customer_id", None) == getattr(user, "id", None)


@router.get(
    "/orders/events",
    dependencies=[Depends(Require(HasPermissions("shop.view_order")))],
)
async def order_events(request: Request) -> EventSourceResponse:
    return live_events.stream(request, OrderShipped, OrderCancelled, rule=IsCustomer())


@router.websocket(
    "/orders/live",
    dependencies=[Depends(Require(HasPermissions("shop.view_order")))],
)
async def order_socket(websocket: WebSocket) -> None:
    await live_events.forward(
        websocket, OrderShipped, OrderCancelled, rule=IsCustomer()
    )
```

An `OrderShipped` event has `order_id` and `customer_id`, and an
`address` marked `Annotated[str, Personal(of="customer_id")]`. Over
Server-Sent Events, it reaches the client as:

```text
event: OrderShipped
id: 937d44f9-c748-4a92-ad91-4e394ea2c8d8
data: {"event_id":"937d44f9-c748-4a92-ad91-4e394ea2c8d8","occurred_at":"2026-11-04T18:24:37.429021Z","order_id":"00000000-0000-0000-0000-000000000001","customer_id":"00000000-0000-0000-0000-000000000002"}
```

Over a WebSocket, it is one JSON message,
`{"event": "OrderShipped", "id": "937d44f9-...", "data": {...}}`, with
the same `data`. The `address` is not sent.

- **Two rules.** The route's own rule decides who may connect. `rule`
  decides, event by event, what each client sees: its `has_permission`
  and its `has_object_permission` (with the event as the object) must
  both allow it, for the user as of the last re-check. `rule` is
  required. Pass `AllowAny()` to send every event of those types to
  whoever may connect.
- **Scopes.** The user is read as `current_user` reads it. Credentials
  limited to scopes need a route that declares a rule that checks scopes,
  a `Public` one included
  ([Scopes on every route](auth/authentication.md#scopes-on-every-route)).
  Elsewhere `stream` answers `403`, and `forward` and `subscribe` refuse
  the WebSocket or the subscription before any event.
- **What is sent.** Over Server-Sent Events, each event is named after its
  type, has its `event_id` as the id, and its fields as JSON data. Over a
  WebSocket, `forward` accepts the connection if the route has not, and
  reads and drops what the client sends.
- **Personal data.** The fields marked `Personal` are left out. `render=`
  chooses what is sent instead:
  `render=lambda event: {"order": str(event.order_id)}`. `stream` also
  takes `retry=`.
- **Slow clients.** Each connection buffers up to `LIVE_SEND_QUEUE` events
  that its rule allows. One that falls further behind is closed as too
  slow (1013), and connects again. Events that a client may not see never
  count against it, however many one commit has. They wait for the rule in
  a buffer of their own, 16 times as large, and only a client that falls
  that far behind on them is closed too.
- **Publishing never waits.** Publishing never waits for a client, and
  never fails the commit. It lets the event loop run after each event, so
  a client that keeps up takes the events of a large commit one by one.
- **Your own sending.** `live_events.subscribe(connection, *types,
  rule=...)` gives the events to a route that sends them its own way:

  ```python
  async with live_events.subscribe(websocket, OrderShipped, rule=IsCustomer()) as events:
      await websocket.accept()
      async for event in events:
          await websocket.send_text(f"Order {event.order_id} has shipped.")
  ```

  Events committed while the block runs are heard; none before.

All three refuse a call with no event types, or a project without
`AuthenticationMiddleware` (`ImproperlyConfigured`): events are filtered
per user.

Events reach the connections of the process that committed them. Behind
several replicas, a client sees the events committed by its own replica.
To give every client every event, a durable handler (`durable=True`) could
publish each event to a fan-out exchange that each replica reads into its
`live_events.publish`. domainkit does not do this yet (ADR 0057).

A client that connects again misses what committed while it was away. A
route that must not lose events replays them from its own records, after
`last_event_id(request)`, then streams live ones.

## Options

The settings; the [HTTP reference](reference/http.md#live-connections)
has the full rows:

| Setting | Default | What it does |
| --- | --- | --- |
| `LIVE_MAX_CONNECTIONS` | `1000` | Open WebSockets and event streams per process (at most 1,000,000). |
| `LIVE_MAX_CONNECTIONS_PER_CALLER` | `10` | The same per user, or per address; at most `LIVE_MAX_CONNECTIONS`. |
| `LIVE_SEND_QUEUE` | `64` | Messages or events that may wait for one client (1 to 10,000). |
| `LIVE_SEND_TIMEOUT` | `10.0` | Seconds one message may take to reach the client. |
| `LIVE_REAUTH_INTERVAL` | `60.0` | Seconds between re-checks (at most 3,600); `None` turns them off. |
| `WEBSOCKET_IDLE_TIMEOUT` | `300.0` | Seconds without a message before a WebSocket closes. |
| `WEBSOCKET_MAX_LIFETIME` | `3600.0` | Seconds a WebSocket may stay open (at most 86,400). |
| `WEBSOCKET_MESSAGE_RATE` | `"20/second"` | Messages a client may send per connection; `None`: any. |
| `SSE_KEEPALIVE` | `15.0` | Seconds without an event before a keep-alive comment. |
| `SSE_MAX_LIFETIME` | `3600.0` | Seconds an event stream may stay open (at most 86,400). |
| `MAX_REQUEST_BODY_SIZE` | see the reference | Also the largest WebSocket message. |

`LIVE_MAX_CONNECTIONS_PER_CALLER` above `LIVE_MAX_CONNECTIONS` is refused
when the settings load.

## Errors and refusals

| What the client sees | Why | What to do |
| --- | --- | --- |
| 503 with `Retry-After`, or close 1013 | The process or the caller has too many connections. | Wait, then connect again. Raise the caps if the load is normal. |
| Close 1008 `Too many messages.` | The client sent faster than `WEBSOCKET_MESSAGE_RATE`. | Send less, or set `LiveLimits(message_rate=...)`. |
| Close 1013 `Client too slow.` | A message waited longer than `LIVE_SEND_TIMEOUT`. | The client connects again. |
| Close 1001 | Idle, or the lifetime is reached. | The client connects again. |
| Close 1008 `Not authorized.`, or `: Not authorized.` | A re-check failed. | The client logs in again. |
| Close 1013 `Authorization not confirmed.` | A re-check could not finish. | The client connects again later. |
| Close 1009 | A message larger than `MAX_REQUEST_BODY_SIZE`. | Send smaller messages. |
| 403 from `live_events.stream` | Scoped credentials on a route with no rule that checks scopes. | Declare such a rule on the route. |

## Testing

Freeze the clock to move a connection's timers. Under a frozen clock, the
connection reads the time every few milliseconds, so ticking past a limit
ends the connection without the test waiting for it. Use the
`domainkit.testing` `TestClient`, which carries the frozen clock into the
application:

```python
import pytest
from starlette.websockets import WebSocketDisconnect

from domainkit.clock import freeze_time
from domainkit.testing import TestClient


def test_idle_sockets_close(client: TestClient) -> None:
    with freeze_time() as clock, client.websocket_connect("/echo") as socket:
        socket.send_text("hi")
        assert socket.receive_text() == "HI"
        clock.tick(300)  # WEBSOCKET_IDLE_TIMEOUT
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
    assert (closed.value.code, closed.value.reason) == (1001, "Idle for too long.")
```

The test clients read a response whole, so test an event stream through
a source that ends:

```python
def test_prices_resume_after_the_last_id(client: TestClient) -> None:
    answer = client.get("/prices", headers={"Last-Event-ID": "2"})
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert "id: 3\n" in answer.text
```

More in [testing.md](testing.md).

## Common mistakes

- **Checking a permission once, inside the handler.** It is not checked
  again. Put lasting conditions in the route's `Require`, which every
  re-check runs.
- **Single-use credentials on a live route.** The first re-check refuses
  them, and the connection closes after one interval. Use a token or a
  session.
- **Turning re-checks off to save work.** A connection then keeps revoked
  rights for up to a day. A re-check costs one authentication per
  interval, not per message.
- **Expecting every replica's events.** `live_events` sends only the
  events committed in the client's own process.
- **Leaving the server's message limit at 16 MiB.** Set `--ws-max-size` to
  `MAX_REQUEST_BODY_SIZE`.

## See also

- [HTTP reference](reference/http.md#live-connections): every live
  setting.
- [Servers](reference/servers.md): the server's own WebSocket limits.
- [Authentication](auth/authentication.md#the-principal-cache): the
  principal cache that re-checks read.
- [Caches and rate limits](cache.md#throttles-for-one-route): a
  `Throttle` also limits WebSocket handshakes.
- [The glossary](learning.md#words): domain event, handler.

# Live connections: WebSockets, event streams, and live events

A WebSocket or a Server-Sent Events stream stays open for minutes or hours,
where a request lasts milliseconds. domainkit keeps such a connection
bounded and authorized for its whole life:

- **Caps.** A process holds at most `LIVE_MAX_CONNECTIONS` of them, and a
  caller (a user, or an address for anonymous callers) at most
  `LIVE_MAX_CONNECTIONS_PER_CALLER`. One more is refused at once, with 503
  and `Retry-After`.
- **Bounded sending.** At most `LIVE_SEND_QUEUE` messages wait for a client.
  A client that takes longer than `LIVE_SEND_TIMEOUT` seconds to receive
  one is too slow, and its connection is closed; it reconnects.
- **Clocks.** A WebSocket ends after `WEBSOCKET_IDLE_TIMEOUT` seconds
  without a message either way, and after `WEBSOCKET_MAX_LIFETIME` seconds
  in all; an event stream after `SSE_MAX_LIFETIME`.
- **Re-authorization.** Every `LIVE_REAUTH_INTERVAL` seconds, the
  credentials of the handshake are checked again, and so are the access
  rules of the route (its `Require`). A token revoked or expired, a user
  deactivated, or a permission removed ends the connection. Checks a
  handler makes itself, once, are not run again: put lasting conditions in
  the route's rules.
- **Live events.** A route can stream the domain events that commit to the
  clients that may see them (`domainkit.http.live_events`).

The design is explained in [ADR 0055](adr/0055-bound-live-connections.md)
(the limits), [ADR 0056](adr/0056-reauthorize-open-connections.md)
(re-authorization), and [ADR 0057](adr/0057-live-committed-events-in-process.md)
(live events).

## WebSockets

`WebSocketLimitsMiddleware` bounds every WebSocket. New projects list it;
add it to an older project's `MIDDLEWARE`, after the authentication and
rate limit middleware, so it counts users and only handshakes the rate
limit let through (with `domainkit.contrib.auth` installed, a project that
lists it before `AuthenticationMiddleware` does not start):

```python
MIDDLEWARE = [
    ...,
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
```

A WebSocket route needs an access rule like any route. Nothing else
changes in it:

```python
@router.websocket("/chat", dependencies=[Depends(Require(HasPermissions("chat.join")))])
async def chat(websocket: WebSocket) -> None:
    await websocket.accept()
    async for text in websocket.iter_text():
        await websocket.send_text(reply_to(text))
```

What the middleware does, and the close codes a client sees:

| When | What happens | Close |
| --- | --- | --- |
| The process or the caller is at its cap | The handshake is refused: 503 with `Retry-After` (5 to 15 seconds), where the server can answer a handshake with a status (uvicorn can), else a close | 1013 |
| The client sends more than `WEBSOCKET_MESSAGE_RATE` | Closed; logged as a security event | 1008 `Too many messages.` |
| A message waits longer than `LIVE_SEND_TIMEOUT` to reach the client | Closed: the client is too slow | 1013 `Client too slow.` |
| No message either way for `WEBSOCKET_IDLE_TIMEOUT` | Closed | 1001 `Idle for too long.` |
| Open for `WEBSOCKET_MAX_LIFETIME` | Closed; the client reconnects | 1001 `Connection lifetime reached.` |
| The credentials or the route's rules no longer hold | Closed | 1008 `Not authorized.` |
| They cannot be checked (the database is down, or the check takes over 10 seconds) | Closed | 1013 `Authorization not confirmed.` |

When the framework closes a WebSocket, the route's handler is cancelled,
so it never outlives its connection, whatever it was awaiting; its
`finally` blocks run. A handler that sends faster than the client reads
waits while the queue is full, as it would on the network. The message
rate is counted per connection, in the process that holds it.

Keep-alive pings are the server's. uvicorn pings every 20 seconds and
closes a connection whose client does not answer within 20 more
(`--ws-ping-interval`, `--ws-ping-timeout`), so a client that vanished is
found even when nothing is sent.

### A route's own limits

`LiveLimits` sets a route's limits in place of the settings. The caps on
open connections stay the process's:

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

Its keys are `max_lifetime`, `idle_timeout` (None: never idle),
`message_rate` (None: any), `send_queue`, `send_timeout`, `reauth_interval`
(None: never), and, for event streams, `keepalive`. They are checked when
the route is defined.

### Message size

`MAX_REQUEST_BODY_SIZE` also bounds each WebSocket message: a larger one
closes the connection with 1009. The server has read the whole message by
then, up to its own limit, which is 16 MiB in uvicorn. Run it with the same
limit, so it refuses larger messages before reading them:

```bash
uvicorn config.asgi:application --ws-max-size 1048576 ...
```

and tell `check` that it does, with `WEBSOCKET_SERVER_MAX_SIZE = 1048576`.
In staging and production, `check` warns when the application serves
WebSockets and the server's limit (16 MiB unless that setting says
otherwise) is above `MAX_REQUEST_BODY_SIZE`, or when
`WebSocketLimitsMiddleware` is not in `MIDDLEWARE`.

## Server-Sent Events

`EventSourceResponse` streams events to a browser's `EventSource`, or any
client that reads `text/event-stream`. Return one from a route over an
async iterator of `ServerSentEvent`s (or of text, sent as one event's
data):

```python
from domainkit.http.sse import EventSourceResponse, ServerSentEvent, last_event_id


@router.get("/prices", dependencies=[Depends(Require())])
async def prices(request: Request) -> EventSourceResponse:
    async def events() -> AsyncIterator[ServerSentEvent]:
        async for price in price_feed(after=last_event_id(request)):
            yield ServerSentEvent.json(price, event="price", id=str(price.id))

    return EventSourceResponse(events(), retry=3000)
```

- `ServerSentEvent(data, event=..., id=..., retry=...)`: data may hold
  several lines; a type or an id with a line break, an id with NUL, and a
  negative retry are refused. `ServerSentEvent.json(value, ...)` sends a
  value as compact JSON.
- `retry` (milliseconds) goes first, and tells the browser how long to wait
  before reconnecting. A reconnecting browser sends the last id it saw;
  `last_event_id(request)` reads it back (None if absent, empty, longer
  than 256 characters, or holding control characters), so the route can
  resume after it.
- The response has `Cache-Control: no-store` and `X-Accel-Buffering: no`,
  so nginx passes events on at once; `headers=` replaces them, whatever
  the case of their names. A route's background tasks run once the stream
  ends.
- After `SSE_KEEPALIVE` seconds without an event it sends a comment
  (`: keep-alive`), so proxies keep the connection open.
- It stops as soon as the client disconnects, and closes the source (an
  async generator's `finally` blocks run).

An event stream is bounded as a WebSocket is: the caps (refused with 503
and `Retry-After`), `LIVE_SEND_QUEUE` and `LIVE_SEND_TIMEOUT`,
`SSE_MAX_LIFETIME`, and the re-checks of the credentials and the route's
rules. A stream the framework ends finishes with a comment that says why
(`: Not authorized.`); the browser reconnects, and a client no longer
allowed gets its 401 or 403 then. A source may end the stream itself by
raising `Closed(closure)` (`domainkit.http.live`); the events it gave are
sent first.

Event streams last longer than any request deadline: the response marks
its scope with `domainkit.long_lived`, and sends `text/event-stream`, so
deadlines can tell it apart.

## Live committed events

`live_events` hears every event the global `event_bus` publishes after a
commit, and passes each to the open connections subscribed to its type
whose rule allows it:

```python
from domainkit.http.live_events import live_events


class IsCustomer(BasePermission):
    async def has_object_permission(self, request, user, obj) -> bool:
        return obj.customer_id == user.id


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

- The route's own rule decides who may connect. `rule` decides, event by
  event, what each client sees: its `has_permission` and its
  `has_object_permission` (with the event as the object) must both allow
  it, for the user as of the last re-check. `rule` is required: pass
  `AllowAny()` to send every event of those types to whoever may connect.
- Over Server-Sent Events, each event is named after its type, has its
  `event_id` as the id, and its fields as JSON data. Over a WebSocket, each
  is a JSON message, `{"event": ..., "id": ..., "data": {...}}`; what the
  client sends is read and dropped.
- The fields marked `Personal` are left out. `render=` chooses what is sent
  instead: `render=lambda event: {"order": str(event.order_id)}`.
- Each connection buffers up to `LIVE_SEND_QUEUE` events. One that falls
  further behind is closed as too slow (1013), and reconnects. Publishing
  never waits for a client, and never fails the commit.
- `live_events.subscribe(connection, *types, rule=...)` gives the events to
  a route that sends them its own way.

Events reach the connections of the process that committed them. Behind
several replicas, a client sees the events committed by its own replica.
To give every client every event, a durable handler (`durable=True`) can
publish each one to a fan-out exchange that each replica reads into its
`live_events.publish`; domainkit does not do this yet (ADR 0057).

A client that reconnects misses what committed while it was away. A route
that must not lose events replays them from its own records, after
`last_event_id(request)`, then streams live ones.

## Testing

Freeze the clock to move a connection's timers: under a frozen clock, the
watch reads the time every few milliseconds, so ticking past a limit ends
the connection without the test waiting for it. Use `domainkit.testing`'s
`TestClient`, which carries the frozen clock into the application:

```python
def test_idle_sockets_close(client_for: ClientFor) -> None:
    client = client_for("chat.join")
    with freeze_time() as clock, client.websocket_connect("/chat") as socket:
        clock.tick(300)  # WEBSOCKET_IDLE_TIMEOUT
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
    assert (closed.value.code, closed.value.reason) == (1001, "Idle for too long.")
```

The test clients read a response whole, so test an event stream through
one with a source that ends.

## Settings

The settings are listed in [reference.md](reference.md#live-connections).

A re-check costs what authenticating a request costs (with tokens, a
token lookup and the user with its groups and permissions), once per
connection per interval, never per message: 1,000 open connections at the
default interval make about 17 checks a second.

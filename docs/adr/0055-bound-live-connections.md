# 0055. Bound every live connection: caps, a send queue, a message rate, and clocks

- Status: Accepted
- Date: 2026-10-20

## Context

A request ends in milliseconds; a WebSocket or an event stream may stay
open for hours. Until now domainkit gave a WebSocket its request context,
its user, a rate-limited handshake, access rules, and a size limit per
message, and nothing else (R08, N9 in the async re-audit):

- nothing capped how many connections a process, or one caller, held open,
  so one client could hold every file descriptor and every task;
- nothing bounded what waited for a slow client. A handler that buffers for
  it (fan-out to many sockets) grows without bound, and one that does not
  waits on it for ever;
- a client could send messages as fast as the network carries them;
- an idle connection, or a busy one, stayed open as long as both ends did;
- a handler blocked on something other than the socket (a queue of
  events) never noticed that the connection was gone.

Options for each:

- **Where the limits live.** In each route (a helper that routes call), or
  in middleware that every WebSocket passes. A helper is easy to forget,
  and a route that forgets it is unbounded. Middleware bounds every
  WebSocket, and new projects list it.
- **A slow client.** Drop its messages; close it as soon as one message
  waits; or let the sender wait for room in a bounded queue, and close the
  client when one message takes too long. Dropping loses data silently.
  Closing on the first full queue punishes a fast sender to a healthy
  client (a burst of history). Waiting with a deadline keeps memory bound
  by the queue and time bound by the deadline.
- **The message rate.** Count in the rate limit's backend
  (`RATE_LIMIT_BACKEND`, Redis behind several replicas) or in process
  memory. A connection lives in one process: counting it in Redis adds a
  round trip per message and shares nothing.
- **Over the rate.** Close with 1008, or drop the message and warn the
  client. A client over the rate is either broken or hostile; a warning
  frame gives it a channel to ignore, and the server still reads its
  flood.
- **A refused handshake.** Close with 1013 before the accept, or answer
  503 with `Retry-After` through the ASGI denial extension. uvicorn turns
  a close before the accept into a 403, so the 1013 never reaches the
  client, and a 403 says the wrong thing. The rate limit already answers
  429 where it can and closes with 1013 where it cannot.
- **Ending the handler.** When the framework closes a connection, it can
  only tell the handler (a disconnect from `receive`, an error from
  `send`), or cancel it. A handler awaiting something else would not hear
  either.

## Decision

- `WebSocketLimitsMiddleware`, in the default middleware and the project
  template, bounds every WebSocket. `EventSourceResponse` applies the same
  limits to event streams. The two share `domainkit.http.live`.
- **Caps.** `LIVE_MAX_CONNECTIONS` (1,000) per process and
  `LIVE_MAX_CONNECTIONS_PER_CALLER` (10) per user, or per address for
  anonymous callers, counted together for WebSockets and event streams.
  One more is answered 503 with `Retry-After` (a random 5 to 15 seconds, so
  retries spread out): through the denial extension for a WebSocket where
  the server has it, else a 1013 close.
- **Sending.** A WebSocket's messages wait in a queue of `LIVE_SEND_QUEUE`
  (64). A sender waits while it is full. A message that takes longer than
  `LIVE_SEND_TIMEOUT` (10 seconds) to reach the client closes the
  connection with 1013, "Client too slow.", and the client reconnects.
  An event stream reads its source only as fast as the client takes
  events, with the same queue and deadline.
- **The rate.** `WEBSOCKET_MESSAGE_RATE` (`"20/second"`) per connection,
  counted with the rate limit's `MemoryBackend` in the process. One more
  message closes the connection with 1008, logged as a security event.
- **Clocks.** `WEBSOCKET_IDLE_TIMEOUT` (300 seconds without a message
  either way) and `WEBSOCKET_MAX_LIFETIME` (an hour) close a WebSocket with
  1001; `SSE_MAX_LIFETIME` (an hour) ends an event stream, and
  `SSE_KEEPALIVE` (15 seconds) sends a comment through quiet proxies. The
  clocks read `domainkit.clock`, so a frozen clock moves them in tests. A
  route sets its own with `LiveLimits`.
- **Ending.** When the framework closes a connection, it cancels the
  handler, then sends the close frame with its reason, bounded by five
  seconds. Errors of the handler are raised as they were.
- **Pings** stay the server's: uvicorn pings every 20 seconds and drops a
  client that does not answer.
- **Message size.** The 1009 check counts text in UTF-8 a chunk at a time,
  never copying a whole message. It runs once the server has read the
  message, so `check` warns, in staging and production, when the
  application serves WebSockets and the server's limit (read from the
  server's environment, and its command line at start; uvicorn's 16 MiB
  unless set) is above `MAX_REQUEST_BODY_SIZE`, and when the middleware is
  missing.

## Consequences

- A process holds a bounded number of live connections, and each a
  bounded number of messages, whatever its clients do.
- A client on a poor network may be closed as too slow, and must
  reconnect; one that holds a connection open for over an hour, or idles
  for five minutes, too. Clients of WebSockets and event streams have to
  reconnect anyway; these limits make them do it.
- Projects without `MIDDLEWARE` in their settings get the new middleware
  with the default list, and their WebSockets the new limits.
- The caps are per process. Behind several replicas, the total is the
  replicas times the cap; the per-caller cap is per replica too.
- Handlers that held resources across an `await` must release them in
  `finally` blocks: a cancelled handler runs them, an abandoned one never
  did.

# 0072. Walk proxy headers from the right, trust unix sockets by name, and name the scheme header

- Status: Accepted
- Date: 2026-10-25

## Context

`ProxyHeadersMiddleware` decides who a request is from: the rate limit,
the hold on refused credentials, the live-connection budget per caller,
idempotency's anonymous scope, and the audit trail's address all follow
its `scope["client"]`. The 0.9.0 audit found three ways to get it wrong
at the proxy boundary:

- It parsed every `X-Forwarded-For` hop before walking from the right,
  and gave up on the whole header when any hop was no address. A proxy
  appends the client's address at the right, so a client that sent
  `garbage` itself arrived as `garbage, 203.0.113.9`, and was counted as
  the proxy: a second rate limit and hold of its choosing (WEB-01).
- Behind a proxy on a unix socket (nginx to `uvicorn --uds`), the server
  gives no peer address, `TRUSTED_PROXIES` held networks only, and every
  caller was `ip:unknown`: one flood of bad tokens held every token user
  back (WEB-02).
- Every `X-Forwarded-Proto` line was believed once the peer was trusted.
  A proxy that sets `X-Forwarded-For` but not the scheme passes a
  client's own line through, which then chose whether its cookies were
  `Secure` (WEB-06).

Options considered:

- For WEB-01, parse every hop and skip the malformed ones. A skipped hop
  shifts the places the scheme is counted by, and a client could still
  push hops around; and what a client wrote left of its own hop should
  not be read at all.
- For WEB-02, a boolean setting (`TRUST_UNIX_SOCKET`). It splits one
  question, whom to believe, over two settings, and an environment
  variable cannot hold it beside the networks in one list.
- For WEB-06, detect a proxy that does not set the scheme. Nothing in a
  request tells a client's line from a proxy's when there is one of
  each; only the operator knows what the proxy writes. Turning scheme
  headers off by default would drop `Secure` cookies for every deployment
  whose proxy does set it, the common case.

## Decision

- The client is found by walking `X-Forwarded-For` from the right,
  through trusted proxies only, parsing each hop as it is reached: the
  first hop that is not a trusted proxy is the client, and nothing left
  of it is read. A hop that is no address where the walk stops is the
  client's, written by a trusted proxy (`unknown`, an obfuscated
  identifier): the client is then the last trusted hop passed (the peer
  when none was), at the unreadable hop's place for the scheme, and a
  warning on `domainkit.security` says so, throttled per proxy.
- `TRUSTED_PROXIES` takes `"unix"` beside networks. It believes a
  connection the server reports without an address (`scope["client"]` is
  None), and keeps `("unix", 0)` as the peer, which no network holds, so
  nothing that asks for a proxy by address (client certificates in
  headers) believes it. `check` warns, where deployed, when the server's
  command line or variables put it on a unix socket without `"unix"`.
- `PROXY_SCHEME_HEADER` names the header the trusted proxies set to the
  client's scheme, `X-Forwarded-Proto` by default, as before; `None`
  takes the scheme from no header. The value at the client's place is
  read; with fewer values than places, the right-most, the nearest
  proxy's, never the left-most.

## Consequences

- A client's own hops can only make it what it is. Headers whose
  client's hop was readable but had junk to its left now name the client
  where they named the proxy: the only change a correct deployment sees.
- `TRUSTED_PROXIES` is now a tuple of networks and a word; code that
  reads it must skip the string (`network.prefixlen` on `"unix"` fails).
  `"unix"` trusts every connection without an address, so it is for a
  server that listens on a unix socket only; a server that also takes
  TCP connections reports their addresses, which the networks judge.
- A proxy that does not set the scheme header still lets clients choose
  it until the operator sets `PROXY_SCHEME_HEADER = None`; the reference
  says so. A proxy-boundary check that sends requests through the real
  proxy (`check --proxy`) is left for 0.10.

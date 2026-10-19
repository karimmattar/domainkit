# 0036. Send webhooks through the outbox, signed, to checked addresses only

- Status: Accepted
- Date: 2026-10-19

## Context

Services tell other services about their events with webhooks. Doing it
in the request loses events when the process dies after the commit, sends
events for changes that roll back, and makes the request wait on someone
else's server. A webhook URL is also an address chosen by a user (staff,
here) that the server will call: the classic path to server-side request
forgery, cloud metadata theft, and port scans of the internal network.
Receivers need to know a request came from us and was not replayed.

The choices were how deliveries are queued, how they are signed, and how
the target is checked:

- **Queueing.** A separate webhook queue, or the outbox: an exposed event
  becomes a durable handler call, which fans out into one delivery task
  per subscriber.
- **Signing.** A scheme of our own, or Standard Webhooks, which Svix and
  others use and which has libraries in many languages.
- **Checking the target.** Validate the URL when saved, or before every
  attempt; check the host name, or every address it resolves to; then
  let the HTTP client resolve again (open to DNS rebinding), or connect to
  the checked address.

## Decision

- Exposed events go through the outbox: a durable handler (`fan_out`)
  writes one delivery row and one `deliver` task per active subscription
  that wants the event type, in one transaction, with ids derived from the
  event and the subscription so a retried fan-out adds nothing. Deliveries
  run on their own queue, with the task options of 0035 set from
  `WEBHOOKS`. Each attempt is logged (status code, latency, error). A
  delivery that exhausts its attempts is marked failed and can be
  replayed; it is not dead-lettered.
- Signatures follow Standard Webhooks: `webhook-id` (stable across retries
  and replays), `webhook-timestamp`, and `webhook-signature` with
  `v1,base64(HMAC-SHA256(secret, id.timestamp.body))`. A rotated secret
  keeps signing alongside the new one for an overlap.
- The URL is checked when saved and before every attempt: https (http only
  under `DEBUG`), no credentials, no local or metadata names; then every
  resolved address must be public, judging IPv4 addresses embedded in
  IPv6 ones by their IPv4 address. Named private networks may be allowed;
  metadata addresses never are. The request goes to the checked address,
  with the host name kept for the `Host` header, SNI, and the certificate
  check. Redirects are not followed and environment proxies are ignored.
  Each attempt has one deadline and reads a bounded answer.
- The client is httpx2, the library the `testing` extra already brings, in
  a new `webhooks` extra.
- Staff manage subscriptions through an admin API guarded by model
  permissions on `webhooks.subscription`; the secret is shown when made
  or rotated, and nowhere else.

## Consequences

- An event is sent only if its change commits, at least once; receivers
  must deduplicate by `webhook-id`.
- Secrets are stored in the clear, because signing needs them; database
  access is enough to forge deliveries. Encrypting them needs a key-
  management design (a `Signer`) that does not exist yet.
- Deliveries cannot reach internal services unless a network is allowed by
  name, and never through an egress proxy; a deployment that requires one
  needs a change here.
- Connections are not pooled across attempts: each attempt opens its own,
  which costs a handshake and keeps the address check simple.

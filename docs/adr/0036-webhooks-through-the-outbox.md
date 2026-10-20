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
  a new `webhooks` extra, with cryptography for the secrets.
- Secrets are stored encrypted with `domainkit.encryption.SecretBox`
  (ADR 0030), for the purpose `domainkit.webhooks.secret`, and bound to the
  subscription's id. A delivery decrypts the secrets it signs with; a
  rotation, the old secret it keeps. Values a fallback key encrypted are
  encrypted again under `SECRET_KEY` by the next delivery or rotation.
- Staff manage subscriptions through an admin API guarded by model
  permissions on `webhooks.subscription`; the secret is shown when made
  or rotated, and nowhere else. Those two answers are marked
  `Cache-Control: no-store`, so no cache keeps them and the idempotency
  middleware stores no copy to replay: a retry with the same
  `Idempotency-Key` gets a 409, not the secret again.

## Consequences

- An event is sent only if its change commits, at least once; receivers
  must deduplicate by `webhook-id`.
- The subscriptions table alone is not enough to forge deliveries: that
  also takes `SECRET_KEY` (or a fallback), and a secret copied to another
  row does not decrypt. Webhooks therefore need `SECRET_KEY` in every
  environment, and refuse to start without it; staging and production
  already require a strong one.
- `SECRET_KEY` can be replaced with the old key in `SECRET_KEY_FALLBACKS`:
  deliveries keep verifying and move secrets under the new key. A
  subscription with no delivery or rotation meanwhile still needs the
  fallback, so dropping it early loses that secret.
- A secret no key decrypts fails its deliveries at once, unsent, with the
  reason logged, reported, and kept as `last_error`; nothing is sent
  unsigned. A rotation drops such a secret (there is nothing to overlap
  with), and the failed deliveries can then be replayed.
- Deliveries cannot reach internal services unless a network is allowed by
  name, and never through an egress proxy; a deployment that requires one
  needs a change here.
- Connections are not pooled across attempts: each attempt opens its own,
  which costs a handshake and keeps the address check simple.

# Webhooks: signed events for other services

`domainkit.contrib.webhooks` sends domain events to URLs that staff
subscribe. Install it with messaging and the `webhooks` extra
(`domainkit[webhooks]`, which brings httpx2 and cryptography), set
`SECRET_KEY` (subscription secrets are encrypted under a key derived from
it; webhooks refuse to start without it), then expose the events:

```python
INSTALLED_DOMAINS = ["domainkit.contrib.messaging", "domainkit.contrib.webhooks", ...]

# In a DomainConfig.ready():
from domainkit.contrib.webhooks import expose

expose(
    InvoicePaid, "invoice.paid", payload=lambda event: {"invoice_id": event.invoice_id}
)
```

Without `payload`, every field of the event is sent: leave personal data
out. Subclasses of an exposed event are sent too. Each delivery is tagged
with the people its event is about, the subjects of its `Personal`
fields, so `privacy erase` blanks its body (and cancels it if still
pending), and `privacy export` lists it
([privacy.md](../privacy.md#what-erasure-reaches-and-when)). A receiver gets a `POST`
with a JSON body:

```json
{"type": "invoice.paid", "timestamp": "2026-10-19T12:00:00.123456Z", "data": {"invoice_id": "..."}}
```

**Delivery** goes through the outbox, so nothing is sent unless the change
commits. A committed exposed event becomes a durable handler call; the
worker then writes one delivery per active subscription that wants the
event type (or `*`), and a task for each, in one transaction. Deliveries
run on the `webhooks` queue (`QUEUE`), so slow receivers hold up no other
work. Any answer but 2xx fails the attempt, redirects included (they are
not followed). A failed attempt is retried with exponential backoff and
jitter, up to `MAX_ATTEMPTS`; then the delivery is marked failed. Receivers
must accept duplicates: delivery is at least once.

**Signatures** follow [Standard Webhooks](https://www.standardwebhooks.com):
each attempt carries `webhook-id` (the delivery's id, the same on retries
and replays), `webhook-timestamp`, and `webhook-signature`, `v1,` and the
base64 HMAC-SHA256 of `<id>.<timestamp>.<body>` keyed with the secret's
bytes. Secrets look like `whsec_` and 32 random bytes in base64; their
libraries verify them. In Python, `domainkit.contrib.webhooks.verify(secret,
headers, body)` checks one in constant time and refuses timestamps more
than five minutes away from `domainkit.clock` (frozen under
`freeze_time`). After a rotation, deliveries carry a second
signature with the old secret for `SECRET_OVERLAP_SECONDS`.

**What may be called.** Before every attempt, and when a subscription is
saved:

- the URL must use `https` (`http` only while `DEBUG` is on), and have no
  user name, password, or fragment; it is at most 2048 characters;
- `localhost`, `*.localhost`, and cloud metadata names are refused;
- every address the host resolves to must be public. Private, loopback,
  link-local, shared (CGNAT), reserved, multicast, and unspecified
  addresses are refused, in IPv4 and IPv6, and so are IPv4 addresses
  inside IPv6 ones (`::ffff:10.0.0.1`, NAT64). Cloud metadata addresses
  (`169.254.169.254`, `fd00:ec2::254`, `168.63.129.16`, and others) are
  always refused.
- The connection goes to the address that was checked, never to a second
  lookup, so a host that answers differently a moment later (DNS
  rebinding) gains nothing. The host name still serves for the `Host`
  header and for TLS: the certificate is checked against it.
- Proxies from the environment are not used. An attempt has `TIMEOUT`
  seconds in all, and at most `MAX_RESPONSE_BYTES` of the answer are read.
- Host names are looked up on the guard's own threads
  (`GUARD_LOOKUP_THREADS`, 2 at once per host). Each delivery's lookups
  are its subscription's owner's (`webhook-owner:<owner>`), or, for a
  subscription staff made, its own (`webhook:<id>`), whoever caused the
  event, and one owner may hold a quarter of the threads, however many
  subscriptions it has: receivers whose DNS never answers leave the rest
  to other subscribers' deliveries of the same events. A lookup holds its
  thread until the system's resolver gives up; then its host fails at
  once for a minute, so retries of its deliveries do not take the threads
  again. A receiver whose DNS answers after 5 seconds or more has its
  answer kept for a minute, so its next attempts take no thread either;
  one whose DNS fails in under 5 seconds takes a thread on each attempt
  ([runtime.md](../runtime.md#outgoing-http-calls)).
- Staff's subscriptions share by subscription: one holds at most 2
  threads (its host's bound), and staff who create many can still hold
  many, for a first round of slow lookups. Users' own subscriptions
  (below) share by owner: eight of one owner's hold 4 of the 16 threads,
  not all of them.

`ALLOWED_PRIVATE_NETWORKS` lets receivers in named private networks
through (`["10.20.0.0/16"]`); staging and production refuse loopback and
link-local networks there. A refused URL fails the delivery at once.

**Staff** manage subscriptions over the admin API, with
`domainkit.contrib.auth` installed:

```python
from domainkit.contrib.webhooks.admin import router as webhooks_admin

router.include_router(webhooks_admin, prefix="/admin/webhooks")
```

| Method and path | Permission | Does |
| --- | --- | --- |
| `GET /subscriptions` | `webhooks.view_subscription` | List subscriptions (`limit`, `offset`). |
| `POST /subscriptions` | `webhooks.add_subscription` | Subscribe a URL to `event_types`; the answer holds the secret, shown this once (`no-store`). |
| `GET /subscriptions/{id}` | `view` | One subscription. |
| `PATCH /subscriptions/{id}` | `change` | Change `url`, `event_types`, `description`, or `active`. |
| `POST /subscriptions/{id}/rotate-secret` | `change` | A new secret, shown once (`no-store`). |
| `DELETE /subscriptions/{id}` | `delete` | Delete it; its pending deliveries are cancelled. |
| `GET /subscriptions/{id}/deliveries` | `view` | Its deliveries, newest first (`status`, `limit`, `offset`). |
| `GET /deliveries/{id}` | `view` | A delivery, its body (`payload`, null once its subjects were erased), and every attempt (status code, latency, error). |
| `POST /deliveries/{id}/replay` | `change` | Send a finished delivery again (not an erased one). |

Each endpoint also needs a staff user or a superuser. Secrets never appear
in listings, logs, events, or reprs; changes are recorded as events
(`SubscriptionCreated` and others), which the audit trail keeps, with the
URL's host only, since URLs may hold tokens: a new URL is recorded in
`SubscriptionChanged` with its host and the host before (`host`,
`previous_host`), so the trail shows where events went. For the same reason, neither
a `Subscription`'s `repr` nor those of the admin API's bodies show its
URL. The two answers that hold a secret are marked `Cache-Control:
no-store`: no cache keeps them, and
`IdempotencyMiddleware` stores no copy, so a retry with the same
`Idempotency-Key` gets a 409 rather than the secret again.

**Secrets at rest.** The subscriptions table holds the current secret, and
the previous one during an overlap, encrypted with
`domainkit.encryption.SecretBox` (AES-256-GCM, purpose
`domainkit.webhooks.secret`) and bound to the subscription's id: the table
alone signs nothing, and a value copied to another row does not decrypt.
Deliveries decrypt the secrets they sign with, and a rotation the old
secret it keeps; nothing else does. To replace `SECRET_KEY`, put the old
key in `SECRET_KEY_FALLBACKS`: deliveries still verify, and each one
encrypts its subscription's secrets again under the new key (a rotation
does too). Drop the old key once every active subscription has had a
delivery or a rotation since. If no key decrypts a secret (`SECRET_KEY`
changed without the old one in `SECRET_KEY_FALLBACKS`), its deliveries
fail at once, unsent, with that reason in `last_error` and the error
log, and are reported: never sent unsigned. Put the old key back, or
rotate the secret (a rotation drops one no key decrypts, so there is no
overlap) and give receivers the new one; then `webhooks replay --failed`.

**Users' own subscriptions.** A platform whose customers register their
own webhooks mounts the self-service API next to its users' routes:

```python
from domainkit.contrib.webhooks.owned import router as my_webhooks

router.include_router(my_webhooks, prefix="/webhooks")

# In a DomainConfig.ready(): whose each event is.
expose(InvoicePaid, "invoice.paid", owner=lambda event: event.customer_id)
```

It has the admin API's subscription routes (`GET` and `POST
/subscriptions`, `GET`, `PATCH`, and `DELETE /subscriptions/{id}`, `POST
/subscriptions/{id}/rotate-secret`) and `GET /subscriptions/{id}/deliveries`
(without bodies), for any authenticated user, on the subscriptions that
user made. The subscription's `owner` is the user's id; another user's
subscription, or staff's, is 404, as one that does not exist. An owned
subscription receives only the events whose `owner` (`expose(...,
owner=...)`) is its owner, never one exposed without an owner, while
staff's subscriptions still receive every event they want. Where
subscriptions belong to a tenant rather than a user, name the owner
both ways: `owned_router(owner=lambda user: str(user.tenant_id))` and
`expose(..., owner=lambda event: event.tenant_id)`. An owner may have
`MAX_SUBSCRIPTIONS_PER_OWNER` subscriptions (a 422
`webhook_subscription_limit` past that, checked as one is made), and
erasing an owner (`privacy erase`) deletes theirs. The admin API shows
each subscription's `owner` (null for staff's). The subscriptions table
has a new `owner` column: run `makemigrations` and `migrate`.

`python manage.py webhooks subscriptions` lists subscriptions (host only),
`webhooks deliveries [--status S] [--subscription ID]` the latest
deliveries, and `webhooks replay ID ...` (or `--failed`) sends finished
deliveries again under the same `webhook-id`, except erased ones. Finished
deliveries and their attempts are deleted after `RETENTION_SECONDS`.

| `WEBHOOKS` key | Default | Meaning |
| --- | --- | --- |
| `QUEUE` | `"webhooks"` | The messaging queue of deliveries. |
| `MAX_ATTEMPTS` | `12` | Attempts before a delivery is marked failed (at most 100). |
| `RETRY_BASE_SECONDS` | `30.0` | First retry delay; it doubles per attempt, with jitter (at most 365 days). |
| `RETRY_MAX_SECONDS` | `21600.0` | Cap on the retry delay (six hours; at most 365 days). |
| `TIMEOUT` | `15.0` | Seconds an attempt may take in all (at most 120). Delivery tasks get 30 more for their database work, which must stay under `MESSAGING.VISIBILITY_TIMEOUT`. |
| `CONNECT_TIMEOUT` | `5.0` | Seconds to connect; at most `TIMEOUT`, and 60. |
| `MAX_RESPONSE_BYTES` | `65536` | Most bytes of an answer read (at most 10 MiB). |
| `MAX_PAYLOAD_BYTES` | `262144` | Largest body (1 KiB to 10 MiB); a larger event is reported and not sent. |
| `ALLOWED_PRIVATE_NETWORKS` | `()` | Private networks receivers may be in. |
| `CA_BUNDLE` | `None` | Certificates to trust instead of the system's. |
| `SECRET_OVERLAP_SECONDS` | `86400.0` | How long the old secret still signs after a rotation (at most 365 days). |
| `RETENTION_SECONDS` | `2592000.0` | How long finished deliveries are kept (30 days). |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | `10` | Subscriptions one owner may make through the self-service API (1 to 1000). |
| `USER_AGENT` | `"domainkit-webhooks"` | The `User-Agent` of deliveries: not empty, and at most 200 characters. |


# Webhooks: signed events for other services

A webhook sends a [domain event](../learning.md#words) to another
service's URL, as an HTTP `POST`. The webhooks app sends the events that
you expose to the URLs that staff, or your users, subscribe. Each
delivery is signed, so the receiver knows it came from you. It goes
through the [outbox](messaging.md), so nothing is sent unless the change
commits, and a failed delivery is tried again.

On this page you learn how to:

- expose an event as a webhook, and choose what its body holds;
- verify a delivery's signature on the receiving side;
- manage subscriptions through the staff API, or let users manage
  their own;
- know which URLs may be called, and how retries and replays work;
- keep subscription secrets safe, and change `SECRET_KEY` without
  losing them.

## Install

Install the app with messaging, which runs the deliveries, and the
`webhooks` extra, which brings httpx2 and cryptography:

```bash
pip install "domainkit[webhooks]"
```

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.webhooks",
]
```

```bash
python manage.py makemigrations -m "webhooks"
python manage.py migrate
```

You must also set `SECRET_KEY`. The app encrypts subscription secrets
under a key derived from it, and refuses to start without it. The app
brings three tables: `domainkit_webhook_subscriptions`,
`domainkit_webhook_deliveries`, and `domainkit_webhook_attempts`.

## A first example

Expose an event in your domain's `DomainConfig.ready()`, and choose what
its body holds:

```python
from domainkit.apps import DomainConfig
from domainkit.contrib.webhooks import expose


class BillingConfig(DomainConfig):
    label = "billing"

    def ready(self) -> None:
        from billing.domain import InvoicePaid

        expose(
            InvoicePaid,
            "invoice.paid",
            payload=lambda event: {"invoice_id": str(event.invoice_id)},
        )
```

When a use case that records `InvoicePaid` commits, each subscription
that wants `invoice.paid` (or `*`) gets a `POST` with this JSON body:

```json
{"type": "invoice.paid", "timestamp": "2026-10-19T12:00:00.123456Z", "data": {"invoice_id": "42"}}
```

The `timestamp` is when the event happened, in UTC.

## Exposing events: `expose`

`expose(event_type, name, *, payload=None, owner=None)` sends committed
events of a class to the subscribers of `name`:

| Argument | What it does |
| --- | --- |
| `event_type` | The event class. Its subclasses are sent too. |
| `name` | The event type that subscribers choose: up to 128 letters, digits, `_`, `.`, `:`, or `-`. A bad name raises `ImproperlyConfigured`. |
| `payload` | A function that returns the body's `data`. Without it, every field of the event is sent, as JSON. |
| `owner` | A function that returns whose event it is. Only then do users' own subscriptions get it (see [Users' own subscriptions](#users-own-subscriptions)). |

!!! warning
    Without `payload`, every field of the event is sent. Leave personal
    data out: a receiver keeps what it gets.

Each delivery is tagged with the people its event is about: the
subjects of its `Personal` fields. So `privacy erase` blanks the
delivery's body, and cancels it if it is still pending, and `privacy
export` lists it ([privacy.md](../privacy.md#what-erasure-reaches-and-when)).

To send an event that is not a domain event, call `queue_deliveries`
yourself. It writes one delivery per subscriber and returns how many:

```python
from domainkit import clock
from domainkit.contrib.webhooks import queue_deliveries

count = await queue_deliveries(
    "report.ready",
    {"report_id": "r-17"},
    key="report-r-17",  # the same key again adds nothing
    occurred_at=clock.now(),
)
```

`key` names the event: queueing it again adds no delivery. `subjects`
names the people the data is about, and `owner` whose event it is. A body
larger than `MAX_PAYLOAD_BYTES` raises `PayloadTooLarge`.

## How a delivery runs

1. A committed exposed event becomes a durable handler call in the
   outbox.
2. The worker writes one delivery per active subscription that wants
   the event type, and a task for each, in one transaction. Running this
   step twice adds nothing: a delivery's id comes from the event and
   the subscription.
3. Each task makes one attempt, on the `webhooks` queue (`QUEUE`). So
   slow receivers hold up no other work.

Any answer but 2xx fails the attempt, redirects too: they are not
followed. A failed attempt is tried again after a delay that starts at
`RETRY_BASE_SECONDS` and doubles, with jitter, up to `RETRY_MAX_SECONDS`.
After `MAX_ATTEMPTS`, the delivery is marked failed.

A delivery is in one of four states:

| State | Meaning |
| --- | --- |
| `pending` | Waiting for its next attempt. |
| `succeeded` | A receiver answered 2xx. |
| `failed` | Every attempt failed, the URL was refused, or no key decrypts the secret. |
| `cancelled` | The subscription was deleted or made inactive, or the people the delivery was about were erased. |

Delivery is at least once. A receiver must accept the same delivery
twice: the `webhook-id` header is the same on every attempt. Finished
deliveries and their attempts are deleted after `RETENTION_SECONDS`, by
an hourly task.

## Signatures

Signatures follow [Standard Webhooks](https://www.standardwebhooks.com).
Each attempt carries three headers:

| Header | What it holds |
| --- | --- |
| `webhook-id` | `msg_` and the delivery's id: the same on every retry and replay. |
| `webhook-timestamp` | When this attempt was signed, in seconds since 1970. |
| `webhook-signature` | `v1,` and the base64 HMAC-SHA256 of `<id>.<timestamp>.<body>`, keyed with the secret's bytes. |

A secret is `whsec_` and 32 random bytes in base64. The Standard
Webhooks libraries verify these deliveries. In Python, use `verify`:

```python
from domainkit.contrib.webhooks import InvalidSignature, verify

secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"  # from the subscription


async def receive(headers: dict[str, str], body: bytes) -> None:
    try:
        delivery_id = verify(secret, headers, body)
    except InvalidSignature:
        return  # answer 400 and drop it
    ...  # skip it if delivery_id was seen before
```

`verify` matches header names in any case, compares signatures in
constant time, and returns the `webhook-id`. It refuses a timestamp more
than 300 seconds away from now (`tolerance=300`). "Now" is
`domainkit.clock`, so `freeze_time` moves it, or the `now` you pass:

```python
from domainkit.contrib.webhooks import sign

body = b'{"type":"invoice.paid","timestamp":"2026-10-19T12:00:00.123456Z","data":{"invoice_id":"42"}}'
signature = sign(secret, "msg_2b7f", 1760875200, body)
headers = {
    "Webhook-Id": "msg_2b7f",
    "Webhook-Timestamp": "1760875200",
    "Webhook-Signature": signature,
}
print(signature)
print(verify(secret, headers, body, now=1760875230))
verify(secret, headers, body, now=1760876000)
```

```text
v1,WnoTcWo77PMLvoJmjmd3aXqfzMdzssUashtncIKncVw=
msg_2b7f
domainkit.contrib.webhooks.signing.InvalidSignature: The webhook timestamp is too old or too new.
```

The other refusals say `The webhook headers are missing or malformed.`
and `No webhook signature matches.` `new_secret()` makes a new secret,
as the app does for each subscription.

### Rotating a secret

When staff rotate a subscription's secret, deliveries carry a second
signature, with the old secret, for `SECRET_OVERLAP_SECONDS` (one day by
default). Receivers can then change the secret at their own pace. The
`webhook-signature` header holds both signatures, separated by a space;
`verify` accepts a match with either.

## What may be called

A URL is checked when a subscription is saved, and again before every
attempt. This guards against server-side request forgery: a subscriber
cannot make your service call your own network.

- The URL must use `https` (`http` only while `DEBUG` is on). It has no
  user name, password, or fragment, and has at most 2,048 characters.
- `localhost`, `*.localhost`, and cloud metadata names are refused.
- Every address that the host resolves to must be public. Private,
  loopback, link-local, shared (CGNAT), reserved, multicast, and
  unspecified addresses are refused, in IPv4 and IPv6. An IPv4 address
  inside an IPv6 one (`::ffff:10.0.0.1`, NAT64) is judged as the IPv4
  address. Cloud metadata addresses (`169.254.169.254`,
  `fd00:ec2::254`, `168.63.129.16`, and others) are always refused.
- The connection goes to the address that was checked, never to a
  second lookup. So a host that answers differently a moment later (DNS
  rebinding) gains nothing. The host name is still used for the `Host`
  header and for TLS: the certificate is checked against it.
- Proxies from the environment are not used. An attempt has `TIMEOUT`
  seconds in all, and reads at most `MAX_RESPONSE_BYTES` of the answer.

A refused URL raises `UnsafeTarget` (422, `unsafe_webhook_url`) when the
subscription is saved, and fails the delivery at once when it is sent.
The message never quotes the URL.

To let receivers in your own private network through, name the network:

```python
WEBHOOKS = {"ALLOWED_PRIVATE_NETWORKS": ["10.20.0.0/16"]}
```

Staging and production refuse loopback and link-local networks here.

### Host name lookups

Host names are looked up on the guard's own threads
(`GUARD_LOOKUP_THREADS`, at most 2 at once per host). The lookups of a
delivery belong to its subscription's owner (`webhook-owner:<owner>`),
or, for a subscription that staff made, to the subscription
(`webhook:<id>`), whoever caused the event. One owner may hold a quarter
of the threads, however many subscriptions it has. So receivers whose DNS
never answers leave threads for other subscribers' deliveries of the
same events.

A lookup holds its thread until the system's resolver gives up. Then
that host fails at once for 1 minute, so the retries of its deliveries
do not take the threads again. A receiver whose DNS answers after 5
seconds or more has its answer kept for 1 minute, so its next attempts
take no thread either. One whose DNS fails in under 5 seconds takes a
thread on each attempt
([runtime.md](../runtime.md#outgoing-http-calls)).

Staff's subscriptions share by subscription: one holds at most 2 threads
(its host's limit). Staff who create many can still hold many, for a
first round of slow lookups. Users' own subscriptions share by owner:
8 of one owner's hold 4 of the 16 threads, not all of them.

## The staff API

Staff manage subscriptions over the admin API. It needs
`domainkit.contrib.auth`:

```python
from domainkit.contrib.webhooks.admin import router as webhooks_admin

router.include_router(webhooks_admin, prefix="/admin/webhooks")
```

| Method and path | Permission | What it does |
| --- | --- | --- |
| `GET /subscriptions` | `webhooks.view_subscription` | List subscriptions, oldest first (`limit`, `offset`). |
| `POST /subscriptions` | `webhooks.add_subscription` | Subscribe a URL to `event_types` (201). The answer holds the secret, shown this once. |
| `GET /subscriptions/{id}` | `webhooks.view_subscription` | One subscription. |
| `PATCH /subscriptions/{id}` | `webhooks.change_subscription` | Change `url`, `event_types`, `description`, or `active`. |
| `POST /subscriptions/{id}/rotate-secret` | `webhooks.change_subscription` | A new secret, shown once. |
| `DELETE /subscriptions/{id}` | `webhooks.delete_subscription` | Delete it; its pending deliveries are cancelled. |
| `GET /subscriptions/{id}/deliveries` | `webhooks.view_subscription` | Its deliveries, newest first (`status`, `limit`, `offset`). |
| `GET /deliveries/{id}` | `webhooks.view_subscription` | A delivery, its body (`payload`, `null` once its subjects were erased), and every attempt: status code, time taken, error. |
| `POST /deliveries/{id}/replay` | `webhooks.change_subscription` | Send a finished delivery again (not an erased one). |

Each route also needs a staff user or a superuser. A new subscription
takes 1 to 50 event types (`*` for every event) and a description of at
most 255 characters:

```json
{"url": "https://hooks.example.com/in", "event_types": ["invoice.paid"], "description": "billing", "active": true}
```

```json
{"id": "6f1c...", "url": "https://hooks.example.com/in", "event_types": ["invoice.paid"], "active": true, "description": "billing", "created_at": "2026-10-19T12:00:00Z", "rotating": false, "owner": null, "secret": "whsec_..."}
```

`rotating` is true while an old secret still signs. `owner` is `null`
for a subscription that staff made.

Secrets never appear in listings, logs, events, or reprs. The two
answers that hold a secret are marked `Cache-Control: no-store`. No
cache keeps them, and `IdempotencyMiddleware` stores no copy, so a retry
with the same `Idempotency-Key` gets 409, not the secret again
([idempotency](idempotency.md)).

Each change is recorded as an event that the [audit trail](audit.md)
keeps: `SubscriptionCreated`, `SubscriptionChanged`,
`SubscriptionSecretRotated`, and `SubscriptionDeleted`. These events hold
the URL's host only, because a URL may hold a token. When the URL
changes, `SubscriptionChanged` records the new host and the one before
(`host`, `previous_host`), so the trail shows where events went. For the
same reason, neither a subscription's `repr` nor the API bodies' reprs
show the URL.

## Users' own subscriptions

A platform whose customers register their own webhooks mounts the
self-service API next to its users' routes. Each event then says whose
it is:

```python
from domainkit.contrib.webhooks.owned import router as my_webhooks

router.include_router(my_webhooks, prefix="/webhooks")

# In a DomainConfig.ready():
expose(InvoicePaid, "invoice.paid", owner=lambda event: event.customer_id)
```

It has the subscription routes of the staff API (`GET` and `POST
/subscriptions`; `GET`, `PATCH`, and `DELETE /subscriptions/{id}`; `POST
/subscriptions/{id}/rotate-secret`) and `GET
/subscriptions/{id}/deliveries` (without bodies). Any authenticated user
may call them, on the subscriptions that user made. A subscription's
`owner` is the user's id (`user_owner`). Another user's subscription, or
staff's, is 404, as if it did not exist.

An owned subscription gets only the events whose `owner` is its owner,
never an event exposed without an owner. Staff's subscriptions still get
every event they want.

When subscriptions belong to a tenant, not a user, name the owner both
ways with `owned_router`:

```python
from domainkit.contrib.webhooks.owned import owned_router

router.include_router(
    owned_router(owner=lambda user: str(user.tenant_id)), prefix="/webhooks"
)
expose(InvoicePaid, "invoice.paid", owner=lambda event: event.tenant_id)
```

An owner may have `MAX_SUBSCRIPTIONS_PER_OWNER` subscriptions. Past that,
a create gets 422 `webhook_subscription_limit`. The count is taken after
the insert, in its transaction, one create of an owner at a time, so
creates sent at the same moment cannot pass the limit together. Erasing
an owner (`privacy erase`) deletes their subscriptions.

## Secrets at rest

The subscriptions table holds the current secret, and the old one during
an overlap, encrypted with `domainkit.encryption.SecretBox` (AES-256-GCM,
purpose `domainkit.webhooks.secret`). Each value is bound to its
subscription's id. So the table alone signs nothing, and a value copied
to another row does not decrypt. Only a delivery decrypts the secrets it
signs with, and a rotation the old secret that it keeps.

To replace `SECRET_KEY`, put the old key in `SECRET_KEY_FALLBACKS`.
Deliveries still sign, and each one encrypts its subscription's secrets
again under the new key (a rotation does too). Remove the old key once
every active subscription has had a delivery or a rotation since.

If no key decrypts a secret, because `SECRET_KEY` changed without the old
one in `SECRET_KEY_FALLBACKS`, its deliveries fail at once, unsent. The
reason is in `last_error` and the error log, and the error is reported.
A delivery is never sent unsigned. To fix it, put the old key back, or
rotate the secret (a rotation drops an old secret that no key decrypts,
so there is no overlap) and give receivers the new one. Then run
`webhooks replay --failed`.

## From the command line

| Action | What it does |
| --- | --- |
| `webhooks subscriptions` | List subscriptions: id, state, host only, event types. |
| `webhooks deliveries [--status S] [--subscription ID]` | The newest deliveries: id, state, event type, attempts, last status code, last error. |
| `webhooks replay ID ...` | Send finished deliveries again, under the same `webhook-id` (erased ones excepted). |
| `webhooks replay --failed [--subscription ID]` | The same, for the most recent failed deliveries. |

Every action takes `--limit` (1 to 1,000, default 20). `replay` takes
`--operator` and is audited ([operators](../reference/commands.md#operators)).
An id may be written with or without `msg_`:

```bash
python manage.py webhooks subscriptions
python manage.py webhooks deliveries --limit 1
python manage.py webhooks replay --failed --subscription 6f1c0d4e-6a43-4e58-9a59-5f1e3c3bd2a1
```

```text
6f1c0d4e-6a43-4e58-9a59-5f1e3c3bd2a1  active    hooks.example.com  invoice.paid
2b7f6c1e-1d0a-4c5e-8f40-9a2d7b3c1e55  pending    invoice.paid  x0  -
Queued 1 delivery(ies) again.
```

A delivery that is still pending is left alone, so `replay` may queue
fewer than you named.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `WEBHOOKS` key | Default | Meaning |
| --- | --- | --- |
| `QUEUE` | `"webhooks"` | The messaging queue of deliveries. |
| `MAX_ATTEMPTS` | `12` | Attempts before a delivery is marked failed (at most 100). |
| `RETRY_BASE_SECONDS` | `30.0` | The first retry's delay; it doubles per attempt, with jitter (at most 365 days). |
| `RETRY_MAX_SECONDS` | `21600.0` | The longest retry delay (6 hours; at most 365 days). |
| `TIMEOUT` | `15.0` | Seconds an attempt may take in all: lookup, connection, and answer (at most 120). A delivery task gets 30 more for its database work, which must stay under `MESSAGING.VISIBILITY_TIMEOUT`. |
| `CONNECT_TIMEOUT` | `5.0` | Seconds to connect; at most `TIMEOUT`, and 60. |
| `MAX_RESPONSE_BYTES` | `65536` | The most bytes of an answer read (at most 10 MiB). |
| `MAX_PAYLOAD_BYTES` | `262144` | The largest body (1 KiB to 10 MiB). A larger event is reported and not sent. |
| `ALLOWED_PRIVATE_NETWORKS` | `()` | Private networks that receivers may be in. |
| `CA_BUNDLE` | `None` | Certificates to trust instead of the system's (a private CA). |
| `SECRET_OVERLAP_SECONDS` | `86400.0` | How long the old secret still signs after a rotation (at most 365 days). |
| `RETENTION_SECONDS` | `2592000.0` | How long finished deliveries are kept (30 days). |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | `10` | Subscriptions one owner may make through the self-service API (1 to 1000). |
| `USER_AGENT` | `"domainkit-webhooks"` | The `User-Agent` of deliveries: not empty, and at most 200 characters. |

## Startup checks

The app refuses to start, with `ImproperlyConfigured`, when:

- `SECRET_KEY` is not set;
- cryptography is not installed (`pip install "domainkit[webhooks]"`);
- in staging or production, `ALLOWED_PRIVATE_NETWORKS` reaches loopback
  or link-local addresses;
- `CONNECT_TIMEOUT` is larger than `TIMEOUT`;
- the webhooks tables are routed to a shard group
  (`DATABASE_ROUTING['webhooks']` must name one database).

`setup()` also refuses to start when `domainkit.contrib.messaging` is not
installed.

## On PostgreSQL and SQLite

Webhooks work on PostgreSQL and SQLite, and on no other database. Both
skip a delivery that a second fan-out would write again (`ON CONFLICT DO
NOTHING`).

## Errors and refusals

| Error | When | HTTP |
| --- | --- | --- |
| `UnsafeTarget` | The URL may not be called. | 422, `unsafe_webhook_url` |
| `InvalidEventTypes` | No event types, more than 50, or a bad one. | 422, `invalid_webhook_event_types` |
| `SubscriptionLimitReached` | The owner has `MAX_SUBSCRIPTIONS_PER_OWNER` subscriptions. | 422, `webhook_subscription_limit` |
| `SubscriptionNotFound` | No subscription has that id, or it is not yours. | 404, `webhook_subscription_not_found` |
| `DeliveryNotFound` | No delivery has that id. | 404, `webhook_delivery_not_found` |
| `PayloadTooLarge` | A body larger than `MAX_PAYLOAD_BYTES`. The fan-out reports it once and sends nothing. | none |
| `InvalidSignature` | On the receiving side: `verify` refused the delivery. | your choice; 400 is usual |

## Common mistakes

- **Exposing an event without `payload`.** Every field is sent, personal
  data too. Pick the fields.
- **Not dropping duplicates on the receiving side.** Delivery is at least
  once. Keep the `webhook-id`s you have seen.
- **Changing `SECRET_KEY` without `SECRET_KEY_FALLBACKS`.** Every
  delivery then fails. Keep the old key there until each subscription has
  had a delivery.
- **Expecting owned subscriptions to get an event without `owner`.** Pass
  `owner=` to `expose`, and name owners the same way in `owned_router`.
- **Pointing a test receiver at `localhost`.** It is refused. In
  development, turn on `DEBUG` and use a public tunnel, or allow a private
  network.

## See also

- [Messaging](messaging.md): the outbox, queues, and retries.
- [Outgoing HTTP calls](../runtime.md#outgoing-http-calls): the guard and
  its lookup threads.
- [Privacy](../privacy.md): erasure and export of deliveries.
- [Audit](audit.md): the history of subscription changes.
- [Commands](../reference/commands.md): every command and its options.

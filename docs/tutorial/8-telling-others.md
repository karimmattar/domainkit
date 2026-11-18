# Part 8: telling others

The transfers of [part 6](6-moving-money.md) and [part 7](7-four-eyes.md)
stay inside the bank. Other systems need to hear of them: a customer's
bookkeeping app, or the bank's fraud monitoring. In this part, each
transfer is sent to them as a signed webhook, and you write a receiver
that checks the signature.

## The problem

Telling another service looks like one HTTP call. Done inside the
transfer's request, it goes wrong in several ways:

- **A slow receiver slows the transfer, and a broken one fails it.** The
  transfer waits for an answer it does not need.
- **A call can tell a lie.** The call goes out, then the transfer rolls
  back: the receiver heard of money that never moved. Called after the
  commit instead, the call is lost when the process stops in between.
- **Anyone can call the receiver.** Its URL is no secret. Without a
  signature, a stranger can post a transfer that never happened.
- **A delivery can arrive twice.** A sender that retries after a lost
  answer sends the same event again, and the receiver must not count it
  twice.

The [webhooks app](../contrib/webhooks.md) answers each one. A delivery
goes through the [outbox](4-events.md): it is stored with the change,
and sent by the worker after the commit, again and again with longer
waits until the receiver answers. Each delivery is signed with the
subscription's secret, as the [Standard Webhooks](https://www.standardwebhooks.com)
specification says, and carries the same id on every retry.

## What you will build

- The event `AccountTransferred`, sent as the webhook `transfer.made` to
  every subscription that wants it.
- A receiver, `POST /receiver/transfers`, that refuses a delivery whose
  signature does not match, and takes each delivery once.
- A subscription of the receiver, made through the staff API.
- A test that sends a transfer to the receiver, end to end.

## Step 1: install the app

The webhooks app signs with HMAC, sends with httpx2, and encrypts each
subscription's secret. It needs the `webhooks` extra:

```bash
pip install "domainkit[webhooks]"
```

Add `webhooks` to the extras that `pyproject.toml` lists for domainkit
too. Then add the app at the end of `src/config/settings/base.py`:

```python title="src/config/settings/base.py"
INSTALLED_DOMAINS = [*INSTALLED_DOMAINS, "domainkit.contrib.webhooks"]
```

A webhook never goes to an address on your own network, unless you
allow it: a subscriber must not make the bank call its internal
services. On your machine, the receiver is the project itself, so allow
this machine, in the settings of this machine only. Add these lines at
the end of `src/config/settings/local.py`:

```python title="src/config/settings/local.py"
# Webhooks may go to this machine: to the receiver, src/config/receiver.py.
WEBHOOKS = {"ALLOWED_PRIVATE_NETWORKS": ["127.0.0.0/8"]}
```

Make the app's tables:

```bash
python manage.py makemigrations -m "webhooks"
python manage.py migrate
```

## Step 2: send the event

An event is sent once the domain exposes it, under a name that
subscribers choose. In `src/domains/accounts/apps.py`, replace `ready()`
with this one. The guard is as before; `expose` is new:

```python title="src/domains/accounts/apps.py"
from domainkit.contrib.webhooks import expose

from domains.accounts.domain import AccountTransferred


class AccountsConfig(DomainConfig):
    ...

    def ready(self) -> None:
        """Map the tables, subscribe the handlers, guard transfers, send them."""
        super().ready()
        guard(
            "accounts.transfer",
            controllers.transfer_account,
            unit_of_work=SqlAlchemyAccountsUnitOfWork,
            policies=[
                Policy(
                    "accounts.large_transfer",
                    permission="accounts.approve_transfer",
                    when=lambda command: LARGE.exceeded_by([command.amount]),
                ),
            ],
            permission="accounts.change_account",
            submit=True,
        )
        expose(
            AccountTransferred,
            "transfer.made",
            payload=lambda event: {
                "transfer_id": str(event.transfer_id),
                "account_id": str(event.account_id),
                "amount": {
                    "amount": str(event.amount.amount),
                    "currency": event.amount.currency.code,
                },
            },
        )
```

`payload` chooses what the receiver gets. Without it, every field of the
event is sent, the payee too: leave out what the receiver does not need,
because it keeps what it gets.

## Step 3: write the receiver

In real life, another team runs the receiver, in another service. Here
it is a route of your project, so you can watch a delivery arrive. Make
the new file `src/config/receiver.py`:

```python title="src/config/receiver.py (new file)"
"""A service that hears of the bank's transfers: a receiver of its webhooks."""

import json
import logging
from typing import Any

from domainkit.conf import Env
from domainkit.contrib.webhooks import InvalidSignature, verify
from domainkit.http.access import Public
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
logger = logging.getLogger(__name__)
received: dict[str, Any] = {}
"""Each delivery taken, by its id. A real receiver keeps them in its database."""


@router.post("/receiver/transfers", dependencies=[Public], status_code=204)
async def receive_transfer(request: Request) -> None:
    """Take a delivery signed with the subscription's secret, once."""
    body = await request.body()
    try:
        delivery = verify(Env().str("RECEIVER_SECRET"), request.headers, body)
    except InvalidSignature:
        raise HTTPException(400, "The signature does not match.") from None
    if delivery not in received:
        received[delivery] = json.loads(body)["data"]
        logger.info("Transfer %s made", received[delivery]["transfer_id"])
```

- The route is `Public`: the bank calls it with no login. The signature
  proves the sender instead.
- `verify` reads the `webhook-id`, `webhook-timestamp`, and
  `webhook-signature` headers. It refuses a signature that is not the
  HMAC-SHA256 of the id, the time, and the body under the secret, and a
  time more than five minutes away, so an old delivery cannot be played
  again later. It returns the delivery's id.
- The id is the same on every retry of a delivery: the receiver takes a
  delivery it has already taken as done, and changes nothing.
- The secret comes from the environment variable
  `BANK_API_RECEIVER_SECRET` (`Env` adds the project's prefix).

Mount the receiver, and the staff API that manages subscriptions. Add
these lines at the end of `src/config/urls.py`:

```python title="src/config/urls.py"
from domainkit.contrib.webhooks.admin import router as webhooks_router

from config.receiver import router as receiver_router

router.include_router(webhooks_router, prefix="/admin/webhooks")
router.include_router(receiver_router)
```

## Step 4: subscribe, and make a transfer

```bash
python manage.py runserver
```

In the second shell, as the superuser, subscribe the receiver to
`transfer.made`. The answer holds the subscription's secret, shown this
once: keep it in `$BANK_API_RECEIVER_SECRET`, the variable the receiver
reads:

```bash
BANK_API_RECEIVER_SECRET=$(curl -s http://127.0.0.1:8000/admin/webhooks/subscriptions -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "http://127.0.0.1:8000/receiver/transfers", "event_types": ["transfer.made"]}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['secret'])")
```

The secret starts with `whsec_`. Send 100 dollars:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/transfer -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 9a7c5e31-4b2d-4f60-8e1a-2c3d4e5f6a7b" -H "Content-Type: application/json" \
  -d '{"payee": "DE89370400440532013000", "amount": {"amount": "100.00", "currency": "USD"}}'
```

The transfer answered at once: nothing was sent while it ran. Stop the
server, and look at the subscription and the outbox:

```bash
python manage.py webhooks subscriptions
python manage.py messages list pending --limit 10
```

The subscription is `active`, for the host `127.0.0.1` and the event
`transfer.made`. The outbox holds the transfer's two reactions: its
entry in the books (`accounts.book_transfer`) and the webhook
(`domainkit.contrib.webhooks.fan_out`). The worker turns the webhook
into one delivery per subscription, and sends each one.

To watch it arrive, give the receiver the secret: add a line
`BANK_API_RECEIVER_SECRET=whsec_...`, with the secret you kept, to the
project's `.env` file, which `Env` reads too and which is never
committed. Start the server, and run `python manage.py runworker` in a
third shell. The server's log shows `Transfer ... made`, and
`python manage.py webhooks deliveries` shows the delivery `succeeded`. A
receiver that is down gets the delivery later: the worker tries again
after 30 seconds, then waits twice as long each time, up to 12 tries.

## Step 5: test it

The test serves the project on a port of its own (`live_server`), so the
worker's delivery reaches the receiver through a real connection, and
never leaves the machine. Make the new file `tests/test_receiver.py`:

```python title="tests/test_receiver.py (new file)"
"""A transfer reaches the receiver signed; a forged or repeated one changes nothing."""

import json

import pytest
from domainkit import clock
from domainkit.contrib.messaging.testing import Outbox
from domainkit.contrib.webhooks import new_secret, sign
from domainkit.testing import LiveServer, SettingsOverrides, TestClient

from config import receiver

URL = "/api/v1/accounts/accounts"


def test_a_transfer_reaches_the_receiver(
    admin_client: TestClient,
    live_server: LiveServer,
    outbox: Outbox,
    settings: SettingsOverrides,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscribed to transfer.made, the receiver gets the transfer, verified."""
    settings.DEBUG = True  # Webhooks to http:// addresses, as on your machine.
    settings.WEBHOOKS = {"ALLOWED_PRIVATE_NETWORKS": ["127.0.0.0/8"]}
    monkeypatch.setattr(receiver, "received", {})
    subscription = admin_client.post(
        "/admin/webhooks/subscriptions",
        json={
            "url": f"{live_server.url}/receiver/transfers",
            "event_types": ["transfer.made"],
        },
    ).json()
    monkeypatch.setenv("BANK_API_RECEIVER_SECRET", subscription["secret"])
    item = admin_client.post(URL, json={"name": "Savings"}).headers["location"]
    admin_client.post(
        f"{item}/deposit", json={"amount": {"amount": "500.00", "currency": "USD"}}
    )
    hundred = {"amount": "100.00", "currency": "USD"}

    admin_client.post(f"{item}/transfer", json={"payee": "DE89", "amount": hundred})
    outbox.run_queued()

    [data] = receiver.received.values()
    assert data["amount"] == hundred
    assert data["account_id"] == item.rsplit("/", 1)[1]


def test_a_forged_or_repeated_delivery_changes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another secret's signature is refused; the same delivery twice counts once."""
    secret = new_secret()
    monkeypatch.setenv("BANK_API_RECEIVER_SECRET", secret)
    monkeypatch.setattr(receiver, "received", {})
    body = json.dumps({"type": "transfer.made", "data": {"transfer_id": "t-1"}})
    now = int(clock.now().timestamp())
    signed = {
        "webhook-id": "msg_1",
        "webhook-timestamp": str(now),
        "webhook-signature": sign(secret, "msg_1", now, body.encode()),
    }
    forgery = sign(new_secret(), "msg_1", now, body.encode())
    forged = {**signed, "webhook-signature": forgery}

    answers = [
        client.post("/receiver/transfers", content=body, headers=headers).status_code
        for headers in (forged, signed, signed)
    ]

    assert answers == [400, 204, 204]
    assert list(receiver.received) == ["msg_1"]
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format .
pytest
```

pytest prints `23 passed`.

## What you learned

- `expose` sends an event's committed instances as a webhook to each
  subscription that wants it. `payload` chooses what leaves the bank.
- Deliveries go through the outbox: never before the commit, never lost,
  and tried again until the receiver answers.
- A receiver verifies each delivery's signature with `verify`, and takes
  a delivery's id once.
- A webhook never goes to your own network unless
  `ALLOWED_PRIVATE_NETWORKS` allows it.

## Where next

Your bank now moves money once, within its limits, with a second person
for large amounts, and tells others about it. From here:

- [`domainkit init --profile fintech`](../fintech.md) writes a whole
  project with these controls on: step-up, transactions authorized for
  their amount and payee, approvals, limits, the books, encrypted card
  numbers, and their tests.
- [Accounting](../contrib/accounting.md): accounting periods, holds,
  reversals, the trial balance, and reconciliation with a bank's
  statements.
- [Approvals](../contrib/approvals.md): several approvals, approver
  groups, rejecting and cancelling requests.
- [Webhooks](../contrib/webhooks.md): users' own subscriptions, rotating
  a secret, and replaying deliveries.
- [Deploying](../deploying.md): what production needs of each app, such
  as a sealed and anchored audit trail.

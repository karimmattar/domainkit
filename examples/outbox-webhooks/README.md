# Outbox and webhooks: tell others once it committed

A transfer, once saved, must reach the bank, and the services that
subscribed must hear that it was sent. Both go through the outbox, and
the webhook is signed.

## The problem

A clerk requests a transfer to a supplier. Two others must hear of it:
the bank, which moves the money, and the supplier's own system, which
waits for `transfer.sent`. Called from the request that saves the
transfer, each call goes wrong in its own way:

- the call goes out, then the transaction rolls back: the bank pays a
  transfer that the books never kept;
- the transaction commits, then the call fails, or the process dies: the
  bank never hears of it, and nothing tries again;
- the supplier's endpoint cannot tell a webhook sent by you from one that
  anyone posted to its URL, or from an old one sent again.

This example commits the transfer and its event in one transaction, and
the event's reactions with them, in the
[outbox](../../docs/contrib/messaging.md). A worker sends the transfer to
the bank, tries again when the bank fails, and after the last attempt
leaves it as a dead letter for an operator. When the bank takes it,
[webhooks](../../docs/contrib/webhooks.md) delivers `transfer.sent` to
each subscriber, signed. The receiver checks the signature, and keeps
each delivery once.

## What is here

`domainkit init transfersAPI` made the project, and `manage.py
startdomain transfers` its domain. These files were then changed:

| File | What changed |
| --- | --- |
| `src/config/settings/base.py` | The webhooks app is installed, and the bank's URL is a setting of the domain's (`TRANSFERS`). |
| `src/config/settings/test.py` | Webhooks may reach 127.0.0.1, and a message is tried three times; the tests run on PostgreSQL when `TRANSFERS_API_TEST_DATABASE_URL` names one. |
| `src/config/urls.py` | The staff's webhook API at `/admin/webhooks`, and the transfers at `/api/v1`. |
| `src/domains/transfers/domain.py` | The `Transfer` aggregate, its two events, and the `Bank` port. |
| `src/domains/transfers/controllers.py` | Requesting a transfer, and sending it to the bank. |
| `src/domains/transfers/models.py` | The table, the bank's adapter, and the durable handler. |
| `src/domains/transfers/apps.py` | The handler subscribed, and `transfer.sent` exposed as a webhook. |
| `src/receiver.py` | The other side: an endpoint that verifies each delivery. |
| `tests/` | The tests, a fake bank, and the receiver served on 127.0.0.1 (`tests/conftest.py`). |
| `pyproject.toml`, `.github/workflows/ci.yml` | mypy, strict, as `init --profile fintech` adds it; the `httpclient` and `webhooks` extras. |

## Run it

From a checkout of domainkit:

```bash
cd examples/outbox-webhooks
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e "../..[httpclient,postgres,redis,sqlite,testing,webhooks]"
pip install --group dev
export TRANSFERS_API_SETTINGS_MODULE=config.settings.local
python manage.py makemigrations -m "transfers"
python manage.py migrate
pytest
```

The example holds no migrations: `makemigrations` writes them for the
domainkit you installed. In a project of your own, made from this one,
commit the migrations it writes. `init` writes a `.env` that names the
settings module, and `.env` is never committed, so the `export` line does
its job. Run `python manage.py runworker` beside `runserver`: the worker
sends transfers and delivers webhooks.

## The request commits; the worker tells

A transfer records `TransferRequested` when it is made. In
`src/domains/transfers/domain.py`:

```python
@classmethod
def request(cls, payee: str, iban: str, amount: Money) -> Self:
    """Ask for ``amount`` to go to ``payee``'s account ``iban``."""
    if not payee.strip():
        raise InvalidTransfer("Name the payee.")
    if amount.amount <= 0:
        raise InvalidTransfer("A transfer moves more than nothing.")
    transfer = cls(
        id=TransferId(uuid4()), payee=payee.strip(), iban=iban, amount=amount
    )
    transfer.record(TransferRequested(transfer_id=transfer.id))
    return transfer
```

The domain subscribes a durable handler to that event. In
`src/domains/transfers/apps.py`:

```python
event_bus.subscribe(
    TransferRequested,
    send_transfer,
    durable=True,
    name="transfers.send_to_bank",
)
```

`durable=True` writes a message to the outbox, a table in the same
database, in the transaction that commits the transfer. So the message
exists if and only if the transfer does: a refused or rolled-back
request leaves none, and a committed one keeps its message whatever
happens to the process next. The worker claims the message and runs the
handler. The handler's name is what the message carries, so moving the
function to another module breaks nothing.

## Sending to the bank, at least once

The worker may run a message twice: it died after the bank answered but
before it recorded the message as done. In
`src/domains/transfers/controllers.py`:

```python
async def send_to_bank(uow: TransfersUnitOfWork, bank: Bank, transfer_id: UUID) -> None:
    """Hand a requested transfer to the bank, then record that it was sent.

    No transaction is open while the bank answers. The worker may run this
    twice (it died after the bank's answer, before the commit): the bank
    keys the transfer by its id, so the second call moves nothing more, and
    the handler's inbox keeps its commit to one.
    """
    async with uow:
        transfer = await uow.transfers.get(TransferId(transfer_id))
    reference = await bank.send(transfer)
    async with uow:
        transfer = await uow.transfers.get(TransferId(transfer_id))
        transfer.mark_sent(reference)
        await uow.commit()
```

Two things make a second run harmless:

- **The bank's key.** The adapter sends the transfer's id as the
  `Idempotency-Key` (`HttpBank` in `models.py`), so the bank moves the
  money once, however often it is asked.
- **The inbox.** The handler is marked `@inbox`. Its commit writes the
  message's id beside the change, so a message run again after that
  commit is skipped, and `TransferSent` is recorded once.

```python
@inbox
async def send_transfer(event: TransferRequested) -> None:
    """Send a requested transfer to the bank (the durable handler)."""
    await controllers.send_to_bank(
        SqlAlchemyTransfersUnitOfWork(), HttpBank(), event.transfer_id
    )
```

## Retries and dead letters

When the bank answers 503, `raise_for_status()` fails the attempt. The
HTTP client does not try the call again itself; the worker runs the whole
message again later, after a wait that doubles each time: half to all of
2 seconds after the first attempt, of 4 seconds after the second, and so
on, 256 to 512 seconds after the ninth. After `MESSAGING["MAX_ATTEMPTS"]`
attempts (10; the tests use 3), some 8 to 17 minutes from the first, the
message is a dead letter. The one-hour cap on a wait
(`RETRY_MAX_SECONDS`) is not reached at these defaults. A dead letter
stays in the outbox, and an error report goes to `ERROR_REPORTERS`. Once
the bank is back, an operator sends it again, with fresh attempts:

```bash
python manage.py messages list dead
python manage.py messages retry all
```

The retried message carries the same transfer, so the bank gets the same
key. After 5 failures in a row the HTTP client opens the bank's circuit:
for 30 seconds its calls fail at once, without reaching the bank, and an
attempt made then still counts as one of the message's ten
([outgoing calls](../../docs/runtime.md#outgoing-http-calls)).

A bank that refuses a transfer for good (a 4xx: an IBAN it does not
know) is tried again too, and the message ends as a dead letter, where an
operator reads why. A durable handler takes no `dont_retry_on`; a project
that wants such refusals to stop at once records them on the transfer
(a `refused` status) instead of raising, or does the call in a task,
whose `dont_retry_on` dead-letters them at once
([task options](../../docs/contrib/messaging.md#task-options)).

## The webhook, signed

The domain exposes `TransferSent` as the event type `transfer.sent`, and
chooses what its body holds. In `src/domains/transfers/apps.py`:

```python
expose(
    TransferSent,
    "transfer.sent",
    payload=lambda event: {
        "transfer_id": str(event.transfer_id),
        "amount": str(event.amount.amount),
        "currency": event.amount.currency.code,
        "bank_reference": event.bank_reference,
    },
)
```

When `TransferSent` commits, a durable handler of the webhooks app writes
one delivery per subscription that wants `transfer.sent`, and a task for
each. Staff subscribe a URL at `POST /admin/webhooks/subscriptions`; the
answer holds the subscription's secret, shown once. Each attempt is
signed as [Standard Webhooks](https://www.standardwebhooks.com) sign: a
`webhook-id`, the same on every attempt; a `webhook-timestamp`; and a
`webhook-signature`, the HMAC-SHA256 of the id, the time, and the body,
under the secret. A delivery whose receiver does not answer 2xx is tried
again (`WEBHOOKS["MAX_ATTEMPTS"]`, 12), then marked failed. Staff fix
the subscription and replay it (`POST /admin/webhooks/deliveries/{id}/replay`,
or `python manage.py webhooks replay --failed`), under the same
`webhook-id`.

## The receiver

The supplier's system runs elsewhere, on any framework. `src/receiver.py`
is such an endpoint, in FastAPI, with domainkit's `verify`:

```python
async def receive(self, request: Request) -> Response:
    """Take a delivery: 204 if it is signed, 400 if it is not."""
    body = await request.body()
    try:
        delivery_id = verify(self.secret, request.headers, body)
    except InvalidSignature:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    self.events.setdefault(delivery_id, json.loads(body))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

`verify` compares the signature in constant time, and refuses a
timestamp more than 5 minutes away: a delivery captured and sent again
later fails. A `Receiver` cannot be made without its secret: `verify`
refuses an empty one, so every delivery would fail instead of being
verified. The same delivery tried twice has one `webhook-id`, so the
receiver keeps it once. A real receiver keeps the ids in its database,
unique, and acts on a delivery the first time only.

## Tests without a network

Nothing leaves the machine. The bank is a function that every call of
`domainkit.http.client` reaches instead (`stub`). The receiver is served
on a port of the test process (`LiveServer`), where the worker, run in
the test by `drain`, delivers over HTTP. In `tests/conftest.py`:

```python
@pytest.fixture
def bank() -> Iterator[FakeBank]:
    """The bank, for every call the test's process makes."""
    fake = FakeBank()
    with stub(fake, everywhere=True):
        yield fake
```

```python
@pytest.fixture
def partner(settings: SettingsOverrides) -> Iterator[tuple[Receiver, str]]:
    """The receiving service, served on 127.0.0.1, and its URL.

    Webhooks go to ``https`` URLs only, unless ``DEBUG`` is on, as on a
    developer's machine.
    """
    settings.DEBUG = True
    receiver = Receiver(new_secret())  # The subscription's, once it is made.
    with LiveServer(receiver.app) as server:
        yield receiver, server.url
```

The test settings let webhooks reach 127.0.0.1, which deployments refuse
(`WEBHOOKS["ALLOWED_PRIVATE_NETWORKS"]`).

## What the tests prove

| Test | What it shows |
| --- | --- |
| `test_a_transfer_is_told_once_it_commits` | The request calls nobody; the worker calls the bank once, keyed, then delivers the signed webhook. |
| `test_nothing_is_told_of_a_transfer_that_did_not_commit` | A refused transfer leaves nothing to send. |
| `test_a_failing_bank_is_tried_again_then_left_for_an_operator` | Three attempts, a dead letter, `messages retry`, and the money moved once. |
| `test_a_worker_that_dies_before_its_commit_moves_the_money_once` | The bank answered, the commit never came: the bank is asked again under the same key, and one webhook goes out. |
| `test_a_failed_delivery_is_tried_again_then_replayed` | A receiver that answers 404 fails each attempt; staff fix the URL and replay it. |
| `tests/test_receiver.py` | The receiver keeps a signed delivery once, refuses another secret, a changed body, an old timestamp, and no signature, and needs its secret. |

domainkit's CI runs them on SQLite and on PostgreSQL, with the project's
ruff, mypy, `check`, and migrations, for every change to the framework.

## Read next

- [Messaging](../../docs/contrib/messaging.md): tasks, ordering keys,
  schedules, and workers on RabbitMQ.
- [Webhooks](../../docs/contrib/webhooks.md): which URLs may be called,
  rotating a secret, and users' own subscriptions.
- [Payments ledger](../payments-ledger/README.md): wallets in
  double-entry books.

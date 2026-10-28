# A payment that runs again when it loses a race

A payment moves money between two wallets: it reads both balances,
checks one, and changes both. Two payments from the same wallet at once
must not both pass the check. This recipe makes the payment one
`SERIALIZABLE` transaction that locks both rows in id order, and runs
again, whole, when the database says it lost a race
([ADR 0092](../adr/0092-retry-units-of-work-choose-isolation-and-lock-in-order.md),
[databases.md](../databases.md#running-work-again)).

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## A wallet with a balance

```bash
domainkit init payAPI pay
cd pay
python manage.py startdomain wallets --aggregate Wallet --minimal
python manage.py addcommand wallets Wallet DepositWallet --field amount:decimal
python manage.py addcommand wallets Wallet PayWallet --field to:ref:Wallet --field amount:decimal --event WalletPaid
```

`addcommand` wrote each command, its use case, its route
(`POST /wallets/{wallet_id}/deposit` and `.../pay`), and a method on the
aggregate that records the event; the rule and the change are yours. Give
the wallet a balance, and write them:

```python title="src/domains/wallets/domain/model.py"
from decimal import Decimal

from domainkit.ddd import Digits


class InvalidAmount(BusinessRuleViolation):
    """Amounts are positive."""


class InsufficientFunds(BusinessRuleViolation):
    """A wallet pays at most what it holds."""


class Wallet:
    ...
    balance: Annotated[Decimal, Digits(12, 2)] = Decimal("0.00")

    def deposit(self, *, amount: Decimal) -> None:
        """Add ``amount`` to the balance."""
        self.balance += _positive(amount)
        self.record(WalletDeposited(wallet_id=self.id, amount=amount))

    def pay(self, *, to: WalletId, amount: Decimal) -> None:
        """Take ``amount`` from the balance, for the wallet ``to``."""
        if _positive(amount) > self.balance:
            raise InsufficientFunds(f"Wallet {self.id} holds less than {amount}.")
        self.balance -= amount
        self.record(WalletPaid(wallet_id=self.id, to=to, amount=amount))

    def receive(self, *, amount: Decimal) -> None:
        """Add a payment to the balance."""
        self.balance += _positive(amount)


def _positive(amount: Decimal) -> Decimal:
    """The amount, if it is more than zero."""
    if amount <= 0:
        raise InvalidAmount("An amount must be more than zero.")
    return amount
```

`Digits(12, 2)` makes the column an exact decimal, in cents on SQLite.

## The payment, in one transaction that may run again

The use case changes two aggregates: it locks both rows in one statement,
by ascending id (`get_many_for_update`), so two payments between the same
wallets take the locks in the same order and cannot deadlock. `uow.run`
runs the whole function again when its transaction loses a race: on
PostgreSQL a serialization failure or a deadlock, and the errors
`retry_on` names. Only a run that commits publishes its events.

```python title="src/domains/wallets/application/services.py"
from domainkit.ddd import ConcurrentUpdate, Retry

RACES = Retry(attempts=5, retry_on=(ConcurrentUpdate,))
"""Up to five runs; each next one after a short random wait."""


async def pay_wallet(command: PayWallet, uow: WalletsUnitOfWork) -> Wallet:
    """Pay from one wallet into another, whole or not at all."""
    source, target = WalletId(command.wallet_id), WalletId(command.to)

    async def work() -> Wallet:
        async with uow:
            wallets = await uow.wallets.get_many_for_update([source, target])
            wallets[source].pay(to=target, amount=command.amount)
            wallets[target].receive(amount=command.amount)
            await uow.commit()
        return wallets[source]

    return await uow.run(work, retry=RACES)
```

The work enters the unit of work itself: `run` never runs part of an
`async with` block again. What a failed run wrote went with its
transaction. Work that calls outside its database, such as a card
processor, must not be run again: queue it as a task instead
([messaging](../contrib/messaging.md)).

On PostgreSQL, make the transaction `SERIALIZABLE`, so a payment that
could not have run one after the other fails and runs again, instead of
breaking a rule across rows. On SQLite such units of work begin with
`BEGIN IMMEDIATE` and run one at a time:

```python title="src/domains/wallets/infrastructure/unit_of_work.py"
from domainkit.db import Isolation


class SqlAlchemyWalletsUnitOfWork:
    ...
    isolation = Isolation.SERIALIZABLE
```

A command route can run its service again the same way, with the reach
check and `If-Match` made again in each run:
`command_route(..., retry=Retry())` ([resources.md](../resources.md#commands)).

## The table, and the tests

```bash
python manage.py makemigrations -m "create wallets"
python manage.py migrate
```

Replace the tests `addcommand` wrote for the payment, and add one in
which the first commit loses a race:

```python title="tests/domains/wallets/test_pay_wallet.py"
from domainkit.ddd import ConcurrentUpdate

from domains.wallets.domain.model import Wallet


async def funded(uow: FakeWalletsUnitOfWork, amount: str) -> Wallet:
    """A stored wallet holding ``amount``."""
    wallet = new_wallet()
    wallet.receive(amount=Decimal(amount))
    async with uow:
        await uow.wallets.add(wallet)
        await uow.commit()
    return wallet


@pytest.mark.anyio
async def test_pay_wallet_publishes_wallet_paid() -> None:
    """The money moves, in one commit; WalletPaid follows."""
    published: list[DomainEvent] = []
    bus = EventBus()

    async def collect(event: WalletPaid) -> None:
        published.append(event)

    bus.subscribe(WalletPaid, collect)
    uow = FakeWalletsUnitOfWork(bus)
    source, target = await funded(uow, "10.00"), await funded(uow, "0.01")

    command = PayWallet(wallet_id=source.id, to=target.id, amount=Decimal("2.50"))
    await services.pay_wallet(command, uow)

    assert (source.balance, target.balance) == (Decimal("7.50"), Decimal("2.51"))
    assert uow.commits == 3
    assert [type(each) for each in published] == [WalletPaid]


@pytest.mark.anyio
async def test_a_payment_that_loses_a_race_runs_again_whole() -> None:
    """The first commit fails; the second run moves the money, once."""

    class LosesOnce(FakeWalletsUnitOfWork):
        lost = False

        async def _commit(self) -> None:
            if self.commits == 2 and not self.lost:  # The payment's first commit.
                self.lost = True
                raise ConcurrentUpdate("Another payment changed the wallet first.")
            await super()._commit()

    uow = LosesOnce()
    source, target = await funded(uow, "10.00"), await funded(uow, "0.01")

    command = PayWallet(wallet_id=source.id, to=target.id, amount=Decimal("4.00"))
    await services.pay_wallet(command, uow)

    assert (uow.lost, uow.commits) == (True, 3)
    assert (source.balance, target.balance) == (Decimal("6.00"), Decimal("4.01"))


def test_pay_wallet_route(admin_client: TestClient) -> None:
    """A payment past the balance is refused, 422, and nothing moves."""
    ids = [admin_client.post(URL, json=new_wallet_request()).json()["id"] for _ in "ab"]
    admin_client.post(f"{URL}/{ids[0]}/deposit", json={"amount": "5.00"})

    payment = {"to": ids[1], "amount": "3.00"}
    paid = admin_client.post(f"{URL}/{ids[0]}/pay", json=payment)
    refused = admin_client.post(f"{URL}/{ids[0]}/pay", json=payment)

    assert paid.json()["balance"] == "2.00"
    assert (refused.status_code, refused.json()["code"]) == (422, "insufficient_funds")
    assert admin_client.get(f"{URL}/{ids[1]}").json()["balance"] == "3.00"
```

```bash
ruff check --fix .
ruff format --check .
pytest
```

`ruff --fix` sorts the imports the edits added, and drops the one the old
tests needed. Run the tests on PostgreSQL too, where races are real: point the test
settings' `DATABASES` at a server ([testing.md](../testing.md)).

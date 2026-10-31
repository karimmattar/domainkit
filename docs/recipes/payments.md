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
python manage.py addcommand wallets Wallet DepositWallet --field amount:money
python manage.py addcommand wallets Wallet PayWallet --field to:ref:Wallet --field amount:money --event WalletPaid
```

`addcommand` wrote each command, its use case, its route
(`POST /wallets/{wallet_id}/deposit` and `.../pay`), and a method on the
aggregate that records the event; the rule and the change are yours. An
amount is `Money` ([money.md](../money.md)): an exact amount and its
currency, `{"amount": "5.00", "currency": "USD"}` in JSON. Give the wallet
a balance, and write them:

```python title="src/domains/wallets/domain/model.py"
from domainkit.ddd import Digits
from domainkit.money import Money


class InvalidAmount(BusinessRuleViolation):
    """Amounts are positive."""


class InsufficientFunds(BusinessRuleViolation):
    """A wallet pays at most what it holds."""


class Wallet:
    ...
    balance: Annotated[Money, Digits(12, 2)] = Money.zero("USD")

    def deposit(self, *, amount: Money) -> None:
        """Add ``amount`` to the balance."""
        self.balance += _positive(amount)
        self.record(WalletDeposited(wallet_id=self.id, amount=amount))

    def pay(self, *, to: WalletId, amount: Money) -> None:
        """Take ``amount`` from the balance, for the wallet ``to``."""
        if _positive(amount) > self.balance:
            raise InsufficientFunds(f"Wallet {self.id} holds less than {amount}.")
        self.balance -= amount
        self.record(WalletPaid(wallet_id=self.id, to=to, amount=amount))

    def receive(self, *, amount: Money) -> None:
        """Add a payment to the balance."""
        self.balance += _positive(amount)


def _positive(amount: Money) -> Money:
    """The amount, if it is more than zero."""
    if amount.amount <= 0:
        raise InvalidAmount("An amount must be more than zero.")
    return amount
```

The balance is two columns: `balance`, an exact decimal of `Digits(12, 2)`
(in cents on SQLite), and `balance_currency`. Amounts of another currency
cannot be added, subtracted, or compared: `pay` with euros from a wallet of
dollars raises `CurrencyMismatch`, which answers 422 `currency_mismatch`.
An amount with more places than its currency (`"1.005"` dollars) is
refused, never rounded.

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
from domainkit.money import Money

from domains.wallets.domain.model import Wallet


def usd(amount: str) -> Money:
    """``amount`` US dollars."""
    return Money.of(amount, "USD")


async def funded(uow: FakeWalletsUnitOfWork, amount: str) -> Wallet:
    """A stored wallet holding ``amount`` US dollars."""
    wallet = new_wallet()
    wallet.receive(amount=usd(amount))
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

    command = PayWallet(wallet_id=source.id, to=target.id, amount=usd("2.50"))
    await services.pay_wallet(command, uow)

    assert (source.balance, target.balance) == (usd("7.50"), usd("2.51"))
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

    command = PayWallet(wallet_id=source.id, to=target.id, amount=usd("4.00"))
    await services.pay_wallet(command, uow)

    assert (uow.lost, uow.commits) == (True, 3)
    assert (source.balance, target.balance) == (usd("6.00"), usd("4.01"))


def test_pay_wallet_route(admin_client: TestClient) -> None:
    """A payment past the balance is refused, 422, and nothing moves."""
    ids = [admin_client.post(URL, json=new_wallet_request()).json()["id"] for _ in "ab"]
    five = {"amount": "5.00", "currency": "USD"}
    admin_client.post(f"{URL}/{ids[0]}/deposit", json={"amount": five})

    payment = {"to": ids[1], "amount": {"amount": "3.00", "currency": "USD"}}
    paid = admin_client.post(f"{URL}/{ids[0]}/pay", json=payment)
    refused = admin_client.post(f"{URL}/{ids[0]}/pay", json=payment)

    assert paid.json()["balance"] == {"amount": "2.00", "currency": "USD"}
    assert (refused.status_code, refused.json()["code"]) == (422, "insufficient_funds")
    assert admin_client.get(f"{URL}/{ids[1]}").json()["balance"]["amount"] == "3.00"
```

```bash
ruff check --fix .
ruff format .
pytest
```

`ruff --fix` sorts the imports the edits added, and drops the one the old
tests needed; `ruff format` closes the gap it leaves.

A new aggregate can start with all of this written: `startaggregate
wallets Purse --field balance:money` makes the balance, a transfer that
locks both rows in id order and runs again when it loses a race
(`TransferPurseBalance`, `POST /purses/{purse_id}/transfer-balance`), and
its tests ([resources.md](../resources.md#generators)). Run the tests on PostgreSQL too, where races are real: point the test
settings' `DATABASES` at a server ([testing.md](../testing.md)).

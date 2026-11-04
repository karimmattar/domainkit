# A payment that runs again when it loses a race

This recipe writes a payment between two wallets that stays correct when
two payments run at the same time.

## The problem

A payment moves money between two wallets. It reads both balances,
checks that the payer has enough, and changes both. Now two payments
from the same wallet run at the same time. Each one reads the old
balance, each one passes the check, and the wallet pays more than it
holds.

The solution has three parts
([ADR 0092](../adr/0092-retry-units-of-work-choose-isolation-and-lock-in-order.md),
[databases.md](../databases.md#running-work-again)):

- the payment is one `SERIALIZABLE` transaction;
- it locks both rows in id order, so two payments never deadlock;
- it runs again, whole, when the database says it lost a race.

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a wallet with two commands

Make a project, a `wallets` domain, and two commands: a deposit, and a
payment to another wallet:

```bash
domainkit init payAPI pay
cd pay
python manage.py startdomain wallets --aggregate Wallet --minimal
python manage.py addcommand wallets Wallet DepositWallet --field amount:money
python manage.py addcommand wallets Wallet PayWallet --field to:ref:Wallet --field amount:money --event WalletPaid
```

`addcommand` wrote each command, its use case, its route
(`POST /wallets/{wallet_id}/deposit` and `.../pay`), and a method on the
aggregate that records the event. The rule and the change are yours to
write.

### Step 2: give the wallet a balance and its rules

An amount is `Money` ([money.md](../money.md)): an exact amount and its
currency, `{"amount": "5.00", "currency": "USD"}` in JSON. Give the
wallet a balance, and write the methods:

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

The balance is two columns: `balance`, an exact decimal of
`Digits(12, 2)` (in cents on SQLite), and `balance_currency`. Amounts of
two currencies cannot be added, subtracted, or compared: `pay` with euros
from a wallet of dollars raises `CurrencyMismatch`, which answers 422
`currency_mismatch`. An amount with more places than its currency
(`"1.005"` dollars) is refused, never rounded.

### Step 3: write the payment as work that may run again

The use case changes two aggregates. It locks both rows in one statement,
by ascending id (`get_many_for_update`). So two payments between the same
wallets take the locks in the same order, and cannot deadlock. `uow.run`
runs the whole function again when its transaction loses a race:

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
`async with` block again.

### Step 4: make the transaction `SERIALIZABLE`

On PostgreSQL, a `SERIALIZABLE` transaction fails when it could not have
run one after the other with the payments around it; the payment then
runs again, instead of breaking a rule across rows. On SQLite, such units
of work begin with `BEGIN IMMEDIATE`, and run one at a time:

```python title="src/domains/wallets/infrastructure/unit_of_work.py"
from domainkit.db import Isolation


class SqlAlchemyWalletsUnitOfWork:
    ...
    isolation = Isolation.SERIALIZABLE
```

### Step 5: make the table

```bash
python manage.py makemigrations -m "create wallets"
python manage.py migrate
```

### Step 6: test the payment, and a lost race

Replace the tests that `addcommand` wrote for the payment, and add one in
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

Run the checks and the tests:

```bash
ruff check --fix .
ruff format .
pytest
```

`ruff --fix` sorts the imports the edits added, and drops the one the
old tests needed; `ruff format` closes the gap it leaves.

## How it works

1. `uow.run(work, retry=RACES)` calls `work`.
2. `work` opens the unit of work, locks both wallets, moves the money,
   and commits.
3. If the commit loses a race (on PostgreSQL, a serialization failure or
   a deadlock), or raises an error that `retry_on` names, `run` waits a
   short random time and calls `work` again, up to `attempts` times.
4. What a failed run wrote went with its transaction. Only a run that
   commits publishes its events.

Keep these rules in mind:

- **Never run outside work again.** Work that calls outside its
  database, such as a card processor, must not run twice. Queue it as a
  task instead ([messaging](../contrib/messaging.md)), or see
  [Work that commits once per message](exactly-once.md).
- **A route can do the same.** A command route runs its service again
  the same way, with the reach check and `If-Match` made again in each
  run: `command_route(..., retry=Retry())`
  ([resources.md](../resources.md#commands)).
- **Test on PostgreSQL too,** where races are real: point the test
  settings' `DATABASES` at a server ([testing.md](../testing.md)).

A new aggregate can start with all of this written. `startaggregate
wallets Purse --field balance:money` makes the balance, a transfer that
locks both rows in id order and runs again when it loses a race
(`TransferPurseBalance`, `POST /purses/{purse_id}/transfer-balance`), and
its tests ([resources.md](../resources.md#generators)).

## See also

- [Databases](../databases.md#running-work-again): `Retry`, isolation,
  and row locks.
- [Money](../money.md): amounts and currencies.
- [Problem codes](../reference/problems.md#codes-of-your-own): how
  `InsufficientFunds` answers with the code `insufficient_funds`.

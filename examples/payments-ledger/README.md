# Payments ledger: wallets in double-entry books

Customers keep money in wallets and pay merchants from them. Every
payment is an entry in double-entry books, sent under an
`Idempotency-Key`.

## The problem

A customer pays 30.00 from a wallet that holds 100.00. The phone loses
its signal before the answer comes, so the app sends the payment again.
At the same moment the customer's tablet pays 80.00 from the same wallet.

Kept as a `balance` column that each request reads, checks, and writes
back, the wallet goes wrong three ways:

- the retry runs again, and the customer pays twice;
- both payments read 100.00, both pass the check, and the wallet is
  overdrawn; or one write replaces the other, and money is lost;
- nothing records where the money went, so no report can show that the
  books add up.

This example keeps each wallet as an account of
[`domainkit.contrib.accounting`](../../docs/contrib/accounting.md). A
payment is one entry: its debits equal its credits, and it is booked once
per wallet and `Idempotency-Key`. A retried request books once, two
payments at once never overdraw the wallet, and the trial balance shows
that the books net to zero.

## What is here

`domainkit init ledgerAPI` made the project, and `manage.py startdomain
wallets` its domain. These files were then changed:

| File | What changed |
| --- | --- |
| `src/config/settings/base.py` | The idempotency and accounting apps are installed, and `IdempotencyMiddleware` is last. |
| `src/config/settings/test.py` | The tests run on PostgreSQL when `LEDGER_API_TEST_DATABASE_URL` names one. |
| `src/config/urls.py` | The wallets at `/api/v1`, and the accounting app's API at `/api/accounting`. |
| `src/domains/wallets/controllers.py` | The books: wallets, deposits, payments, and holds. |
| `src/domains/wallets/routes.py` | The HTTP API, and the `Idempotency-Key` it requires. |
| `src/domains/wallets/apps.py` | `manage.py seed wallets` opens the books; the worker keeps the next month open. |
| `tests/` | The tests, and the requests they send (`tests/money.py`). |
| `pyproject.toml`, `.github/workflows/ci.yml` | mypy, strict, as `init --profile fintech` adds it. |

The domain has no table of its own. The books hold every wallet, every
entry, and every hold.

## Run it

From a checkout of domainkit:

```bash
cd examples/payments-ledger
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e "../..[postgres,redis,sqlite,testing]"
pip install --group dev
export LEDGER_API_SETTINGS_MODULE=config.settings.local
python manage.py makemigrations -m "books"
python manage.py migrate
pytest
```

The example holds no migrations: `makemigrations` writes them for the
domainkit you installed. In a project of your own, made from this one,
commit the migrations it writes. `init` writes a `.env` that names the
settings module, and `.env` is never committed, so the `export` line does
its job. To try the API, run `python manage.py seed wallets`, then
`createsuperuser`, `runserver`, and `runworker`, and open
`http://127.0.0.1:8000/docs`.

## The books

The business keeps two accounts of its own, beside the wallets. In
`src/domains/wallets/controllers.py`:

```python
CURRENCY: Final = "USD"
BANK: Final = "1000"
"""The business's bank account, where the customers' money is: an asset."""
MERCHANTS: Final = "2200"
"""What the business owes merchants until it pays them out: a liability."""
WALLETS: Final = "2100"
"""Each wallet's code starts so, then a random number that names nobody."""
```

A deposit raises both the bank account and the wallet. A payment moves
money from the wallet to what the business owes the merchants. A wallet
is a liability: money the business holds for its customer. It may never
go below zero, which is the ledger's default.

```python
async def open_wallet(holder: str) -> Account:
    """Open an empty wallet for ``holder``.

    Its code is random: erasing the holder renames the holder, never the
    code, so a code must name nobody.
    """
    wallet = OpenAccount(
        code=f"{WALLETS}-{uuid4().hex[:16]}",
        name="Wallet",
        type=AccountType.LIABILITY,
        currency=CURRENCY,
        holder=holder,
    )
    return await open_account(wallet, AccountingUnitOfWork())
```

An entry must be dated in an open accounting period, and the trial
balance is a period's report. `open_books` opens the current month's
period and the next month's, and the two accounts of the business, each
once. `manage.py seed wallets` runs it before the first payment
(`apps.py`), and the worker runs it every day (`@periodic`), so the next
month is open before it starts: a payment at midnight on the 1st finds its
period. The tests' `books` fixture runs it too.

## A payment is an entry, once per key

In `src/domains/wallets/controllers.py`:

```python
async def pay(wallet: Account, amount: Money, key: UUID, description: str) -> Entry:
    """Pay ``amount`` from the wallet to the merchants, once per key.

    The reference is the wallet and the request's ``Idempotency-Key``. The
    ledger books one entry per reference: sent again on the day it was
    booked, the payment returns that entry. A week later, once the
    middleware has forgotten the key, the date differs, and the ledger
    refuses the reference (``reference_reused``); the entry booked under it
    is returned if it moved the same amount between the same accounts, as
    a retry of the same payment does. Anything else under the key is
    refused (409).
    """
    merchants = await house_account(MERCHANTS)
    payment = PostEntry(
        source="wallets.payment",
        reference=f"{wallet.id}:{key}",
        booked_on=clock.today(),
        description=description,
        lines=(Line.debit(wallet.id, amount), Line.credit(merchants, amount)),
    )
    try:
        return await post_entry(payment, AccountingUnitOfWork())
    except ReferenceReused:
        async with AccountingUnitOfWork() as uow:
            booked = await uow.entries.by_reference(payment.source, payment.reference)
        if booked is None or moves(booked) != moves(payment):
            raise
        return booked
```

Two things stop a payment from running twice:

1. **The middleware.** `IdempotencyMiddleware` stores the answer of the
   first request under its key. A retry with the same key and body gets
   that answer again, with `Idempotent-Replayed: true`, and the route does
   not run. The key is kept for 24 hours, and its tombstone for 7 days
   more ([idempotency](../../docs/contrib/idempotency.md)).
2. **The books.** The ledger books one entry per source and reference.
   Posted again the same day, as a message delivered twice is, it returns
   the first entry. Once the middleware has forgotten the key, a week
   later, the retry is booked on another date, so the ledger refuses the
   reference (`409 reference_reused`). `pay` then reads the entry booked
   under it: if it moved the same amount between the same accounts, the
   retry was the same payment, and `pay` returns that entry. Another
   payment under the key is refused.

The route makes the key required where money moves. In
`src/domains/wallets/routes.py`:

```python
async def idempotency_key(
    key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> UUID:
    """The request's ``Idempotency-Key``, a UUID; without one, it is refused.

    ``IdempotencyMiddleware`` replays a retry's answer; this makes the key
    required where money moves, so no client can leave it out.
    """
    if key is None:
        raise IdempotencyKeyRequired(
            "Send an Idempotency-Key with requests that move money."
        )
    return key
```

What each request gets:

| Request | Answer |
| --- | --- |
| A new key | `201`, the payment's `entry_id` |
| The same key and body again | `201`, the same body, `Idempotent-Replayed: true` |
| The same key, another body | `422 idempotency_key_reused` |
| The same key, while the first still runs | `409 idempotency_key_in_use`, `Retry-After: 1` |
| No key | `400 idempotency_key_required` |
| More than the wallet has available | `422 insufficient_funds` |

## Two payments at once

The ledger locks the accounts an entry moves, by ascending id, and moves
their balances in the same transaction. Two payments from one wallet
wait for each other, in one order, so they neither deadlock nor lose an
update, and a payment is refused if those before it left too little.
SQLite runs postings one at a time. The test sends five payments of 30.00
together, from 100.00, on both databases. In
`tests/domains/wallets/test_payments.py`:

```python
answers = await asyncio.gather(
    *(
        customer.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=key())
        for _ in range(5)
    )
)
```

Three are answered `201` and two `422 insufficient_funds`, and the wallet
keeps 10.00. Five, not two, so that the payments overlap: with two, the
second often starts after the first has committed, and a ledger that did
not lock would pass too.

## Holds: authorize, then capture or release

A card authorization reserves an amount. The balance stays the same, but
the amount is no longer available to spend. Later the business captures
what it charges and gives back the rest, or releases it all. In
`src/domains/wallets/controllers.py`:

```python
async def authorize(wallet: Account, amount: Money, key: UUID) -> Hold:
    """Hold ``amount`` on the wallet: its balance stays, its available drops."""
    hold = PlaceHold(
        account_id=wallet.id,
        amount=amount,
        source="wallets.hold",
        reference=f"{wallet.id}:{key}",
    )
    return await place_hold(hold, AccountingUnitOfWork())
```

The customer places the hold (`POST /api/v1/wallets/{id}/holds`). A clerk
captures it (`POST /api/v1/holds/{id}/capture`, with
`accounting.capture_hold`) or releases it (`.../release`, with
`accounting.release_hold`). A capture books an entry from the wallet to
the merchants' account, and its route names that account itself. A hold
that nobody settles expires after 7 days (`ACCOUNTING["HOLD_TTL"]`): the
worker releases it.

## Refunds and the trial balance

Entries never change. A refund reverses the payment's entry, through the
accounting app's own API, which a clerk with `accounting.reverse_entry`
calls:

```bash
curl -X POST http://127.0.0.1:8000/api/accounting/entries/$ENTRY/reverse \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'
```

Reversing it again returns the same reversal. After a deposit of 100.00,
a payment of 30.00 and its refund, and a hold of 50.00 of which 20.00 was
captured, the trial balance of the period
(`GET /api/accounting/periods/{id}/trial-balance`, with `Accept:
text/csv`) reads:

```text
code,name,type,currency,opening,debits,credits,closing
1000,Bank,asset,USD,0.00,100.00,0.00,100.00
2100-6f1c9c1e8a2b4c3d,Wallet,liability,USD,0.00,50.00,130.00,-80.00
2200,Merchants payable,liability,USD,0.00,30.00,50.00,-20.00
,total,,USD,0.00,180.00,180.00,0.00
```

Credit balances show as negative, so each currency's total is zero. The
wallet holds 80.00, the merchants are owed 20.00, and the bank holds the
100.00. `python manage.py accounting verify` checks that every entry
balances and every balance is the sum of its postings.

## What the tests prove

| Test | What it shows |
| --- | --- |
| `test_a_retried_payment_books_once` | The same request twice: one entry, the answer replayed. |
| `test_the_books_book_a_key_once_after_its_answer_is_gone` | Eight days on, past the middleware, the same payment returns its first entry; another under the key is refused. |
| `test_money_moves_only_with_a_key` | A payment without a key is refused, and moves nothing. |
| `test_payments_at_once_never_overdraw` | Of five payments sent together, the two that would overdraw are refused. |
| `test_a_wallet_is_its_holders_alone` | Another customer's wallet is not found. |
| `test_a_capture_pays_part_of_a_hold_and_frees_the_rest`, `test_a_release_moves_nothing`, `test_what_is_held_cannot_be_spent` | Holds reserve, capture, and release. |
| `test_the_trial_balance_balances` | Every currency nets to zero, and `verify_ledger()` finds no problem. |
| `test_payments_go_on_into_the_next_month` | A payment on the 1st finds its period, opened the month before. |

domainkit's CI runs them on SQLite and on PostgreSQL, with the project's
ruff, mypy, `check`, and migrations, for every change to the framework.

## Read next

- [Accounting](../../docs/contrib/accounting.md): periods, reports, and
  reconciling a bank's statements.
- [Limits](../../docs/contrib/limits.md): how much a wallet may move in a
  day, counted with each entry.
- [Maker-checker](../maker-checker/README.md): a second person for large
  payouts.

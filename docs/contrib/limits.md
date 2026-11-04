# Limits: transaction and velocity limits on money

A limit bounds how much money may move. It can bound one movement (at
most 10,000 EGP per payment), or the sum and the number of movements in
a window (at most 50,000 EGP and 20 payments a day). The limits app
counts each movement in the [use case](../learning.md#words)'s own unit
of work. So the count commits with the movement or rolls back with it,
and two movements at the same moment never both pass a limit that only
one of them fits.

On this page you learn how to:

- set a limit per subject (an account, a customer, an actor), per
  currency, and per direction;
- count a movement in your use case, and see what is left;
- give a movement back after a refund;
- give default limits in the settings;
- let the ledger count every entry it books;
- manage limits over HTTP and from the command line.

The decision, with the proof of concept behind it, is
[ADR 0123](../adr/0123-count-movements-against-limits-under-the-subjects-row-in-the-movements-transaction.md).
Amounts are `domainkit.money.Money` ([money.md](../money.md)).

## Install

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # optional: purges old usage on a schedule
    "domainkit.contrib.accounting",  # optional: its postings are counted
    "domainkit.contrib.limits",
]
```

```bash
python manage.py makemigrations -m "limits"
python manage.py migrate
```

The app brings four tables: `domainkit_limits_limits`, `_subjects`,
`_usage`, and `_kept` (the ledger's reversals that are counted as
movements). They live in the database that `DATABASE_ROUTING['limits']`
names, or `default`. With the ledger installed, that must be the
ledger's database, because a posting is counted in its own transaction.
`setup()` refuses any other (`limits.E001`).

## A first example

Bound each customer's payments out of their wallet in Egyptian pounds,
then count five payments of 9,000:

```python
from domainkit.contrib.limits import (
    HOLDER,
    LimitsUnitOfWork,
    Movement,
    SetLimit,
    Window,
    consume,
    set_limit,
)
from domainkit.money import Money

await set_limit(
    SetLimit(
        code="wallet.daily",
        kind="holder",
        currency="EGP",
        window=Window.DAY,
        max_amount=Money.of("50000", "EGP"),
        max_count=20,
        per_transaction=Money.of("10000", "EGP"),
    ),
    LimitsUnitOfWork(),
)

for number in range(6):
    async with LimitsUnitOfWork() as uow:
        await consume(
            uow,
            Movement(Money.of("9000", "EGP"), {HOLDER: "customer-42"}),
            key=f"payments:{number}",
        )
        await uow.commit()
```

The first five pass and use 45,000. The sixth would make 54,000, more
than 50,000 a day, so `consume` raises `LimitExceeded`:

```text
domainkit.contrib.limits.exceptions.LimitExceeded: The amount is more than a limit allows in its period.
```

The error's `limit` is `"wallet.daily"` and its `window` is `"day"`. A
client gets 422 with the code `limit_exceeded`.

## Limits

A `Limit` is an aggregate. These fields make its key; at most one limit
has each key, and the key never changes:

| Field | What it is |
| --- | --- |
| `code` | The limit's name: up to 64 lower-case letters, digits, `_`, `.`, `:`, or `-`. |
| `kind` | The kind of subject: `account` (`ACCOUNT`, a ledger account by its id), `holder` (`HOLDER`, the holder an account is kept for), `actor` (`ACTOR`, the request's actor), or a kind of your own, such as `merchant` or `card`: up to 32 lower-case letters, digits, or `_`, starting with a letter. |
| `direction` | `Direction.OUT` (the default): money leaving, what lowers an account's balance. `Direction.IN`: money coming in, such as top-ups. |
| `currency` | One currency with at most 4 decimal places. A movement in another currency is not bounded by this limit. Set one limit per currency. |
| `subject` | `""` (the default) for every subject of the kind, or one subject (1 to 255 printable characters). |

A limit for one subject replaces the limit for every subject, so you can
raise or lower one customer's limit. A subject is an id, as an account's
holder is: never a name or an email address.

A limit has at least one bound:

| Bound | What it allows |
| --- | --- |
| `per_transaction` | The most that one movement may be. |
| `max_amount` | The most that may move in the `window`. |
| `max_count` | The most movements in the `window`; 1 or more. |

Amounts are in the limit's currency and more than zero. The `window`
(`Window`) is one of:

| Window | What it covers |
| --- | --- |
| `Window.DAY` | The calendar day, from midnight in the limit's `time_zone`. |
| `Window.MONTH` | The calendar month, from midnight of its first day. |
| `Window.ROLLING_DAY` | The last 24 hours. |
| `Window.ROLLING_MONTH` | The last 30 days. |

`time_zone` is `TIME_ZONE` when the limit is made, unless you give one.
On PostgreSQL, windows are judged by the database's clock, the same for
every host (ADR 0043).

`enabled=False` bounds nothing. A disabled limit for one subject exempts
that subject: the limit for every subject does not apply to it either.
`note` is free text, at most 255 characters.

### Making and changing limits

Four functions change limits. Each takes a command and a
`LimitsUnitOfWork`:

| Function | Command | What it does |
| --- | --- | --- |
| `set_limit` | `SetLimit` | Makes the limit of a key, or sets the bounds of the one there is. |
| `define_limit` | `SetLimit` | Makes a limit; a key already used raises `LimitTaken` (409, `limit_taken`). |
| `change_limit` | `ChangeLimit(limit_id=...)` | Sets all of a limit's bounds: a bound you leave out is no bound. `time_zone=None` keeps the limit's own. |
| `remove_limit` | `RemoveLimit(limit_id=...)` | Removes it; a default of the settings with its key applies again. |

```python
from domainkit.contrib.limits import ChangeLimit, RemoveLimit, change_limit, remove_limit

limit = await set_limit(
    SetLimit(code="wallet.daily", kind="holder", currency="EGP",
             subject="customer-42", window=Window.DAY,
             max_amount=Money.of("100000", "EGP")),
    LimitsUnitOfWork(),
)
await change_limit(
    ChangeLimit(limit_id=limit.id, window=Window.DAY,
                max_amount=Money.of("80000", "EGP")),
    LimitsUnitOfWork(),
)
await remove_limit(RemoveLimit(limit_id=limit.id), LimitsUnitOfWork())
```

Each change records `LimitSet` or `LimitRemoved`. The [audit
trail](audit.md) writes them with the actor who made the change and every
bound. A limit that cannot be as written raises `InvalidLimit` (422,
`invalid_limit`):

```text
InvalidLimit: A limit's code has up to 64 lower-case letters, digits, '_', '.', ':', or '-'.
InvalidLimit: A kind of subject has up to 32 lower-case letters, digits, or '_', starting with a letter.
InvalidLimit: A limit sets an amount per transaction, or a maximum over a window.
```

An unknown id raises `LimitNotFound` (404, `limit_not_found`). A limit's
id is a `LimitId`, a `NewType` of `UUID`.

### Defaults in the settings

You can also give limits in the settings, as `LimitDefault` entries:

```python
LIMITS = {
    "DEFAULTS": [
        {
            "code": "wallet.daily",
            "kind": "holder",
            "currency": "EGP",
            "window": "day",
            "max_amount": "50000",
        },
    ],
}
```

A default applies unless a stored limit has its key. A disabled stored
limit turns a default off. Amounts are text (`"50000"`, `"10.50"`),
never floats. `setup()` checks each default as it checks a stored limit.
It refuses a default that is not a limit, or two defaults with one key
(`limits.E002`).

`"currency": "*"` makes a default for every currency that no other limit
of its key names. Its amounts are read in each currency's units.

## Counting movements

A use case counts what it moves in its own unit of work, on the limits'
database. It locks its own rows first, and counts before its commit:

```python
from domainkit.contrib.limits import Movement, consume

async with PaymentsUnitOfWork() as uow:
    wallet = await uow.wallets.get_for_update(wallet_id)
    wallet.pay(amount)
    await consume(
        uow,
        Movement(amount, {"holder": str(wallet.owner_id), "merchant": merchant_id}),
        key=f"payments:{payment_id}",
    )
    await uow.commit()
```

A `Movement` is an amount (more than zero), the subjects it counts
against (`{kind: subject}`), and its `direction` (`Direction.OUT` by
default). Each subject is checked against its kind's limits in the
amount's currency and direction. `consume` takes several movements:
they are judged together, and each counts once toward `max_count`.

`consume` refuses a movement over a limit:

| Error | When | Retry? |
| --- | --- | --- |
| `TransactionLimitExceeded` | One movement is more than `per_transaction`. | No: it is refused again. |
| `LimitExceeded` | A window's amount or count cannot take the movement. | Yes, once the window moves on or the limit is raised. |
| `CurrencyNotLimited` | The subject is bounded in other currencies, and nothing names this one. | No. |

Each is a 422 with its code (`transaction_limit_exceeded`,
`limit_exceeded`, `currency_not_limited`). The error carries the
limit's `limit` (its code) and `window` for your code. A client gets the
code and a translated detail, as problem details where `ERROR_FORMAT =
"problem+json"`.

### What is kept, and what counts back

The subjects that a window bounds keep their movements, and only they.
A subject that no window bounds keeps nothing and takes no lock. So a
bank's clearing and fee accounts cost no row per posting (PERF-26).

With the ledger installed, a limit with a window that is set, changed,
or enabled partway through its window (an exemption lifted, a limit that
gains a window) counts what the ledger's postings moved since the window
began. It counts them for accounts and holders, in the transaction that
sets it: every entry not reversed, once, and a reversal booked under an
approved request together with the entry it reverses (SEC-16b-9).
Removing a limit counts back too, for the limit of its code that now
applies to its subjects. For example, after you remove an exemption, the
default's daily window sees the morning (SEC-16b-3). A limit for every
subject counts back in pieces of 1,000 subjects, whatever their number
(SEC-16b-8).

An actor's movements, and those of your own kinds counted with
`consume`, are not on record: they count from when a window bounds them.

!!! warning
    A default added to `LIMITS.DEFAULTS` by a deploy starts with an empty
    window: no transaction sets it, so nothing counts back. To make it
    see the window so far, add it as a stored limit (`set_limit`), and
    move it to the settings once a window has passed.

### Currencies fail closed

A subject that some limit bounds in one currency is bounded in all. A
movement in a currency that none of its limits names is refused with
`CurrencyNotLimited`, unless a default for every currency (`"currency":
"*"`) bounds it (FIN-89). A limit disabled for one subject (an exemption)
names its currency only.

```text
domainkit.contrib.limits.exceptions.CurrencyNotLimited: No limit names this currency for this subject: it moves none.
```

### Two movements at once

Before `consume` reads a subject's usage, it locks the subject's row,
made on its first movement. So two movements of one subject are judged
one after the other. Of 50 payments of 25.00 started at once against
1,000.00 a day, exactly 40 pass, from one wallet or from five (ADR 0123).
Subjects are locked in the order of their keys. Lock the use case's own
rows first. On SQLite, the count takes the database's write lock.

A window is a sum over its movements, under the subject's lock. A limit
on a subject that moves thousands of times in a window (a merchant's
incoming payments, a service's actor) sums them all for each movement,
and its movements wait for one another (PERF-27).

### Giving a movement back: `release`

The count is in the use case's transaction: one that rolls back counted
nothing. To give back a movement that was undone later (a refund, or a
payment the provider declined after the commit), call `release(uow,
key)` with the key it was counted under. It returns how many movements
it gave back:

```python
from domainkit.contrib.limits import release

async with LimitsUnitOfWork() as uow:
    print(await release(uow, "payments:0"))
    await uow.commit()
```

```text
1
```

The movement then counts in no window. Releasing a key again releases
nothing.

### Checking without counting: `check_movements`

`check_movements(uow, *movements)` refuses as `consume` would now, but
counts and locks nothing. It works in a read-only unit of work too. Use
it to show a customer what is left, or to refuse an authorization before
placing a hold:

```python
from domainkit.contrib.limits import check_movements

async with LimitsUnitOfWork() as uow:
    await check_movements(uow, Movement(Money.of("9000", "EGP"), {HOLDER: "customer-42"}))
```

A movement that it lets through may still be refused by `consume`, if
another movement was counted in the meantime.

### What a subject used: `usage_of`

`usage_of(kind, subject, currency=None, direction=None)` returns a
`Usage` for each limit that bounds the subject:

```python
from domainkit.contrib.limits import usage_of

for usage in await usage_of("holder", "customer-42"):
    print(usage.code, usage.window, usage.since, usage.used, usage.count, usage.remaining)
```

```text
wallet.daily day 2026-11-03 22:00:00+00:00 45000.00 EGP 5 5000.00 EGP
wallet.out None None 0.00 EGP 0 None
```

A `Usage` holds the limit's `code`, `kind`, `direction`, `subject`,
`window`, and bounds. `since` is when the window started (in UTC), `used`
and `count` are what moved in it, and `remaining` is what `max_amount`
still allows. `stored` is false for a default of the settings. A limit
with no window has `since` `None`, and `used` and `count` zero.

## On the ledger

With `domainkit.contrib.accounting` installed, limits registers on its
posting path (`on_stage_entry`). Every entry that the ledger books
(posted, reversed, or a hold's capture) is counted in the posting's
transaction, after its accounts are locked. A refused entry is not
booked, and its balances do not move. Each entry is a movement:

- of each account that it posts to (kind `account`, the account's id),
  and of each account's holder (kind `holder`), for the sum of its
  postings to it: `out` where a posting lowers the account's balance (a
  wallet's debit), `in` where it raises it (a top-up);
- of the request's actor (kind `actor`), when there is one: the entry's
  debits, `out`.

The entry's key is `accounting.entry:<id>`.

**The ledger counts at the commit.** An entry's movements are worked out
as it is booked, and counted as its transaction commits, after the rest
of its work. On PostgreSQL this happens under a shared lock of each
ledger kind, which counting back takes alone. A limit set while a
posting is in flight either waits for that commit and counts the
posting, or is read by it (SEC-16b-2). A refusal still rolls the whole
posting back. An entry whose movement for one subject (the sum of its
postings for it) is more than usage keeps (`NUMERIC(18, 4)`) is refused
with `MovementOutOfRange` (422, `amount_out_of_range`, SEC-16b-5).

**Reversals.** A reversal counts nothing, and releases what the entry it
reverses counted. The ledger reverses no reversal
(`entry_not_reversible`), so the money of a reversed entry moves again
only as a new entry, which is counted. A reversal booked while an
approved request runs (with [approvals](approvals.md), one over
`ENTRY_THRESHOLDS` must be) is counted as a movement instead, and
releases nothing. So undoing a payout that two people approved does not
free the holder's limit to draw it again (FIN-88).

A hold moves nothing: it is counted when captured.

A holder or an actor that cannot be a subject (it has a control
character, or more than 255 characters) is counted under its digest:
`sha256:` and 64 hex digits of its UTF-8. Set its limits for that
subject. No entry is refused because of its holder's name.

## Over HTTP

```python
from domainkit.contrib.limits.api import router as limits

router.include_router(limits, prefix="/api/limits")
```

| Route | Permission | What it does |
| --- | --- | --- |
| `GET /limits`, `/limits/{id}` | `limits.view_limit` | The limits, filtered by `code`, `kind`, `direction`, `currency`, `subject`, or `enabled`. |
| `POST /limits` | `limits.add_limit` | Make a limit (409 `limit_taken` if its key is used). |
| `POST /limits/{id}/change` | `limits.change_limit` | Set its bounds; `If-Match` makes it conditional. |
| `DELETE /limits/{id}` | `limits.delete_limit` | Remove it. |
| `GET /usage?kind=holder&subject=...` | `limits.view_limit` | A subject's usage of each limit that bounds it (`currency=` and `direction=` keep to one). |

Every route needs its permission, whatever authenticates requests.
Amounts are `{"amount": "50000.00", "currency": "EGP"}`. Each usage item
also says where its limit comes from: `"origin": "stored"` or
`"settings"`. A breach answers 422 with its code wherever a counted
movement is refused: on the ledger's `POST /entries`, or on a route of
your own.

`domainkit.contrib.limits.api` also exports `limits`, the
`ResourceRouter` of the limits alone, without `/usage`.

## From the command line

| Action | What it does |
| --- | --- |
| `limits list [--kind K]` | The stored limits, then the defaults they leave. |
| `limits usage KIND SUBJECT [--currency C]` | A subject's use of each window. |
| `limits set CODE --kind K --currency C` | Make a limit, or change only the bounds you give: `--subject`, `--direction in\|out`, `--per-transaction`, `--window`, `--max-amount`, `--max-count` (each takes `none` to clear it), `--time-zone`, `--note`, `--enable`, `--disable`. |
| `limits purge` | Delete usage older than `KEEP_USAGE`. |

`set` and `purge` run as the operator and are audited; `purge` is
recorded as `operator.limits.purge`
([operators](../reference/commands.md#operators)).

```bash
python manage.py limits list
python manage.py limits usage holder customer-42
python manage.py limits set wallet.daily --kind holder --currency EGP \
  --subject customer-42 --window day --max-amount 100000
python manage.py limits purge
```

```text
wallet.daily: every holder, out EGP: at most 10000.00 EGP each, 50000.00 EGP a day, 20 a day [stored]
wallet.out: every holder, out EGP: at most 20000.00 EGP each [settings]
wallet.daily: 36000.00 EGP of 50000.00 EGP in 4 movement(s) of 20 since 2026-11-03T22:00:00+00:00 (out)
wallet.out: at most 20000.00 EGP each (out)
wallet.daily: holder 'customer-42', out EGP: 100000.00 EGP a day [stored]
Deleted the usage of 0 movement(s).
```

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

```python
from datetime import timedelta

LIMITS = {"KEEP_USAGE": timedelta(days=62)}
```

| `LIMITS` key | Default | Meaning |
| --- | --- | --- |
| `DEFAULTS` | `()` | Limits in the settings (above), each with a stored limit's fields and its amounts as text; `"currency": "*"` bounds every currency that no other limit of its key names, its amounts read in each currency's units. |
| `KEEP_USAGE` | 35 days | How long a movement's usage is kept: at least 32 days (longer than any window), at most 3,660 days. |
| `PURGE_EVERY` | 1 hour | How often the worker purges older usage, with messaging (1 minute to 1 day). |

Without messaging, nothing purges usage on a schedule, and `check` warns
where deployed (`limits.W001`). Run `manage.py limits purge` from your
scheduler (or call `purge_usage()`, which returns how many movements it
deleted), and list `limits.W001` in `SILENCED_CHECKS`.

## Checks

| Id | When | What to do |
| --- | --- | --- |
| `limits.E001` | The ledger is installed, and limits live in another database. | Route `limits` where `accounting` is, in `DATABASE_ROUTING`. |
| `limits.E002` | A default of `LIMITS.DEFAULTS` is not a limit, or two share a key. | Fix the default that the message names. |
| `limits.W001` | Deployed without messaging: nothing purges old usage. | Install messaging, or schedule `limits purge` and silence it. |

## On PostgreSQL and SQLite

On PostgreSQL, `consume` locks one row per subject, and windows follow
the database's clock. On SQLite, counting takes the database's write
lock, so movements run one at a time; windows follow the host's clock.
Amounts are `ExactDecimal(18, 4)`, exact on both. On PostgreSQL, the
usage key's index is a hash, a third of the size of a B-tree (PERF-30).

## Privacy

A holder's or an actor's id is a subject. `privacy export` lists the
limits set for a subject and its counted movements by kind. `privacy
erase` removes both (each limit's removal is audited), and the subject's
movements then count against no window.

## Errors and refusals

| Error | When | HTTP |
| --- | --- | --- |
| `LimitExceeded` | A window's amount or count cannot take the movement. | 422, `limit_exceeded` |
| `TransactionLimitExceeded` | One movement is over `per_transaction`. | 422, `transaction_limit_exceeded` |
| `CurrencyNotLimited` | No limit names the movement's currency for a bounded subject. | 422, `currency_not_limited` |
| `MovementOutOfRange` | A movement larger than `NUMERIC(18, 4)`. | 422, `amount_out_of_range` |
| `InvalidLimit` | A limit or a movement that cannot be as written. | 422, `invalid_limit` |
| `LimitTaken` | Another limit has the same key. | 409, `limit_taken` |
| `LimitNotFound` | No limit has that id. | 404, `limit_not_found` |

## Common mistakes

- **Counting in another unit of work.** The count must commit with the
  movement. Call `consume` inside the use case's own unit of work.
- **Forgetting `release` after a refund.** The refunded money still
  counts in the window. Release it by its key.
- **Writing a default's amount as a float.** Write `"50000"`, as text.
- **Expecting a new default to see today's payments.** A default added
  by a deploy starts with an empty window. Add it as a stored limit
  first.
- **Putting a name or an email address in a subject.** Use the id.

## See also

- [Accounting](accounting.md): the ledger whose entries are counted.
- [Approvals](approvals.md): a second person for large movements.
- [Money](../money.md): `Money` and currencies.
- [Audit](audit.md): the history of every limit change.

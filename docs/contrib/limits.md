# Limits: transaction and velocity limits on money

`domainkit.contrib.limits` bounds what money may move: the most one
movement may be, and the sum and number of movements in a window (a
calendar day or month, or the last 24 hours or 30 days), per subject (an
account, the holder it is kept for, the actor, or a key of the
project's), per currency, and per direction. A movement is counted in
the use case's own unit of work, so the count commits with it or rolls
back with it, and two movements at once never both pass a limit that
only one fits. With [the ledger](accounting.md) installed, every entry it
books is counted the same way. The decision, and the proof of concept
that made it, is [ADR 0123](../adr/0123-count-movements-against-limits-under-the-subjects-row-in-the-movements-transaction.md);
amounts are `domainkit.money.Money` ([money.md](../money.md)).

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # optional: old usage purged on a schedule
    "domainkit.contrib.accounting",  # optional: its postings are counted
    "domainkit.contrib.limits",
    ...,
]
```

Then `makemigrations` and `migrate`: the app brings three tables,
`domainkit_limits_limits`, `_subjects`, and `_usage`, in the database
`DATABASE_ROUTING['limits']` names, or `default`. With the ledger, that
is the ledger's database: a posting is counted in its own transaction,
so `setup()` refuses another (`limits.E001`).

## Limits

```python
from domainkit.contrib.limits import LimitsUnitOfWork, SetLimit, Window, set_limit
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
        time_zone="Africa/Cairo",
    ),
    LimitsUnitOfWork(),
)
```

- A limit's **key** is its `code` (up to 64 lower-case letters, digits,
  `_`, `.`, `:`, `-`), its `kind` of subject, its `direction`, its
  `currency`, and its `subject`. At most one limit has each key
  (`limit_taken`, 409), and it never changes.
- `kind` is `account` (a ledger account, by its id), `holder` (the
  `holder` an account is kept for), `actor` (the request's actor), or a
  kind of the project's own (`merchant`, `card`): up to 32 lower-case
  letters, digits, or `_`.
- `subject` is `""` (the default) for every subject of the kind, or one
  subject (1 to 255 printable characters), whose limit replaces the one
  for every subject: raise or lower one customer's. A subject is an id,
  as an account's holder is: never a name or an e-mail address.
- `direction` is `out` (the default: money leaving, what lowers an
  account's balance) or `in` (top-ups, what raises it).
- `currency` is one currency of at most four places: a movement in
  another is not bounded by it. Set one limit per currency.
- The **bounds**, one at least: `per_transaction`, the most one movement
  may be; and over `window` (`day`, `month`, `rolling_day`,
  `rolling_month`), `max_amount` and `max_count`. Amounts are in the
  limit's currency, more than zero; a count is one at least.
- A calendar `day` or `month` starts at midnight in the limit's
  `time_zone` (by default `TIME_ZONE` when the limit is made); a
  `rolling_day` is the last 24 hours, a `rolling_month` the last 30 days.
  Windows are judged by the database's clock on PostgreSQL, the same for
  every host (ADR 0043).
- `enabled=False` bounds nothing. A disabled limit for one subject exempts
  it: the limit for every subject does not apply to it either.

`set_limit` makes the limit of a key or sets the bounds of the one there
is; `define_limit` makes one and refuses a key that is taken;
`change_limit(ChangeLimit(limit_id=..., ...))` sets all of a limit's
bounds (one left out is no bound), and `remove_limit` removes it. Each
records `LimitSet` or `LimitRemoved`, which `contrib.audit` writes to the
trail with the actor who made the change and every bound.

### Defaults in the settings

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

A default applies unless a stored limit has its key; a disabled stored
limit switches a default off. Amounts are text (`"50000"`, `"10.50"`),
never floats. `setup()` checks each one as a stored limit is checked, and
refuses one that is not a limit, or two of one key (`limits.E002`).

## Counting movements

A use case counts what it moves in its own unit of work, on the limits'
database, after locking its own rows and before its commit:

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

- A `Movement` is an amount (more than zero), the subjects it counts
  against (`{kind: subject}`), and its `direction` (`out` by default).
- `consume` refuses a movement over a limit: `TransactionLimitExceeded`
  (422 `transaction_limit_exceeded`) for one more than `per_transaction`,
  which is refused again if retried; `LimitExceeded` (422
  `limit_exceeded`) for one that a window's amount or count cannot take,
  which may pass once the window moves on. Each names the limit's `code`
  and `window` on the error, for the code that caught it; a client gets
  the code and a translated detail, as problem details where
  `ERROR_FORMAT = "problem+json"`. Several movements in one call are
  judged together, and each counts once toward `max_count`.
- **Every movement is counted**, under each of its subjects, whether a
  window bounds the subject or not: a limit set partway through its
  window, a limit that gains a window, or an exemption lifted counts what
  moved earlier in the window. A subject no window bounds is counted
  without a lock, so its movements do not wait for each other.
- **Concurrency.** Before reading a subject's usage, `consume` locks the
  subject's row, made on its first movement, so two movements of one
  subject are judged one after the other: of fifty payments of 25.00
  started at once against 1,000.00 a day, exactly forty pass, from one
  wallet or from five (ADR 0123). Subjects are locked in the order of
  their keys; lock the use case's own rows first. On SQLite the count
  takes the database's write lock.
- **Releasing.** The count is in the use case's transaction: one that
  rolls back counted nothing. A movement undone later (a refund, a
  payment the provider declined after the commit) is given back with
  `release(uow, key)`, by the key it was counted under: it counts in no
  window any more. Releasing a key again releases nothing.
- `check(uow, *movements)` refuses as `consume` would now, without
  counting or locking anything, in a read-only unit of work too: to show
  a customer what is left, or to refuse an authorisation before placing
  a hold. A movement it let through may still be refused by `consume`.
- `usage_of(kind, subject)` lists, for each limit that bounds a subject,
  its window's start (`since`), what was `used`, the `count`, and what
  `remaining` amount the window takes.

## On the ledger

With `domainkit.contrib.accounting` installed, limits registers on its
posting path (`on_booking`), so every entry the ledger books (posted,
reversed, or a hold's capture) is counted in the posting's transaction,
after its accounts are locked; a refused entry is not booked, and its
balances do not move. Each entry is a movement:

- of each account it posts to (kind `account`, the account's id) and of
  each account's holder (kind `holder`), the sum of its postings for it:
  `out` where a posting lowers the account's balance (a wallet's debit),
  `in` where it raises it (a top-up);
- of the request's actor (kind `actor`), when there is one, the entry's
  debits, `out`.

A reversal counts nothing, and releases what the entry it reverses
counted; the ledger reverses no reversal (`reversal_not_reversible`), so
the money of a reversed entry moves again only as a new entry, counted.
A hold moves nothing: it is counted when captured. The entry's key is
`accounting.entry:<id>`.

A holder or an actor that a subject cannot be (a control character, more
than 255 characters) is counted under its digest, `sha256:` and 64 hex
digits of its UTF-8 (`limits.postings.ledger_subject`): set its limits
for that subject. No entry is refused for its holder's name.

## Over HTTP

```python
from domainkit.contrib.limits.api import router as limits

router.include_router(limits, prefix="/api/limits")
```

| Route | Permission | Does |
| --- | --- | --- |
| `GET /limits`, `/limits/{id}` | `limits.view_limit` | The limits, by `code`, `kind`, `direction`, `currency`, `subject`, or `enabled`. |
| `POST /limits` | `limits.add_limit` | Make a limit (`limit_taken`, 409, if its key is). |
| `POST /limits/{id}/change` | `limits.change_limit` | Set its bounds; `If-Match` makes it conditional. |
| `DELETE /limits/{id}` | `limits.delete_limit` | Remove it. |
| `GET /usage?kind=holder&subject=...` | `limits.view_limit` | A subject's usage of each limit that bounds it (`currency=`, `direction=` keep to one). |

Every route needs its permission, whatever authenticates requests.
Amounts are `{"amount": "50000.00", "currency": "EGP"}`. A breach answers
422 with its code wherever a counted movement is refused: on the ledger's
`POST /entries`, or a route of the project's.

## From the command line

`manage.py limits list` lists the stored limits, then the defaults they
leave (`--kind` keeps to one kind); `usage KIND SUBJECT` (`--currency`)
shows a subject's windows; `set CODE --kind K --currency C` makes a limit
or changes the bounds given (`--subject`, `--direction`,
`--per-transaction`, `--window`, `--max-amount`, `--max-count`, each
`none` to clear it, `--time-zone`, `--note`, `--enable`, `--disable`),
as the operator, audited; `purge` deletes usage older than `KEEP_USAGE`,
recorded as the operator's `operator.limits.purge`
([commands](../reference/commands.md#operators)).

## Settings

```python
LIMITS = {"KEEP_USAGE": timedelta(days=62)}
```

| `LIMITS` key | Default | Meaning |
| --- | --- | --- |
| `DEFAULTS` | `()` | Limits in the settings (above), each a stored limit's fields, its amounts text. |
| `KEEP_USAGE` | 35 days | How long a movement's usage is kept: at least 32 days (longer than any window), at most 3,660 days. |
| `PURGE_EVERY` | 1 hour | How often the worker purges older usage, with messaging (1 minute to 1 day). |

Without messaging nothing purges usage on a schedule: `check` warns where
deployed (`limits.W001`). Run `manage.py limits purge` from the
project's scheduler, and silence it.

## Privacy

A holder's or an actor's id is a subject. `privacy export` lists the
limits set for a subject and its counted movements by kind; `privacy
erase` removes both (each limit's removal is audited), and the subject's
movements count against no window any more.

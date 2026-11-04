# Accounting: double-entry books, holds, periods, and reconciliation

The accounting app keeps a service's books. It holds a chart of
accounts, journal entries whose postings always balance, the balances
that move with them, holds on what an account has available, accounting
periods that close, a trial balance and a general ledger per period, and
the reconciliation of a bank's statements with the postings. A wallet, a
payment service, or a lender needs this underneath. The hard parts are
done once: concurrent postings that neither deadlock nor lose an update,
a retried request or a message delivered twice that books once, and
entries that never change.

On this page you learn how to:

- open accounts and post balanced entries;
- reserve money with holds, then capture or release it;
- open and close periods;
- read the trial balance and the general ledger, and check the books;
- reconcile a bank statement with the postings;
- serve the books over HTTP, and run the `accounting` command.

The decisions are ADRs
[0116](../adr/0116-keep-double-entry-books-locked-in-id-order-and-append-only.md)
and [0117](../adr/0117-reconcile-statements-through-a-port-with-rules-in-order-and-record-breaks.md).
Amounts are `domainkit.money.Money` (ADR 0115, [money.md](../money.md)).

## Install

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # optional: entries from messages, hold expiry
    "domainkit.contrib.accounting",
]
```

```bash
python manage.py makemigrations -m "books"
python manage.py migrate
```

The app brings eight tables (`domainkit_accounting_accounts`,
`_periods`, `_entries`, `_postings`, `_holds`, `_statement_lines`,
`_matches`, `_breaks`), in the database that
`DATABASE_ROUTING['accounting']` names, or `default`. That is one
database: the ledger refuses a shard group, because an entry's accounts
must commit together. Across databases, use a clearing account and an
entry on each side, linked through the outbox.

## A first example

Open October, a cash account, and a customer's wallet, then book a
top-up of 150 EGP:

```python
from datetime import date

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    OpenAccount,
    OpenPeriod,
    PostEntry,
    open_account,
    open_period,
    post_entry,
    trial_balance,
)
from domainkit.money import Money

october = await open_period(
    OpenPeriod(name="2026-10", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31)),
    AccountingUnitOfWork(),
)
cash = await open_account(
    OpenAccount(code="1000", name="Cash", type=AccountType.ASSET, currency="EGP"),
    AccountingUnitOfWork(),
)
wallet = await open_account(
    OpenAccount(code="2100-000042", name="Wallet", type=AccountType.LIABILITY,
                currency="EGP", holder="user-7"),
    AccountingUnitOfWork(),
)
amount = Money.of("150.00", "EGP")
await post_entry(
    PostEntry(
        source="payments",
        reference="pay-1",
        booked_on=date(2026, 10, 30),
        description="Card top-up",
        lines=(Line.debit(cash.id, amount), Line.credit(wallet.id, amount)),
    ),
    AccountingUnitOfWork(),
)
print((await trial_balance(october.id)).to_csv())
```

```text
code,name,type,currency,opening,debits,credits,closing
1000,Cash,asset,EGP,0.00,150.00,0.00,150.00
2100-000042,Wallet,liability,EGP,0.00,0.00,150.00,-150.00
,total,,EGP,0.00,150.00,150.00,0.00
```

The cash account rose by a debit; the wallet, a liability, rose by a
credit. Credit balances show as negative in the report, so each
currency's total is zero.

## Accounts

An `Account` is an [aggregate](../learning.md#words) (its id is an
`AccountId`). Open one with `open_account(OpenAccount(...), uow)`:

```python
wallet = await open_account(
    OpenAccount(
        code=f"2100-{wallet_number:06d}",  # Your own number, not the user's.
        name="Wallet",
        type=AccountType.LIABILITY,
        currency="EGP",
        holder=str(user.id),
        may_go_negative=False,
    ),
    AccountingUnitOfWork(),
)
```

- A **code** has up to 32 letters, digits, `.`, `_`, or `-`, and is
  unique (`account_code_taken`, 409). A **name** has up to 200
  characters.
- The **type** (`AccountType`: `ASSET`, `LIABILITY`, `EQUITY`, `INCOME`,
  `EXPENSE`) decides the normal balance. Assets and expenses rise with
  debits; liabilities, equity, and income with credits. An account's
  `balance` is in that sign: a wallet's credits less its debits.
- One **currency** per account, of at most 4 decimal places. A line in
  another currency is refused. An unknown currency, or an amount with more
  places than its currency, is refused as `domainkit.money` refuses it
  (`unknown_currency`, `inexact_amount`, 422).
- `held` is what its active holds reserve, and `available` the balance
  less that. An account refuses a posting or a hold that would leave less
  than nothing available (`insufficient_funds`), unless it is opened with
  `may_go_negative=True`, as a clearing, settlement, or external account
  is. No account is overdrawn by default (FIN-97).
- A line, a balance, or what an account holds has at most 14 digits
  before the point (the columns are `NUMERIC(18, 4)`). Past that it is
  refused: `amount_out_of_range` (422).
- An account is `open` or `closed` (`AccountStatus`).
- `close_account(CloseAccount(account_id=...))` closes an account with
  nothing on it (`account_not_empty` otherwise). A closed one takes no
  postings or holds (`account_closed`).

### The holder and erasure

`holder` names the person that the account is kept for. `privacy export`
lists their accounts and postings. `privacy erase` keeps the books (the
law asks a business to keep them for years, and they are append-only)
but pseudonymizes them: their accounts name one `erased:` holder, the
same for all of them, so the books still show which accounts were one
person's (FIN-94).

While the person holds an open account with money on it or held, the
erasure is held (`erasure_held`): a business does not erase someone it
still owes, or who owes it. Close the accounts first. The erasure checks
again as it renames the accounts, which locks them. So money that landed
after the first check holds it then, before anything else is erased. It
also closes the person's open accounts, empty by then, so none takes
money for an erased holder (SEC-16b-4).

**Settling a held erasure.** Anyone who can credit a wallet or place a
hold on it can keep its holder's erasure held. Settle first:

1. release the holds (`release_hold`);
2. pay what is owed out to the person, or to a suspense account that the
   business keeps for unclaimed money (a liability of its own,
   `may_go_negative=False`, with no holder), with an entry the business
   books;
3. close each account (`close_account`), and erase.

Closed accounts take no more money, so the erasure then goes through.
The law's deadline for answering the request (a month under the GDPR)
is the business's to keep. `privacy.erase` recorded as denied, with its
reason, is the evidence of when it was asked.

!!! warning
    `holder` is the only field that erasure changes. Codes, names,
    descriptions, memos, references, and statement lines are kept as
    written, and every report and the audit trail's `AccountOpened` show
    the code. Keep personal data out of all of them: use an opaque code
    (`2100-000042`, from your own sequence), never the person's id or
    name.

## Entries

An `Entry` (its id is an `EntryId`) holds 2 to `MAX_LINES` `Posting`s.
You post one with `post_entry(PostEntry(...), uow)`; each `Line` is
`Line.debit(account_id, amount)` or `Line.credit(account_id, amount)`,
with an optional `memo`:

```python
from domainkit.contrib.accounting import Line, PostEntry, post_entry
from domainkit.money import Money

amount = Money.of("150.00", "EGP")
entry = await post_entry(
    PostEntry(
        source="payments",
        reference=str(payment.id),
        booked_on=today,
        description="Card top-up",
        lines=(Line.debit(cash.id, amount), Line.credit(wallet.id, amount)),
    ),
    AccountingUnitOfWork(),
)
```

- Each line is more than zero, on one side (`Side.DEBIT` or
  `Side.CREDIT`) of one account. Each currency's debits must equal its
  credits, or the entry is refused (`unbalanced_entry`, 422). An entry
  may mix currencies, through accounts of each.
- **Once per source and reference.** `source` (up to 64 lower-case
  letters, digits, `_`, `.`, `:`, `-`) and `reference` (up to 128
  printable characters) name the entry where it came from. Posted again
  under them, the same entry is returned and nothing more is booked: a
  retried request, a message delivered twice. A different entry under
  them is refused (`reference_reused`, 409). The sources that start with
  `accounting.` are the ledger's own (reversals post under
  `REVERSAL_SOURCE`, `accounting.reversal`, and captures under
  `CAPTURE_SOURCE`, `accounting.hold`): `post_entry` refuses them
  (`invalid_entry`, 422).
- **Concurrency.** A posting locks the name of its source and reference,
  then its accounts by ascending id. So two entries that share accounts
  wait for each other in one order and never deadlock. Their balances
  move in the same transaction, so no update is lost. A transaction that
  loses a race anyway runs again, whole, up to `MAX_ATTEMPTS` times
  ([databases.md](../databases.md#running-work-again)). On SQLite,
  postings run one at a time.
- `PostEntry` keeps its `lines` as a tuple, however they are given (a
  generator, a list), so every hook and the ledger read the same lines.

Each refusal says why:

```text
UnbalancedEntry: The entry's debits and credits differ in a currency.
InsufficientFunds: The account does not have enough available.
ReferenceReused: Another entry was posted under this source and reference.
NoOpenPeriod: No open accounting period covers the entry's date.
```

### Reversals

Entries never change. `reverse_entry(ReverseEntry(entry_id=...))` posts
the entry that undoes another, each posting on the other side, dated
today unless `booked_on` says otherwise. An entry is reversed once: the
reversal's source is `accounting.reversal` and its reference the entry's
id, and its `reverses` names the entry. Reversing it again returns that
reversal.

A reversal is not reversed (`entry_not_reversible`, 422). That would
book the entry it undid again, past the checks that a new entry meets
(limits, approvals). Post the entry again instead, under a reference of
its own.

```text
EntryNotReversible: A reversal is not reversed: post the entry again instead.
```

On PostgreSQL, triggers that `migrate` makes afresh each time refuse
every update, delete, and truncation of entries and postings, the table
owner's too. In staging and production the application refuses to start
without them as `migrate` makes them: enabled, firing for every row (no
`WHEN`, no column list), running their own function with its body
(`accounting.E001`). `flush`, `dumpdata`, and `loaddata` leave the
ledger's tables alone.

### Hooks: checks before and while booking

Other apps can refuse entries through two hook points of
`domainkit.contrib.accounting.hooks`:

- **Before posting.** An app registers `async def hook(command)` on
  `on_post_entry` in `ready()`, and raises to refuse. It runs before
  `post_entry` takes any lock. [Approvals](approvals.md#large-postings)
  refuses there the entries over `APPROVALS.ENTRY_THRESHOLDS` that no
  approved request posts (`approval_required`, 403).
- **As entries are booked.** A function registered with
  `on_stage_entry(hook)` is awaited as `hook(uow, entry, accounts)` in the
  posting's transaction, after its accounts and period are locked and
  their balances moved, for entries posted, reversals, and captures.
  Raising refuses the entry. Rows that it locks come after the accounts,
  in an order of its own. [Limits](limits.md#on-the-ledger) count each
  entry this way, and approvals refuses there the postings and captures
  over its thresholds that no approved request books.

```python
from domainkit.apps import DomainConfig
from domainkit.contrib.accounting.hooks import on_stage_entry


class RiskConfig(DomainConfig):
    label = "risk"

    def ready(self) -> None:
        on_stage_entry(check_entry)


async def check_entry(uow, entry, accounts) -> None:
    ...  # raise a DomainError to refuse the entry
```

Every event of the app (`AccountOpened`, `AccountClosed`, `EntryPosted`,
`HoldPlaced`, `HoldFinished`, `PeriodOpened`, `PeriodClosed`,
`StatementImported`, `AccountReconciled`) is recorded by
[audit](audit.md) when it is installed.

### From messages

A use case that should book an entry once its own change commits defers
it, through the outbox:

```python
from domainkit.contrib.accounting import PostEntry, defer_entry

async with orders_uow as uow:
    order.mark_paid()
    defer_entry(uow, PostEntry(source="orders", reference=str(order.id), booked_on=today, lines=lines))
    await uow.commit()
```

The worker posts it with the task `domainkit.accounting.post_entry`,
under the inbox (ADR 0094). A message run again after its entry
committed is skipped, and a second message about the same entry returns
the first one's. A refused entry (a closed period, for example) is
dead-lettered at once; `messages retry` runs it again once the books
allow it. A posting that lost its race `MAX_ATTEMPTS` times
(`ConcurrentUpdate`) is not refused: the worker runs it again after its
backoff, and the source and reference book it once.

## Holds

A hold reserves an amount on an account until it is captured, released,
or expires. It moves no balance: it lowers `available`.

```python
from domainkit.contrib.accounting import (
    CaptureHold,
    PlaceHold,
    capture_hold,
    place_hold,
)

hold = await place_hold(
    PlaceHold(account_id=wallet.id, amount=Money.of("100", "EGP"),
              source="cards", reference="auth-9"),
    AccountingUnitOfWork(),
)
# wallet: balance 150.00, held 100.00, available 50.00
hold = await capture_hold(
    CaptureHold(hold_id=hold.id, counter_account_id=merchants.id,
                amount=Money.of("80", "EGP")),
    AccountingUnitOfWork(),
)
print(hold.status)
```

```text
captured
```

- A `Hold` (its id is a `HoldId`) is `active`, `captured`, `released`,
  or `expired` (`HoldStatus`). It is placed once per source and
  reference, as an entry is.
- It lasts until `expires_at`, or `HOLD_TTL` from now, on the database's
  clock as its expiry is. `expires_at` is a time with its zone, in the
  future and at most 366 days away (`invalid_entry` otherwise; over HTTP,
  a time without a zone is a validation error, 422).
- A capture posts an entry out of the account (the side that lowers its
  balance) and into `counter_account_id`, of the whole hold or less, and
  releases the rest. Capturing again returns the captured hold.
- `release_hold(ReleaseHold(hold_id=...))` gives the hold back.
- An expired hold cannot be captured (`hold_not_active`, 409). On
  PostgreSQL, the database's clock says whether it has expired, so hosts
  whose clocks disagree agree on it (FIN-99).

With messaging, the worker releases expired holds every
`EXPIRE_HOLDS_EVERY`, and `manage.py accounting expireholds` does the
same at once (`expire_holds()` from code). Until then, an expired hold
still counts against `available`. Without messaging, `check` warns in
staging and production (`accounting.W003`): run `expireholds` from your
scheduler, and silence the warning.

## Periods

```python
from domainkit.contrib.accounting import (
    ClosePeriod,
    OpenPeriod,
    close_period,
    open_period,
)

october = await open_period(
    OpenPeriod(name="2026-10", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31)),
    AccountingUnitOfWork(),
)
await close_period(ClosePeriod(period_id=october.id), AccountingUnitOfWork())
```

A `Period` (its id is a `PeriodId`) is `open` or `closed`
(`PeriodStatus`). Its name is unique (`period_name_taken`, 409). It
covers its days, both ends included, and none of another's
(`periods_overlap`).

An entry's `booked_on` must fall in an open period (`no_open_period`),
unless `REQUIRE_PERIOD = False`, which books an entry dated outside
every period without one. One dated in a closed period is always refused
(`period_closed`). A close waits for the postings dated in the period
that are under way, and every posting after it is refused. A closed
period stays closed: corrections go in an open one, as reversals.

The books are closed up to the last day of the latest closed period
(FIN-87):

- no entry is dated there, in a period or in a gap between periods,
  whatever `REQUIRE_PERIOD` says (`period_closed`);
- no period is opened that starts there;
- a period closes only once every period before it is closed
  (`earlier_period_open`).

So a closed period's report reads the same for good: no later entry
moves its opening balances.

## Reports

```python
from domainkit.contrib.accounting import general_ledger, trial_balance

report = await trial_balance(october.id)
report.balanced  # every currency nets to zero
report.to_csv(), report.to_json()

async for piece in general_ledger(october.id, "csv"):  # or "json", streamed
    out.write(piece)
```

- The **trial balance** (`TrialBalance`) lists every account with a
  balance or movements up to the period's end (`TrialBalanceRow`): its
  `opening` balance before the period and its `closing` one (debits less
  credits, so credit balances are negative), and the period's `debits`
  and `credits`. Then one `total` per currency (`CurrencyTotal`), which
  nets to zero.
- The **general ledger** lists the postings dated in the period, by
  date, posting time, entry, and line, each with its `net` (debits
  positive). It ends with each currency's totals, `net` zero.
- The JSON is stable: keys sorted, no spaces, amounts as fixed-point text
  at their currency's places. So the same books give the same bytes. Its
  `format` names the report (`TRIAL_BALANCE_FORMAT`,
  `GENERAL_LEDGER_FORMAT` in `domainkit.contrib.accounting.reports`).
  CSV cells that a spreadsheet would run as a formula start with an
  apostrophe.

```text
booked_on,entry_id,source,reference,description,line,account_code,account_id,side,amount,currency,net,memo
2026-10-30,e050c3c3-6f6d-483b-8079-7d40822ea08a,payments,pay-1,Card top-up,1,1000,f0ebf153-ec4c-4868-b0b2-98e6b8ed5e69,debit,150.00,EGP,150.00,
2026-10-30,e050c3c3-6f6d-483b-8079-7d40822ea08a,payments,pay-1,Card top-up,2,2100-000042,021834f7-8516-4fc3-ba7e-7b4690b930b5,credit,150.00,EGP,-150.00,
,,,,total,,,,,,EGP,0.00,
```

The trial balance is one statement, the chart joined in it, however many
accounts moved. The general ledger is read in pieces of 500 postings,
each in a short transaction of its own, so a slow reader holds no
connection. A closed period's never changes, so its pieces read as one.

### Checking the books: `verify_ledger`

`verify_ledger()` (and `manage.py accounting verify`) checks the books
themselves: every entry balances, every balance is the sum of its
postings, and every account's `held` is the sum of its active holds. The
database compares, and only what differs comes back, streamed, as
`LedgerProblem`s. An empty list means the books balance:

```bash
python manage.py accounting verify
```

```text
The books balance.
```

With the audit trail installed, it also compares each entry with the
`EntryPosted` that the trail recorded for it, 500 entries at a time. The
digest of its stored date, description, and postings, and its source and
reference, must be the trail's (`entry_differs`, naming the account whose
amounts differ, or an entry the trail has and the books do not;
SEC-16b-6). Entries whose trail rows were pruned or excluded
(`AUDIT.EXCLUDE`), or posted before 0.15, are not compared; `audit
verify` says whether the trail is as written.

The audit trail vouches for each entry. `EntryPosted` carries, per
account and currency, its debits and credits (`amounts`, as
`EntryAmount`s), and the SHA-256 of its date, description, and lines
(`digest`), sealed and anchored with the trail (FIN-96). So a rewrite of
postings and balances that the sums cannot see, by a person who can
drop the triggers, no longer matches the trail, and `verify_ledger` says
so.

An entry's `posted_at` is the host's clock, as it is drafted (reading
the database's would cost a round trip per posting). A limit set later
counts back the postings since its window began by it, so keep the
hosts' clocks in step with the database's (NTP). A skew of seconds moves
only entries posted at a window's edge.

## Reconciliation

Reconciliation matches a bank's statement lines with the account's
postings, and records what is left over:

```python
from domainkit.contrib.accounting.reconciliation import (
    CsvStatement,
    breaks_of,
    import_statement,
    reconcile,
)

await import_statement(bank.id, CsvStatement(text, name="october.csv"))
done = await reconcile(bank.id)  # matches by rule, and the breaks left
await breaks_of(bank.id)  # what is still unmatched
```

**Statements** come in through the port `StatementSource`, as
`StatementLine`s. `CsvStatement` reads a CSV with the columns `id`,
`date` (ISO), `amount`, `currency`, and optionally `reference` and
`description`, or a bank's own names (`columns={"id": "Transaction ID",
...}`, `delimiter=";"`). Amounts are digits and a point, at most the
currency's places; anything else is refused (`invalid_statement`), never
read as a float. A statement's amount is signed as the account sees it:
positive is a debit, money in for a bank account kept as an asset.

```text
id,date,amount,currency,reference,description
tx-1001,2026-10-30,150.00,EGP,pay-1,Card top-up
```

**Imports** store lines once per account by their id, so importing a
file again imports nothing more. `import_statement` returns an
`Imported`: the lines stored, and those it had already. A line imported
before under an id with another date, amount, reference, or description
is refused, with the whole statement (`invalid_statement`), never
skipped. Every line is checked first, whatever its source, and at most
`MAX_STATEMENT_LINES` of them: an id of up to 128 characters, a
reference of up to 128 and a description of up to 255 (`CsvStatement`
cuts longer ones), none with control characters, which `manage.py
accounting unmatched` would write to a terminal. A file that is not CSV
(a field over 128 KiB, for example) is refused the same way.

**Matching.** `reconcile` tries the rules of `MATCHING_RULES` in order,
each on what the ones before left:

- `ByReference` pairs a line with the nearest posting whose entry has
  its reference, amount, and currency, within `MATCH_WINDOW_DAYS`;
- `ByAmountAndDate` pairs a line with a posting of the same amount and
  currency within the window, only where each is the other's one
  candidate.

Matches are recorded with their rule. What is left is recorded as
**breaks** (`Break`): lines without a posting, and postings without a
line dated within the days the statements cover. Each run replaces the
account's breaks, and returns a `Reconciled`: the matches by rule, and
the breaks left. `ByAmountAndDate` looks for a line's postings by
amount, and every rule runs on a thread, so a long statement stalls no
request.

Every import and every run is recorded on the account, over HTTP as from
`manage.py accounting`: `StatementImported` (the statement's name, the
lines imported and skipped, and a SHA-256 of its lines) and
`AccountReconciled` (the run, the matches by rule, the breaks left). So
the audit trail names who imported a statement and who reconciled
(FIN-90).

**Your own rule** is a `MatchingRule`: a class with a `name` and
`match(lines, postings, window)`, which gets `StatementItem`s and
`PostingItem`s and returns pairs. Add it by path:

```python
ACCOUNTING = {
    "MATCHING_RULES": [
        "domainkit.contrib.accounting.reconciliation.ByReference",
        "payments.reconciliation.BySettlementId",
    ],
}
```

A rule that pairs a line or a posting twice, or one it was not given
(another account's, or one matched already), fails the reconciliation
with `ImproperlyConfigured`, and nothing is recorded.

## Over HTTP

```python
from domainkit.contrib.accounting.api import router as accounting

router.include_router(accounting, prefix="/api/accounting")
```

Each resource (`accounts`, `entries`, `holds`, `periods` in
`domainkit.contrib.accounting.api`) is a `ResourceRouter`
([resources.md](../resources.md)): pages, filters, `ETag`s, and commands
through the services above. Every route needs a model permission,
whatever authenticates requests, so they all deny by default. Refusals
answer with their codes, as problem details where `ERROR_FORMAT =
"problem+json"`. Amounts are `{"amount": "10.50", "currency": "USD"}`,
the amount as text.

| Route | Permission | What it does |
| --- | --- | --- |
| `GET /accounts`, `/accounts/{id}` | `accounting.view_account` | The chart (`code`, `type`, `currency`, `status`, `holder`), balances, `held`, `available`. |
| `POST /accounts` | `accounting.add_account` | Open an account. |
| `POST /accounts/{id}/close` | `accounting.close_account` | Close one with nothing on it. |
| `POST /accounts/{id}/statements` | `accounting.reconcile_account` | Import a statement: `text/csv`, UTF-8, at most 10 MiB (`name=` names it). |
| `POST /accounts/{id}/reconcile` | `accounting.reconcile_account` | Reconcile; the matches by rule and the breaks. |
| `GET /accounts/{id}/breaks` | `accounting.view_account` | The breaks of the last reconciliation. |
| `GET /entries`, `/entries/{id}` | `accounting.view_entry` | Entries and their lines (`source`, `reference`, `booked_on`, `period_id`, `reverses`). |
| `POST /entries` | `accounting.add_entry` | Post an entry: `source`, `reference`, `booked_on`, `lines`, `description`. |
| `POST /entries/{id}/reverse` | `accounting.reverse_entry` | Post its reversal (`booked_on`, `description`). |
| `GET /holds`, `/holds/{id}` | `accounting.view_hold` | Holds (`account_id`, `status`, `source`, `reference`). |
| `POST /holds` | `accounting.add_hold` | Place a hold. |
| `POST /holds/{id}/capture` | `accounting.capture_hold` | Capture it (`counter_account_id`, `amount`, `booked_on`). |
| `POST /holds/{id}/release` | `accounting.release_hold` | Release it. |
| `GET /periods`, `/periods/{id}` | `accounting.view_period` | Periods. |
| `POST /periods` | `accounting.add_period` | Open a period. |
| `POST /periods/{id}/close` | `accounting.close_period` | Close it. |
| `GET /periods/{id}/trial-balance`, `/general-ledger` | `accounting.view_period` | The reports, as JSON or CSV by `Accept`. |

The permissions are per model, not per holder. A person who may
`view_account` sees every account, its `holder` too, and a person who
may `view_entry` sees every entry. `view_period` reads the reports too,
which list every account's balance and every posting of the period, so
grant it as you would grant `view_account` and `view_entry` together. To
show people their own accounts, serve them from routes of your own,
filtered by `holder`.

## From the command line

| Action | What it does |
| --- | --- |
| `accounting trialbalance PERIOD [--format csv\|json] [--output FILE]` | Write a period's trial balance; a period by name or id. |
| `accounting ledger PERIOD [--format csv\|json] [--output FILE]` | Write a period's general ledger. |
| `accounting verify` | Check the books; fails on a problem. |
| `accounting expireholds` | Release the holds whose time is up. |
| `accounting import ACCOUNT FILE` | Import a statement (CSV) into an account, by its code or id: `import 1000 october.csv`. |
| `accounting reconcile ACCOUNT` | Reconcile it. |
| `accounting unmatched ACCOUNT` | List what is unmatched. |

Imports, reconciliations, and expiries take `--operator` and are
recorded as the operator's actions
([commands](../reference/commands.md#operators)).

```bash
python manage.py accounting trialbalance 2026-10
```

```text
code,name,type,currency,opening,debits,credits,closing
1000,Cash,asset,EGP,0.00,150.00,0.00,150.00
2100-000042,Wallet,liability,EGP,0.00,0.00,150.00,-150.00
,total,,EGP,0.00,150.00,150.00,0.00
```

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

```python
ACCOUNTING = {"REQUIRE_PERIOD": True, "MATCH_WINDOW_DAYS": 3}
```

| `ACCOUNTING` key | Default | Meaning |
| --- | --- | --- |
| `MAX_LINES` | `100` | Lines one entry may have (2 to 1000). |
| `REQUIRE_PERIOD` | `True` | Refuse an entry dated where no period is open; `False` books it without a period, unless a closed one covers its date. |
| `MAX_ATTEMPTS` | `5` | Runs of a posting, the first included, when its transaction lost a race (at most 10). |
| `HOLD_TTL` | 7 days | How long a hold lasts when placed without `expires_at` (at most 366 days). |
| `EXPIRE_HOLDS_EVERY` | 1 minute | How often the worker releases expired holds, with messaging (1 second to 1 day). |
| `MATCH_WINDOW_DAYS` | `3` | Days a statement line's date may be from its posting's (at most 31). |
| `MATCHING_RULES` | `domainkit.contrib.accounting.reconciliation.ByReference`, `domainkit.contrib.accounting.reconciliation.ByAmountAndDate` | The rules a reconciliation tries, in order, by import path; it may not be empty. A path that is not a rule fails `setup()`. |
| `MAX_STATEMENT_LINES` | `10000` | Lines one statement may have (at most 1,000,000). |
| `REQUIRE_PROTECTION` | `True` | In staging and production, refuse to start on PostgreSQL without the triggers that keep entries and postings append-only (`accounting.E001`, or `accounting.E002` when they could not be checked); `False` logs `accounting.W001` instead. |

## Checks

| Id | When | What to do |
| --- | --- | --- |
| `accounting.E001` | Startup in staging or production on PostgreSQL: the append-only triggers are missing, disabled, or changed. | Run `migrate`, which makes them. |
| `accounting.E002` | The same, when the triggers could not be checked. | Make the database answer, then start again. |
| `accounting.W001`, `accounting.W002` | With `REQUIRE_PROTECTION = False` only: what `E001` and `E002` refuse, logged at startup. | The same. |
| `accounting.W003` | Deployed without messaging: nothing releases expired holds. | Install messaging, or schedule `accounting expireholds` and silence it. |

## On PostgreSQL and SQLite

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Concurrent postings | Accounts locked by ascending id; no deadlock, no lost update. | One posting at a time (the database's write lock). |
| Entries and postings | Append-only by triggers, checked at startup. | Append-only by the application only. |
| Hold expiry | Judged by the database's clock. | Judged by the host's clock. |

Amounts are exact on both (`NUMERIC(18, 4)`).

## Errors and refusals

| Error | Code | HTTP |
| --- | --- | --- |
| `AccountNotFound`, `EntryNotFound`, `HoldNotFound`, `PeriodNotFound` | `account_not_found`, `entry_not_found`, `hold_not_found`, `period_not_found` | 404 |
| `AccountCodeTaken`, `PeriodNameTaken` | `account_code_taken`, `period_name_taken` | 409 |
| `ReferenceReused` | `reference_reused` | 409 |
| `HoldNotActive` | `hold_not_active` | 409 |
| `InvalidAmount` | `invalid_amount` | 422 |
| `InvalidEntry` | `invalid_entry` | 422 |
| `UnbalancedEntry` | `unbalanced_entry` | 422 |
| `AccountNotOpen` | `account_closed` | 422 |
| `AccountNotEmpty` | `account_not_empty` | 422 |
| `InsufficientFunds` | `insufficient_funds` | 422 |
| `NoOpenPeriod` | `no_open_period` | 422 |
| `PeriodNotOpen` | `period_closed` | 422 |
| `EarlierPeriodOpen` | `earlier_period_open` | 422 |
| `PeriodsOverlap` | `periods_overlap` | 422 |
| `EntryNotReversible` | `entry_not_reversible` | 422 |
| `InvalidStatement` | `invalid_statement` | 422 |

## Common mistakes

- **Putting a person's name or id in an account code.** Erasure keeps
  codes as written. Use an opaque number from your own sequence.
- **Changing an entry to fix it.** Entries never change. Reverse it, and
  post the right one.
- **Posting the same payment under two references.** It books twice. Use
  one stable reference per thing that happened.
- **Opening a clearing account without `may_go_negative=True`.** It
  refuses to go below zero.
- **Granting `view_period` widely.** The reports show every account and
  posting. Grant it as you grant `view_account` and `view_entry`.

## See also

- [Money](../money.md): `Money`, currencies, and exact amounts.
- [Limits](limits.md): limits on what moves, counted per entry.
- [Approvals](approvals.md): a second person for large postings.
- [Audit](audit.md): the trail that vouches for each entry.
- [Privacy](../privacy.md): export and erasure of holders.

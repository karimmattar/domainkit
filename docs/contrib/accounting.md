# Accounting: double-entry books, holds, periods, and reconciliation

`domainkit.contrib.accounting` keeps a project's books: a chart of
accounts, journal entries whose postings always balance, balances kept
with them, holds on what an account has available, accounting periods
that close, a trial balance and a general ledger per period, and the
reconciliation of a bank's statements with the postings. It is what a
wallet, a payment service, or a lender needs underneath, with the hard
parts done once: concurrent postings that neither deadlock nor lose an
update, a retried request or a message delivered twice that books once,
and entries that never change. The decisions are ADRs
[0116](../adr/0116-keep-double-entry-books-locked-in-id-order-and-append-only.md)
and [0117](../adr/0117-reconcile-statements-through-a-port-with-rules-in-order-and-record-breaks.md);
amounts are `domainkit.money.Money` (ADR 0115).

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # optional: entries from messages, hold expiry
    "domainkit.contrib.accounting",
    ...,
]
```

Then `makemigrations` and `migrate`: the app brings eight tables
(`domainkit_accounting_accounts`, `_periods`, `_entries`, `_postings`,
`_holds`, `_statement_lines`, `_matches`, `_breaks`), in the database
`DATABASE_ROUTING['accounting']` names, or `default`. That is one
database: the ledger refuses a shard group, since an entry's accounts
must commit together. Across databases, use a clearing account and an
entry on each side, linked through the outbox.

## Accounts

```python
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    OpenAccount,
    open_account,
)

cash = await open_account(
    OpenAccount(code="1000", name="Cash", type=AccountType.ASSET, currency="EGP"),
    AccountingUnitOfWork(),
)
wallet = await open_account(
    OpenAccount(
        code=f"2100-{wallet_number:06d}",  # The project's own number, not the user's.
        name="Wallet",
        type=AccountType.LIABILITY,
        currency="EGP",
        holder=str(user.id),
        may_go_negative=False,
    ),
    AccountingUnitOfWork(),
)
```

- A **code** has up to 32 letters, digits, `.`, `_`, or `-`, unique
  (`account_code_taken`, 409). A **name** has up to 200 characters.
- The **type** decides the normal balance: assets and expenses rise with
  debits, liabilities, equity, and income with credits. An account's
  `balance` is in that sign: a wallet's credits less its debits.
- One **currency** per account, of at most four decimal places; a line in
  another currency is refused. An unknown currency, or an amount with more
  places than its currency, is refused as `domainkit.money` refuses it
  (`unknown_currency`, `inexact_amount`, 422).
- `held` is what its active holds reserve, and `available` the balance
  less that. An account that `may_go_negative=False` refuses a posting or
  a hold that would leave less than nothing available
  (`insufficient_funds`); others may be overdrawn.
- `holder` names the person the account is kept for: `privacy export`
  lists their accounts and postings; `privacy erase` keeps the books (the
  law asks for them, and they are append-only) and unlinks the person
  from them, clearing `holder`. It is the only field erasure clears:
  codes, names, descriptions, memos, references, and statement lines are
  kept as written, and a code is shown by every report and by the audit
  trail's `AccountOpened`. Keep personal data out of all of them, as the
  example does: an opaque code (`2100-000042`, from the project's own
  sequence), never the person's id or name.
- `close_account(CloseAccount(account_id=...))` closes an account with
  nothing on it (`account_not_empty` otherwise); a closed one takes no
  postings or holds (`account_closed`).

## Entries

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

- An entry has 2 to `MAX_LINES` lines, each more than zero on one side
  of one account. Each currency's debits must equal its credits, or the
  entry is refused (`unbalanced_entry`, 422); an entry may mix
  currencies, through accounts of each.
- **Once per source and reference.** `source` (up to 64 lower-case
  letters, digits, `_`, `.`, `:`, `-`) and `reference` (up to 128
  printable characters) name the entry where it came from. Posted again
  under them, the same entry is returned and nothing more is booked: a
  retried request, a message delivered twice. A different entry under
  them is refused (`reference_reused`, 409). The sources that start with
  `accounting.` are the ledger's own (reversals and captures post under
  them): `post_entry` refuses them (`invalid_entry`, 422).
- **Concurrency.** A posting locks the name of its source and reference,
  then its accounts by ascending id, so two entries that share accounts
  wait for each other in one order and never deadlock; their balances
  move in the same transaction, so no update is lost. A transaction that
  loses a race anyway runs again, whole, up to `RETRY_ATTEMPTS` times
  ([databases.md](../databases.md#running-work-again)). On SQLite
  postings run one at a time.
- **Never changed.** `reverse_entry(ReverseEntry(entry_id=...))` posts the
  entry that undoes another, each posting on the other side, dated today
  unless `booked_on` says; an entry is reversed once (its source is
  `accounting.reversal`, its reference the entry's id), and reversing it
  again returns that reversal. A reversal is not reversed
  (`reversal_not_reversible`, 422): that would book the entry it undid
  again, past the checks a new entry meets (limits, approvals); post the
  entry again instead, under a reference of its own. On PostgreSQL,
  triggers that `migrate` makes afresh each time refuse every update,
  delete, and truncation of entries and postings, the table owner's too;
  in staging and production the application refuses to start without
  them as `migrate` makes them: enabled, firing for every row (no `WHEN`,
  no column list), running their own function with its body
  (`accounting.E001`). `flush`, `dumpdata`, and `loaddata` leave the
  ledger's tables alone.
- `EntryPosted`, like every event of the app, is recorded by
  `contrib.audit` when it is installed.
- **Checks before posting.** An app may refuse an entry before
  `post_entry` takes any lock: it registers `async def hook(command)` on
  `domainkit.contrib.accounting.hooks.on_posting` in `ready()`, and
  raises. `domainkit.contrib.approvals` refuses there the entries over
  `APPROVALS.ENTRY_THRESHOLDS` that no approved request posts
  (`approval_required`, 403; [approvals.md](approvals.md#large-postings)).
- **Checks as entries are booked.** An app sees every entry as it is
  booked: a function registered in its `ready()` with
  `on_booking(hook)` (from `domainkit.contrib.accounting`) is awaited as
  `hook(uow, entry, accounts)` in the posting's transaction, after its
  accounts and period are locked and their balances moved, for entries
  posted, reversals, and captures. Raising refuses the entry; rows it
  locks come after the accounts, in an order of its own.
  [Limits](limits.md#on-the-ledger) count each entry this way.

### From messages

A use case that should book an entry once its own change commits defers
it, through the outbox:

```python
from domainkit.contrib.accounting import defer_entry

async with orders_uow as uow:
    order.mark_paid()
    defer_entry(uow, PostEntry(source="orders", reference=str(order.id), ...))
    await uow.commit()
```

The worker posts it with the task `domainkit.accounting.post_entry`,
under the inbox (ADR 0094): a message run again after its entry
committed is skipped, and a second message about the same entry returns
the first one's. A refused entry (a closed period, say) is dead-lettered
at once; `messages --retry` runs it again once the books allow it. A
posting that lost its race `RETRY_ATTEMPTS` times (`ConcurrentUpdate`)
is not refused: the worker runs it again after its backoff, and the
source and reference book it once.

## Holds

```python
from domainkit.contrib.accounting import (
    CaptureHold,
    PlaceHold,
    capture_hold,
    place_hold,
)

hold = await place_hold(
    PlaceHold(account_id=wallet.id, amount=amount, source="cards", reference=auth_id),
    AccountingUnitOfWork(),
)
await capture_hold(
    CaptureHold(hold_id=hold.id, counter_account_id=merchants.id, amount=final),
    AccountingUnitOfWork(),
)
```

A hold reserves an amount on an account until it is captured, released,
or expires (`expires_at`, or `HOLD_TTL` from now). `expires_at` is a
time with its zone, in the future and at most 366 days away
(`invalid_entry` otherwise; over HTTP, a time without a zone is a
validation error, 422). It moves no balance:
it lowers `available`. It is placed once per source and reference, as an
entry is. A capture posts an entry out of the account (the side that
lowers its balance) and into `counter_account_id`, of the whole hold or
less, and releases the rest; capturing again returns the captured hold.
`release_hold` gives it back. An expired hold cannot be captured
(`hold_not_active`); with messaging, the worker releases expired holds
every `EXPIRE_HOLDS_EVERY`, and `manage.py accounting expireholds` does
the same on demand. Until then an expired hold still counts against
`available`. Without messaging, `check` warns in staging and production
(`accounting.W003`): run `expireholds` from the project's scheduler, and
silence it.

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

A period covers its days, both ends included, and none of another's
(`periods_overlap`). An entry's `booked_on` must fall in an open period
(`no_open_period`), unless `REQUIRE_PERIOD = False`, which books an entry
dated outside every period without one; one dated in a closed period is
always refused (`period_closed`). A close waits for the postings dated
in the period that are under way, and every posting after it is refused.
A closed period stays closed: corrections go in an open one, as
reversals.

## Reports

```python
from domainkit.contrib.accounting import general_ledger, trial_balance

report = await trial_balance(october.id)
report.balanced  # every currency nets to zero
report.to_csv(), report.to_json()

async for piece in general_ledger(october.id, "csv"):  # or "json", streamed
    out.write(piece)
```

- The **trial balance** lists every account with a balance or movements
  up to the period's end: its `opening` balance before the period and its
  `closing` one (debits less credits, so credit balances are negative),
  and the period's `debits` and `credits`; then one `total` per currency,
  which nets to zero.
- The **general ledger** lists the postings dated in the period, by
  date, posting time, entry, and line, each with its `net` (debits
  positive), and ends with each currency's totals, `net` zero.
- The JSON is stable: keys sorted, no spaces, amounts as fixed-point text
  at their currency's places, so the same books give the same bytes.
  CSV cells a spreadsheet would run as a formula start with an
  apostrophe.
- `verify_ledger()` (and `manage.py accounting verify`) checks the books
  themselves: every entry balances, every balance is the sum of its
  postings, and every account's `held` the sum of its active holds.

## Reconciliation

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

- Statements come in through the port `StatementSource`; `CsvStatement`
  reads a CSV with the columns `id`, `date` (ISO), `amount`, `currency`,
  and optionally `reference` and `description`, or a bank's own names
  (`columns={"id": "Transaction ID", ...}`, `delimiter=";"`). Amounts are
  digits and a point, at most the currency's places; anything else is
  refused (`invalid_statement`), never read as a float. A statement's
  amount is signed as the account sees it: positive is a debit, money in
  for a bank account kept as an asset.
- Lines are stored once per account by their id: importing a file again
  imports nothing more. A line imported before under an id with another
  date, amount, reference, or description is refused, with the whole
  statement (`invalid_statement`), never skipped. Every line is checked
  first, whatever its source, and at most `MAX_STATEMENT_LINES` of them:
  an id of up to 128 characters, a reference of up to 128 and a
  description of up to 255 (`CsvStatement` cuts longer ones), none with
  control characters, which `manage.py accounting unmatched` would write
  to a terminal. A file that is not CSV (a field over 128 KiB, say) is
  refused the same way.
- `reconcile` tries the rules of `MATCHING_RULES` in order, each on what
  the ones before left: `ByReference` pairs a line with the nearest
  posting whose entry has its reference, amount, and currency, within
  `MATCH_WINDOW_DAYS`; `ByAmountAndDate` pairs one with the same amount
  and currency within the window, only where each is the other's one
  candidate. Matches are recorded with their rule. What is left is
  recorded as **breaks**: lines without a posting, and postings without a
  line dated within the days the statements cover. Each run replaces the
  account's breaks.
- A project's own rule is a class with a `name` and `match(lines,
  postings, window)` returning pairs (`MatchingRule`), added by path:
  `ACCOUNTING = {"MATCHING_RULES": [..., "payments.reconciliation.BySettlementId"]}`.
  A rule that pairs a line or a posting twice, or one it was not given
  (another account's, or one matched already), fails the reconciliation
  (`ImproperlyConfigured`) and nothing is recorded.

## Over HTTP

```python
from domainkit.contrib.accounting.api import router as accounting

router.include_router(accounting, prefix="/api/accounting")
```

Each resource is a `ResourceRouter` ([resources.md](../resources.md)):
pages, filters, `ETag`s, and commands through the services above. Every
route needs a model permission, whatever authenticates requests, so they
all deny by default; refusals answer with their codes, as problem details
where `ERROR_FORMAT = "problem+json"`. Amounts are `{"amount": "10.50",
"currency": "USD"}`, the amount as text.

| Route | Permission | Does |
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

The permissions are per model, not per holder: whoever may
`view_account` sees every account, its `holder` among them, and whoever
may `view_entry` every entry. `view_period` reads the reports too, which
list every account's balance and every posting of the period, so grant
it as you would `view_account` and `view_entry` together. A project that
shows people their own accounts serves them from routes of its own,
filtered by `holder`.

## From the command line

`manage.py accounting` writes a period's report (`trialbalance` or
`ledger`, by the period's name or id, `--format csv|json`, `--output
FILE`), checks the books (`verify`, which fails on a problem), releases
expired holds (`expireholds`), imports a statement into an account (by
its code or id: `import 1000 october.csv`), reconciles it (`reconcile
1000`), and lists what is unmatched (`unmatched 1000`). Imports,
reconciliations, and expiries are recorded as the operator's actions
([commands](../reference/commands.md#operators)).

## Settings

```python
ACCOUNTING = {"REQUIRE_PERIOD": True, "MATCH_WINDOW_DAYS": 3}
```

| `ACCOUNTING` key | Default | Meaning |
| --- | --- | --- |
| `MAX_LINES` | `100` | Lines one entry may have (2 to 1000). |
| `REQUIRE_PERIOD` | `True` | Refuse an entry dated where no period is open; `False` books it without a period, unless a closed one covers its date. |
| `RETRY_ATTEMPTS` | `5` | Runs of a posting, the first included, when its transaction lost a race (at most 10). |
| `HOLD_TTL` | 7 days | How long a hold lasts when placed without `expires_at` (at most 366 days). |
| `EXPIRE_HOLDS_EVERY` | 1 minute | How often the worker releases expired holds, with messaging (1 second to 1 day). |
| `MATCH_WINDOW_DAYS` | `3` | Days a statement line's date may be from its posting's (at most 31). |
| `MATCHING_RULES` | `domainkit.contrib.accounting.reconciliation.ByReference`, `domainkit.contrib.accounting.reconciliation.ByAmountAndDate` | The rules a reconciliation tries, in order, by import path; it may not be empty. A path that is not a rule fails `setup()`. |
| `MAX_STATEMENT_LINES` | `10000` | Lines one statement may have (at most 1,000,000). |
| `REQUIRE_PROTECTION` | `True` | In staging and production, refuse to start on PostgreSQL without the triggers that keep entries and postings append-only (`accounting.E001`, or `accounting.E002` when they could not be checked); `False` logs `accounting.W001` instead. |

# 0123. Count movements against limits under the subject's row, in the movement's transaction

- Status: Accepted; amended 2026-10-31 (at the end)
- Date: 2026-10-31

## Context

The fintech audit's FIN-47: domainkit had no transaction or velocity
limits. `domainkit.http.ratelimit` counts requests per caller in a
cache; a limit on money counts amounts, per subject (an account, the
holder it is kept for, the actor, or a key of the project's), per
currency, per transaction and over a window (a calendar day or month, or
the last 24 hours or 30 days). It must be exact, judged by one clock,
and right under concurrency: two payments at once must not both pass a
limit that only one of them fits. A count must also commit with its
movement or roll back with it, and a reversal or a refund must give it
back.

Three designs were measured on PostgreSQL 16 (asyncpg, `READ COMMITTED`),
each enforcing a holder's daily limit of 1,000.00 from inside the
ledger's posting (`on_booking`, below): 50 payments started at once,
three runs each, all of 25.00 (exactly 40 fit), then of 50 mixed amounts
from 1.00 to 60.00. The holder's payments came from one wallet, then from
five wallets, each paying into a till of its own, so that the ledger's
account locks (ADR 0116) order only the payments of one wallet:

| Design | One wallet, 25.00 each | Five wallets, 25.00 each | Five wallets, mixed | Statements per payment |
| --- | --- | --- | --- | --- |
| A. a usage row per (limit, subject, window), locked `FOR UPDATE` in the posting's transaction | 40 of 50 passed (1,000.00), 3 runs | 40 (1,000.00), 3 runs | 34 to 35, 997.16 to 998.74 | 9.1 to 9.6 |
| B. the sum of the day's postings, under the account locks the posting holds | 40 (1,000.00), 3 runs | 42, 40, 41: **1,050.00**, 1,000.00, **1,025.00** | 36 to 41: **1,037.54**, **1,139.25**, **1,074.25** | 8.4 to 8.7 |
| C. a hold on an account of the limit (balance 1,000.00, may not go negative), then the posting | 40 (1,000.00), 3 runs | 40 (1,000.00), 3 runs | 33 to 36, 989.47 to 998.57 | 9.6 to 11.0 |

None deadlocked or lost a race. B is right only where the subject is one
account: the account locks are what orders it, and a holder's payments
from two wallets hold different locks, so both read the same sum and
both pass, up to 14% over the limit. It also sums every posting of the
window on each payment, and cannot count a subject the ledger does not
lock (an actor, a merchant). C is exact, since the hold locks the
limit's account, but it is two transactions: a payment refused after its
hold leaves the hold to be released (or to expire), the books gain an
account per subject per window that the trial balance shows, a count or
a rolling window has no balance to hold against, and a use case outside
the ledger has no account to hold on. A is exact, in the movement's own
transaction, for any subject, and costs about one statement more than
B.

A, as measured, keeps one counter per limit and window: changing a
limit's window (a day to a month) or replacing the limit for every
holder by one for a holder would start its count at zero, and a rolling
window has no counter to keep. The chosen design keeps A's lock and
moves the count into rows: **A row per subject** (its kind, the
direction, the currency, and the subject) is locked by an upsert
(`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, one statement that
makes the row or locks it, and returns the database's time), then **the
subject's usage**, one row per movement with its amount, its time, and
the key it was counted under, is summed per window in one statement
(`sum(...) FILTER (WHERE at >= start)` for each limit). Measured the same
way: 40 of 50 passed (1,000.00), 10 refused, in each of the six runs of
25.00, one wallet or five; 33 to 38 passed of the mixed amounts, never
over (999.81 at most); none failed or was retried; 11.0 to 11.4
statements per payment, against 8 without limits. The concurrency test
of the suite (`test_limits.py`) runs the five-wallet case on every CI
run.

**Order.** The subject rows are locked in the order of their keys, after
whatever the use case locked: in the ledger, after the entry's name, its
accounts by id, and its period (ADR 0116). A use case that counts takes
its own rows first. Release takes no lock: it only lowers usage, so a
movement that reads the sum before a release commits is refused at worst,
never let through.

**Clock.** Windows are judged by the database's clock, as the outbox's
leases are (ADR 0043): the upsert writes `statement_timestamp()` into the
subject row and returns it, so the time costs no statement of its own,
and every host judges the same window. SQLite, and a frozen clock in
tests, use `domainkit.clock`. A calendar window starts at midnight in the
limit's `time_zone` (by default `TIME_ZONE` when it is made), a rolling
one 24 hours or 30 days ago, its start not included.

**Releasing.** The count is in the movement's transaction: a movement
refused after it was counted, by the ledger (`insufficient_funds`) or by
the use case, rolls the count back with it. `release(uow, key)` marks
what a key counted as released, so it counts in no window; the ledger
calls it for the entry a reversal undoes.

**The ledger.** `contrib.accounting` gains one hook point,
`on_booking(uow, entry, accounts)`, called in `book()` after the balances
moved and before the entry is added, for every entry posted, reversed, or
captured; limits registers on it in `ready()` (ADR 0101), so the ledger
does not import limits. Each entry is a movement per account and per
holder (out where a posting lowers the balance, in where it raises it),
and the request's actor moved its debits, out. The limits must live in
the ledger's database, or `setup()` fails (`limits.E001`).

**Configuration.** Limits are rows (`domainkit_limits_limits`), changed
through a unit of work whose events (`LimitSet`, `LimitRemoved`) the
audit trail records with their actor, over a `ResourceRouter` guarded by
model permissions and by `manage.py limits set`. `LIMITS.DEFAULTS` gives
limits in the settings, checked by `setup()` (`limits.E002`); a stored
limit of the same key replaces one. Of the limits of one code, one for
the subject replaces the one for every subject; a disabled one exempts
it.

## Decision

- `domainkit.contrib.limits`: limits per transaction and per window
  (calendar day or month, last 24 hours or 30 days) of an amount and a
  count, per kind of subject, currency, and direction, for every subject
  or one; amounts are `Money` of up to four places, stored exactly.
- `consume(uow, *movements, key=)` counts in the caller's unit of work,
  under the subject's row, locked by an upsert in the order of the
  subjects' keys, against the sum of its usage in each window, on the
  database's clock; `check` reads without counting; `release(uow, key)`
  gives a key's usage back.
- A breach is `LimitExceeded` (422 `limit_exceeded`) or
  `TransactionLimitExceeded` (422 `transaction_limit_exceeded`), naming
  the limit and window on the error.
- The ledger calls `on_booking` for every entry it books; limits counts
  each entry there, and releases what a reversed entry counted.
- Usage older than `LIMITS.KEEP_USAGE` (35 days, more than any window) is
  purged by a periodic task with messaging, or `manage.py limits purge`;
  without messaging `check` warns where deployed (`limits.W001`).

## Consequences

- A subject's movements wait for each other, as an account's postings
  do: a subject that moves money very often (a merchant's settlement
  account) is a queue. Bound such flows by a key of the project's that
  spreads them, or keep them out of limits.
- The sum costs one indexed statement per subject, and grows with the
  movements a subject made in its longest window: thousands a month are
  a millisecond; a subject with millions needs a limit on another key.
- An entry booked when no limit bounds its subjects costs one statement
  (the look-up of the limits); one counted, four.
- Holds are not counted when placed, only when captured: a project that
  wants an authorisation refused at once calls `check` before
  `place_hold`.
- Left open: limits over windows of other lengths (a week, a quarter),
  amounts converted between currencies (one limit per currency instead),
  and limits on what is held rather than what moved.

## Amendment (2026-10-31): every movement is counted

As decided, a movement was written to usage only for a subject some
window bounded when it moved. A limit set partway through its window, a
limit that gained a window, or an exemption lifted, then ignored what
moved earlier in the window: a daily 1,000 set at 11:00 let another 900
through after the holder's 900 at 10:00 (SEC-15b-5).

Two fixes were weighed. Counting the window's earlier movements when a
limit changes, from the ledger's postings, covers only the ledger's
subjects (an actor's movements are not in the postings, nor a project's
own `consume` calls), must lock every subject a limit for every subject
covers, and must tell postings already counted from those not. Counting
every movement always is one rule for every subject. Its cost, measured
on the ledger (100 postings of one wallet into a till, after one to warm
up, SQLite and PostgreSQL 16 alike):

| | No limit bounds the entry | A daily limit of the holder |
| --- | --- | --- |
| Before | 9 statements a posting | 12 |
| Every movement counted | 11 | 13 |

The two statements are one insert of the subjects' rows that are missing
(`ON CONFLICT DO NOTHING`, which locks no row that is there) and one
insert of the usage rows, each finding its subject's id by the subject's
key; the per-posting time moved within the noise of the shared host
(PostgreSQL: 11.8 ms to 12.6 ms without limits). A subject no window
bounds takes no lock, so an actor or a clearing account that many
postings share does not make them wait. Usage grows by a row per
subject per movement, which `purge_usage` keeps to `KEEP_USAGE`.

A holder or an actor that limits cannot take as a subject (a control
character, more than 255 characters) refused every posting to its
accounts once limits were installed; it is counted under its digest
instead (`ledger_subject`).

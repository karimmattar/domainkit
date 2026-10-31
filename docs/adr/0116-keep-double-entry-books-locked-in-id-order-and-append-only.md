# 0116. Keep double-entry books: post under locks taken in id order, and append only

- Status: Accepted
- Date: 2026-10-30

## Context

The fintech audit's FIN-07, FIN-08, and FIN-10: domainkit had no
accounts, journal entries, balances, holds, or accounting periods.
`domainkit.ledger` is an immudb key-value port for evidence (ADR 0011),
not accounting, so the new app is `domainkit.contrib.accounting`. A
project building a wallet, a payment service, or lending writes the same
things each time, and gets the hard parts wrong in the same places: a
balance read, changed, and written by two requests at once; two
transfers that lock the same two accounts in opposite orders; a retried
request or a message delivered twice that books twice; an entry changed
after the books were closed. The questions were where balances live, how
a posting locks, how it is made idempotent, what keeps entries from
changing, how holds count, and what a period guards.

**Balances.** Summing the postings at read time is always right, and
grows with them; a balance projection must move in the posting's own
transaction or it drifts. The balance is a column of the account, in its
normal sign (an asset's debits less credits, a liability's credits less
debits), updated by the unit of work that inserts the postings, under
the account's row lock and its `version` (ADR 0004). `verify_ledger`,
and `manage.py accounting verify`, check every balance against its
postings, and every entry's debits against its credits per currency.

**Locks.** A proof of concept on PostgreSQL 16, asyncpg, `READ
COMMITTED`: 50 transfers started at once between random pairs of six
accounts, three runs each, each posting in one transaction with one run
(`RETRY_ATTEMPTS = 1`), so a lost race shows:

| How a posting reads its accounts | Transfers that failed | Why |
| --- | --- | --- |
| `get_for_update` per line, in the order of the lines | 33, 38, 41 of 50 | deadlocks (`40P01`) and lock timeouts |
| `get` without a lock (the `version` column alone) | 47, 47, 47 of 50 | `ConcurrentUpdate`: no update was lost, but nearly every one was refused |
| `get_many_for_update`, every account in one statement, by ascending id | 0, 0, 0 of 50 | they wait in one order |

None of the three left a balance that differs from its postings: the
`version` column refuses what the lock does not order. Only the third
lets them all through. A posting takes, in this order: a named lock of
its source and reference (`uow.lock`), the rows of any holds it changes,
its accounts by ascending id, and its period's row for share (below).
Every path that changes the books takes them in that order, so none waits
for another that waits for it. On SQLite the named lock begins the
transaction with the database's write lock, so postings run one at a
time there. Each posting runs through `uow.run(..., retry=Retry(...))`
(ADR 0092), `ACCOUNTING.RETRY_ATTEMPTS` (5) runs at most, for the
conflicts that remain possible (a lock timeout is not one); in the proof
of concept no run was retried. A posting of two lines takes 8
statements on either database, and one posted again 3.

**Idempotency.** An entry carries `source` and `reference`, unique
together in its table, and a digest of what it books. Posted again under
them, the same entry is returned, and a different one is refused
(`reference_reused`, 409). The named lock of the two makes the look-up
and the insert one step, and the unique constraint holds if anything
went around it. Entries posted from messages go through the outbox as
the task `domainkit.accounting.post_entry` (`defer_entry`), under the
inbox (`@inbox`, ADR 0094): a message delivered again after its commit is
skipped, and a second message about the same entry finds the first's.
Holds take the same key.

**Append only.** Corrections are reversing entries (`reverse_entry`,
the source `accounting.reversal` and the reversed entry's id, so an entry
is reversed once). On PostgreSQL, triggers refuse `UPDATE`, `DELETE`, and
`TRUNCATE` on entries and postings for every role, as `contrib.audit`'s
`protect()` does. The audit trail asks a project to call `protect(op)`
from a revision of its own; a ledger whose project forgot is as open as
one without triggers, so here `migrate` makes them (`on_migrated`,
ADR 0101), creating any missing or disabled, and staging and production
refuse to start without them (`accounting.E001`, unless
`REQUIRE_PROTECTION = False`). `flush` and fixtures leave every table of
the app alone: a loaded posting would not move its balance.

**Holds.** The audit proposed posting holds to "held" sub-accounts. A
hold is a reservation, not a movement: posting it would put two entries
in the books for each authorisation that is released, and make the
trial balance show money that never moved. A hold is a row of its own
instead, and the account's `held` column its active holds' sum, moved
under the account's lock; `available` is the balance less it. An account
that `may_go_negative` is False refuses any posting or hold that would
leave less than nothing available. A capture posts the entry, out of the
held account and into another, under the source `accounting.hold` and
the hold's id, and releases what it did not take. An expired hold cannot
be captured; the worker releases it within `EXPIRE_HOLDS_EVERY`.

**Periods.** A period covers its days, none of another's. A posting
locks its period's row for share; a close locks it for update. So a
close waits for the postings dated in the period that are under way,
and a posting after it reads the period closed (`READ COMMITTED` reads
the row again once the lock is granted), and is refused. An entry dated
where no period is open is refused too (`no_open_period`), unless
`REQUIRE_PERIOD = False`.

**Amounts** are `domainkit.money.Money` in and out, stored as
`ExactDecimal(18, 4)` and the currency's code (ADR 0066; ADR 0115's type
may replace the pair), so currencies of up to four places are exact on
both databases, and a currency of more is refused when an account opens.

## Decision

- `domainkit.contrib.accounting`: accounts (a code, a type and its normal
  balance, one currency, open or closed, a holder), journal entries of
  two or more postings that balance per currency, holds, and periods;
  each change a command run by a service, `service(command, uow)`.
- Balances are columns of the accounts, moved in the posting's
  transaction, under row locks taken by ascending id after a named lock
  of the entry's key, and the period's row for share.
- Entries are idempotent by `(source, reference)`, posted from messages
  under the inbox, append-only, protected by triggers `migrate` makes,
  and corrected by reversal.
- Holds are rows, summed into the account's `held`; a capture posts.
- Reports (`trial_balance`, `general_ledger`) read one statement each,
  as CSV and stable JSON; each sums to zero per currency.

## Consequences

- Postings to one account wait for each other; a hot account (a bank's
  clearing account) is a queue. Split it into several accounts and sweep
  them, or batch its postings.
- The ledger lives in one database: `DATABASE_ROUTING['accounting']` may
  not name a shard group. Across databases, use a clearing account and
  an entry on each side, linked through the outbox.
- An entry's description and memos are kept as written: they must not
  hold personal data. `privacy erase` unlinks a holder from their
  accounts and keeps the books; `privacy export` lists them.
- A ledger moved off PostgreSQL loses the triggers; SQLite has no roles
  to keep out and gets none.
- Left open: a digest of each day's journal recorded as evidence
  (tamper evidence beyond the triggers), credit limits in `available`,
  multi-currency revaluation, and periods that close in steps.

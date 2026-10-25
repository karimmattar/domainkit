# 0047. Seal the audit trail from a high-water mark, with a short late window

- Status: Accepted; amended 2026-10-21 (at the end)
- Date: 2026-10-20; amended 2026-10-21 (the database's clock, sealing late rows)

## Context

`seal` gives each unsealed audit row the next link of a hash chain. To
find unsealed rows, each batch anti-joined the trail against the seals from
a day (`LOOKBACK`) before the newest sealed row, so rows that committed
late, after newer rows were sealed, were still found. Every batch therefore
scanned a day of sealed rows. On SQLite, sealing 10 new rows after 20k,
80k, and 320k rows sealed over a day took 15, 93, and 493 ms; at millions
of rows a day, runs outgrew their task timeout and sealing fell behind for
good, silently widening the window in which rows can be rewritten
unnoticed.

Rows have no commit order the database exposes portably: `occurred_at` is
set by the application when the row is built, and ids are random.

Options considered:

- **A queue of unsealed ids**, written with each audit row. Exact, but a
  second write per audited row and a schema change for every project.
- **The database's commit order** (PostgreSQL's `xmin`). Not portable, and
  wraps around.
- **A high-water mark and a short window for late rows.**

## Decision

- The newest sealed row (the chain's head) is the high-water mark. A run
  looks for unsealed rows from `AUDIT.SEAL_LATE_WINDOW` (15 minutes, at
  most a day) before its `occurred_at`. Later batches of the same run go on
  from the `(occurred_at, id)` of the last row sealed, since every unsealed
  row before it was sealed by then.
- A run seals at most `AUDIT.SEAL_BATCHES` batches of 1,000 rows. When it
  stops at that bound, it counts the rows sealing could still reach, sets
  the `audit_unsealed_rows` gauge, and logs a warning past
  `AUDIT.SEAL_BACKLOG_WARNING`.
- The chain, the digests, the anchors, and `verify` are unchanged.
  `verify` still reports rows older than `LOOKBACK` (a day) that have no
  seal, which now includes rows that committed later than the window.
- The seal task also makes the keys of today's and tomorrow's anonymous
  callers, in a transaction of its own, so no request inserts the day's key
  inside its transaction at midnight.

## Consequences

- A run costs what is new plus the rows of the late window: the same 10
  rows now take 8.4, 8.6, and 14.7 ms. Rows written within the window are
  still scanned, so a burst of rows inside 15 minutes costs what it did.
- A row whose transaction commits more than the window after a newer
  row was sealed stays unsealed, and `auditverify` reports it a day later.
  Projects with long transactions, or hosts with skewed clocks, raise the
  window and pay for it in scan time.
- A backlog is visible (a gauge and a warning) instead of growing quietly.

## Addendum (2026-10-21): the database's clock, and sealing late rows

Two ways left rows unsealed for good, so they could be rewritten
unnoticed:

- **A sealer whose clock runs ahead.** The mark was capped at the sealer's
  own `now`. A host 40 minutes ahead that writes and seals rows moved the
  mark, and the window, past the rows hosts with right clocks committed
  afterwards.
- **Rows later than the window.** Nothing could seal them; `auditverify`
  reported them a day later, for ever.

Options considered for the late rows:

- **Seal every unsealed row older than the window on every run.** Nothing
  waits for an operator, but finding them anti-joins the whole trail
  before the mark on every run, the cost this record removed.
- **A queue of unsealed ids, or the commit order.** Rejected above.
- **An explicit pass, `auditseal --late`, with its seals flagged.** The
  cost is paid when asked; the flag keeps the difference visible.

Decision:

- The mark is capped at the database's present: `statement_timestamp()`
  on PostgreSQL, the host's clock on SQLite (which lives in the sealer's
  process). Seals are dated by the same clock. Under `freeze_time` the
  frozen clock decides, as for the outbox (ADR 0043).
- `seal(late=True)`, `seal_all(late=True)`, and `auditseal --late` seal
  the unsealed rows below the window after the usual pass, oldest first,
  at most `SEAL_BATCHES` batches more, into the same chain. Their seals
  have `late` set (a new column, so projects run `makemigrations` and
  `migrate`). The worker never runs this pass.
- The mark is the newest seal not flagged late, so sealing a row from
  last week never pulls the window back to last week.
- Digests, links, anchors, and `verify` are unchanged: late seals are
  checked like every other. `verify` counts them (`Report.late`), and
  `auditverify` prints the count; they do not fail it.

Consequences:

- A sealer's clock no longer decides what other hosts' rows are in reach
  on PostgreSQL. On SQLite, which runs in one process, nothing changes.
- Every row can be sealed. A row sealed late was open to change for
  longer, and its seal says so: look into why it was late (a long
  transaction, a host whose clock runs behind) before sealing it.
- `auditseal --late` reads the whole trail; run it when `auditverify`
  reports unsealed rows, not on every schedule.

# 0069. Namespace audit anchors, and check the tamper-evident baseline where deployed

- Status: Accepted; amended 2026-10-25 (at the end)
- Date: 2026-10-25

## Context

The audit trail is tamper-evident only when four things hold: its hash
chain's head is anchored on a ledger (`AUDIT.LEDGER`), the anchors of one
project do not mix with another's, something seals the trail on a
schedule, and the `protect()` triggers refuse updates and deletes. Up to
0.9.0 all four were left to the operator, and nothing said when one was
missing (re-audit findings N5 and N11, audit 0.9.0 DATA-03 and DATA-04).

Anchors went under `domainkit/audit/<alias>`. Every project calls its
main database `default`, and the evidence store invites projects and
environments to share one immudb (`EVIDENCE.NAMESPACE`), so two of them
on one ledger wrote the same key: one's anchor held the other's back for
`ANCHOR_EVERY`, and read as a rewritten anchor in the other's
`auditverify`. Operators who see false alarms learn to ignore them.

A ledger key is kept for good, so its format is costly to change twice.
The options for the key were: a namespace that defaults to the project's
name (every existing deployment would move to a new key on upgrade, and
two projects that kept the default `PROJECT_NAME` would still collide); a
namespace the project sets, `None` keeping the old key; a namespace
required at once (a breaking change in a patch release).

For the baseline, refusing to start without it would break deployments
that run today, and a check of the triggers needs the database, which
`check` does not otherwise reach and may not reach in CI.

## Decision

- `AUDIT.NAMESPACE` (a lower-case name of up to 32 characters, as
  `EVIDENCE.NAMESPACE`) puts anchors under
  `domainkit/audit/<namespace>/<alias>`. `None`, the default in 0.9.1,
  keeps the old key.
- Continuity: with a namespace, `verify` also reads the heads anchored
  under the old key (`AUDIT.PREVIOUS_ANCHORS`, on by default), so a trail
  verified before the change verifies after it, and a rewrite of a head
  anchored before is still found. A number anchored with two links (under
  two keys) counts as rewritten. Nothing is written to the old key again:
  a marker there would break the `verify` of projects still using it.
  Projects whose old key also holds others' anchors turn
  `PREVIOUS_ANCHORS` off once their namespace holds an anchor.
- In staging and production, `check` warns when `AUDIT.LEDGER` is unset,
  when it is set without a namespace, and when nothing seals on a schedule
  (no messaging, and no `AUDIT.SEALED_EXTERNALLY` saying cron runs
  `auditseal`). When the application starts there, it asks each audited
  PostgreSQL database for the four `protect()` triggers, enabled
  (`pg_trigger`, `tgenabled <> 'D'`, tables found through `search_path`),
  within 10 seconds, and logs what is missing, or that a database did not
  answer, naming the error's class only.
- `AUDIT.REQUIRE_BASELINE = True` refuses to start instead, listing every
  missing part. 0.10 makes it the default and requires a namespace with a
  ledger.

## Consequences

- Projects and environments can share a ledger without false alarms once
  each sets its namespace; a deployment that sets none keeps 0.9.0's
  behaviour, with a warning.
- A missing ledger, schedule, namespace, or trigger shows in `check` and
  in the startup log, before an incident needs the trail.
- `verify` reads one more key while `PREVIOUS_ANCHORS` is on: one more
  ledger read per anchor written before the change.
- Startup makes one query per audited PostgreSQL database in staging and
  production. Disabling a trigger, or protecting only the trail and not
  its seals, now shows as a warning; a deployment that relied on SQLite
  there gets no trigger warning, since `protect()` does nothing on it.
- The trigger check runs at application startup, not in the worker or in
  `check`; `auditverify` remains the check of what the triggers protect.

## Amendment (2026-10-25): what counts as a trigger

The probe first counted a trigger by its name and `tgenabled <> 'D'`. A
security review of 0.9.1 (SEC-r2-3) showed two ways to keep the names and
lose the protection: `ENABLE REPLICA TRIGGER` (it then fires only in
replica sessions, never the application's), and `CREATE OR REPLACE` of the
function with a body that lets everything through. Re-creating a trigger
on fewer events, with a `WHEN` condition, or for some columns only, does
the same.

A trigger now counts only as `protect()` makes it: `tgenabled` `O` or `A`;
`tgtype` that of `BEFORE UPDATE OR DELETE ... FOR EACH ROW` (27) or
`BEFORE TRUNCATE ... FOR EACH STATEMENT` (34); no `WHEN` (`tgqual`), no
columns (`tgattr`), no arguments; and its function (`tgfoid`) the plpgsql
one named `<table>_append_only` whose source (`pg_proc.prosrc`) equals the
body `protect()` writes, whitespace collapsed (`protection.function_body`).
That body has not changed since 0.1.0, so a database protected by any
release passes. A project that wrote its own equivalent SQL with another
message is reported, and calls `protect()` instead. The probe runs in the
query it ran before, so startup makes no more round trips.

# 0121. Hold guarded commands as sealed requests, and count approvals under the request's lock

- Status: Accepted; amended 2026-10-31 (at the end)
- Date: 2026-10-31

## Context

The fintech audit's FIN-36: domainkit had no maker-checker. A payment
over a limit, a journal entry over a threshold, a user made superuser:
each runs as soon as one person with the permission asks. Regulated
products need a second person (or two) to approve such a command, never
the person who asked, each once, within a time, and an audit trail of who
did what. The questions were how a use case says it needs approval, what
a pending command is and how it stays what was approved, how approvals
are counted when several come at once, how the approved command runs
exactly once, and how a project asks approvers for more proof (step-up).

**Guarding a use case.** Three places could refuse a direct call: the
HTTP route (a dependency), a registry the unit of work consults, or the
service itself. Only the last catches every caller: a route guard misses
code and messages (`defer_entry` posts without a route), and the unit of
work cannot tell one use case from another. A decorator on the service
(`requires_approval(action)`) checks every call; the policies are
registered in `ready()` (`guard()`, a `HookPoint` like every other hook,
ADR 0101), so they follow the installed domains and settings. A guard
refuses a service that is not decorated, and a decorated service with no
guard refuses every call: a forgotten registration fails closed. Apps
whose services approvals does not own are guarded through a hook point
of theirs: the ledger offers `on_posting`, called before `post_entry`
takes a lock, and approvals registers its check there; the auth admin
API asks `refuse_if_guarded` before it makes a superuser.

**The request.** A pending command is stored as its arguments read back
as JSON through pydantic (`TypeAdapter` of the command), never the JSON
as sent, with its fingerprint: SHA-256 of the action and arguments as
canonical JSON, which a client can compute from what it shows. An
approval states the fingerprint its approver saw. Nothing in the
application changes a request's arguments, but the database can: a row
changed by someone with access to it, or a decision row added. So the
request carries a seal, an HMAC under a key derived from `SECRET_KEY`
(`signing.Signer`, its own salt), of its id, action, fingerprint, maker,
and approvals needed, and each decision one of its request, person,
verdict, and fingerprint. Before the command runs, the worker checks the
seals, that the fingerprint is the arguments', that enough distinct
people other than the maker approved it, and that the arguments still
make the command and read back the same (a deploy may have changed the
command). Any failure fails the request; nothing runs.

**Counting.** A proof of concept on PostgreSQL 16, asyncpg, `READ
COMMITTED`: one request needing 10 approvals, 20 approvers approving at
once, each approval one transaction run once (no retry), three runs each,
and seven more for the two locks:

| How an approval reads the request | Decisions stored | Commands queued | Outcome | Time for all 20 |
| --- | --- | --- | --- | --- |
| No lock, the request's `version` only, approvals counted from the decisions | 20 | 0 | every approval answered "pending": adding a decision did not touch the request's row, so each saw no other, the count never reached 10, and 20 were stored for 10 needed | 109 to 122 ms |
| No lock, with the count stored on the request (its row, and `version`, change with each approval) | 1 | 0 | 19 of 20 refused by the version check (`StaleDataError`, raised during staging): nothing over-counted, but nearly every approval refused | 89 to 179 ms |
| `SELECT ... FOR UPDATE` of the request | 10 | 1 | 10 counted, the tenth approved it, 10 refused as no longer pending | 143 to 378 ms (median of the last seven 288) |
| A named lock of the request (`uow.lock`), then a read | 10 | 1 | the same | 173 to 401 ms (median of the last seven 282) |

The first row is the failure that matters: optimistic locking alone does
not protect a count kept in child rows. The two locks are equally right
and equally fast (the spread is the run's noise); the named lock costs
one statement more on PostgreSQL, but it is the one that also holds on
SQLite, where `FOR UPDATE` is a plain read and `lock()` takes the
database's write lock (`BEGIN IMMEDIATE`). So every change to a request
takes `uow.lock("approvals.request:<id>")` first, and the request keeps
its approvals count in its own row, so its `version` (and its `ETag`)
changes with every decision and refuses a change that went around the
lock. The unique `(request_id, approver)` constraint holds a person to
one decision even then. The proof is
`test_twenty_approvals_at_once_count_to_ten_and_run_once`, on both
databases.

**Running once.** The approval that completes a request queues the
command in its own transaction (`uow.defer`, ADR 0078): one transaction
moves the request to `approved`, under the lock, so one message is
queued. The task runs under the inbox (ADR 0094), so a message delivered
twice commits the command once. Marking the request `executed` after the
use case's commit would leave a window: the use case committed (with the
inbox row), the worker dies, and the message run again is skipped by the
inbox, leaving the request `approved` for good. So where the use case's
unit of work is on the requests' database, its first commit also locks
the request, marks it `executed`, and records the event: the change, the
status, its audit row, and the inbox row commit together. The unit of
work is wrapped (its repositories are the same; one more repository
joins its session) rather than changed. On another database the request
is marked after the use case, and a message run again after that commit
marks it when the inbox refuses the use case's second commit
(`AlreadyHandled`).

**Step-up.** Who may approve is an access rule, so any rule fits, a
step-up rule included: the policy's `approve_rule`, else
`APPROVALS.APPROVE_RULE` (an import path, checked at `setup()`). A
route's rules are asked about the request object (`has_object_permission`)
so the rule can depend on the policy. Rejecting does not ask for it.

## Decision

- `domainkit.contrib.approvals`, requiring messaging and audit. A use
  case is guarded by `@requires_approval(action)` on its service and
  `guard(action, service, unit_of_work=..., policies=[...])` in `ready()`.
  A direct call with a command a policy applies to raises
  `ApprovalRequired` (or submits it, with `submit=True`, and raises
  `ApprovalPending`); only the run of the approved request passes.
- `Policy(name, approvals=1..10, permission= | group=, expires_after=,
  approve_rule=, when=)`; `Threshold` for amounts per currency. The maker
  never decides; one decision per person, unique in the database.
- Requests store the command's arguments read back, a fingerprint, and
  seals; the worker checks all of it before the command runs.
- Every change takes the request's named lock; the approvals count is a
  column of the request.
- The completing approval defers the command; the task runs under the
  inbox; on the requests' database the request is marked in the use
  case's first commit.
- Postings over `APPROVALS.ENTRY_THRESHOLDS` are guarded through the
  ledger's `on_posting` hook; making superusers, when
  `SUPERUSER_APPROVALS` asks, through the auth admin API.

## Consequences

- A sensitive use case is guarded with a decorator and a registration,
  and its callers (routes, code, messages) all fail closed.
- Approvals for one request serialize; requests do not wait for each
  other. Twenty approvers at once cost about a fifth of a second in all.
- Rotating `SECRET_KEY` needs the old key in `SECRET_KEY_FALLBACKS` while
  requests sealed under it may still run; without it they fail, rather
  than run unverified.
- A guarded use case should commit once: one that commits twice is
  marked executed with its first commit.
- The approved command's own events are audited with the actor whose
  approval queued it (work a request queues keeps its actor); the
  request's rows name the maker and every approver.
- Found on the way, for the framework: a version race found while
  staging a commit surfaces as SQLAlchemy's `StaleDataError`, not
  `ConcurrentUpdate`, so `Retry(retry_on=(ConcurrentUpdate,))` does not
  retry it.

## Amendment (2026-10-31): the security review (SEC-15b)

The review of the merged 0.15 branch found four ways around these
decisions; each is closed where it happened.

- **Seal the whole state, and spend a token to run.** The seal left out
  the status, so a database writer without the key set an executed
  request back to `approved`, queued its message again, and ran it twice
  (SEC-15b-4). The seal now covers the policy, the approvers, the expiry,
  the status, and a `run_token` made when the request is approved; it is
  made again with each change of state, and checked before each. The
  message carries the token and the run spends it. What seals cannot do
  is tell a whole row restored from an earlier copy (seal and token
  included) from the original: that is the database's own protection,
  and the append-only audit trail keeps the run's row.
- **Check the booked entry, not only the command.** A capture booked an
  entry over the thresholds with no command to check (SEC-15b-2), and
  lines given as a generator were spent before the hook read them
  (SEC-15b-6). Approvals also checks every entry posted or captured as
  it is booked (the ledger's `on_booking`), from its postings, and lets
  through only the entry of the approved request being run; captures
  are a guarded action of their own, and `PostEntry` keeps its lines as
  a tuple. Reversals are left to limits: each moves back what an entry
  moved.
- **Who may make a command, and who it is about.** Making superusers
  took any holder of `auth.change_user` as maker and approver
  (SEC-15b-1). A guard names a `maker_rule` (a superuser) checked at
  submission, its service checks the maker again when it runs, a policy
  may name approvers by an access rule (`approver_rule`, superusers by
  default), and a guard's `parties` (the user promoted) never decide.
- **Run as the maker; fail closed without a policy.** The run acted as
  the last approver, so actor limits charged the approver; it acts as
  the maker now, and `RequestExecuted` names the approvers. A request
  whose guard or policy no longer exists is neither decided nor run
  (`policy_not_found`), where it fell back to `APPROVE_RULE`.

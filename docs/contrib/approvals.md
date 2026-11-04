# Approvals: maker-checker for sensitive commands

Some commands should run only after other people agree: a large payment,
a journal entry over a limit, a user made superuser. The approvals app
holds such a command as a pending **request** until enough people
approve it. The person who submits the command is the **maker**. A
person who approves it is a **checker**, and the maker is never one of
them. The request keeps exactly the arguments it was submitted with, and
their fingerprint. Once enough checkers approve that fingerprint, the
command runs once, through the [outbox](messaging.md), after the worker
checks that it is still what was approved.

On this page you learn how to:

- guard a [use case](../learning.md#words) so that it needs approval;
- write policies: who approves, how many, and when;
- submit, approve, reject, and cancel requests, in code and over HTTP;
- guard large ledger postings and the making of superusers;
- ask approvers for a step-up;
- know what maker-checker does not stop.

Every step is a domain event, recorded in the [audit trail](audit.md)
with its actor. The decisions are in
[ADR 0121](../adr/0121-hold-guarded-commands-as-sealed-requests-and-count-approvals-under-the-requests-lock.md).

## Install

Approvals need messaging (approved commands run in the worker) and audit
(every decision is audited):

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.audit",
    "domainkit.contrib.approvals",
]
```

```bash
python manage.py makemigrations -m "approvals"
python manage.py migrate
```

The app brings two tables, `domainkit_approval_requests` and
`domainkit_approval_decisions`, in the database that
`DATABASE_ROUTING['approvals']` names, or `default`.

## A first example

A refund of more than 1,000 dollars needs two approvals from people who
hold `payments.approve_refund`. In code, a maker submits the refund, two
checkers approve it, and the worker runs it:

```python
from domainkit.contrib.approvals import (
    ApprovalsUnitOfWork,
    ApproveRequest,
    approve_request,
    submit_command,
)

request = await submit_command("payments.refund", refund_command, maker=str(ann.id))
for checker in (bob, carol):
    request = await approve_request(
        ApproveRequest(
            request_id=request.id,
            approver=str(checker.id),
            fingerprint=request.fingerprint,
        ),
        ApprovalsUnitOfWork(),
    )
print(request.status)
```

```text
approved
```

The second approval completes the request and queues the refund in the
same transaction. The worker then runs `refund` once, as Ann. The
request's status becomes `executed`. The next section shows how the
refund is guarded.

## Guarding a use case

You guard a use case in two steps. First, decorate its service. Then a
direct call is refused whenever its command needs approval:

```python
from domainkit.contrib.approvals import requires_approval


@requires_approval("payments.refund")
async def refund(command: Refund, uow: PaymentsUnitOfWork) -> Payment: ...
```

Second, the domain registers the guard in `ready()`, with the policies
that decide who approves the command and when. This is the same place
where every hook is registered
([ADR 0101](../adr/0101-register-every-domain-hook-and-check-it-when-it-registers.md)):

```python
from domainkit.apps import DomainConfig
from domainkit.contrib.approvals import Policy, guard
from domainkit.money import Money


class PaymentsConfig(DomainConfig):
    requires = ("domainkit.contrib.approvals",)

    def ready(self) -> None:
        guard(
            "payments.refund",
            refund,
            unit_of_work=PaymentsUnitOfWork,
            policies=[
                Policy(
                    "payments.large_refund",
                    approvals=2,
                    permission="payments.approve_refund",
                    when=lambda command: command.amount > Money.of("1000", "USD"),
                ),
            ],
            permission="payments.add_refund",
        )
```

`guard()` returns a `Guard`; `guards()` returns every registered guard,
by action. Its arguments:

| Argument | What it does |
| --- | --- |
| `action` | The guard's name: dotted lower-case words, at most 128 characters. |
| `service` | The decorated service, `service(command, uow)`. |
| `unit_of_work` | What makes the unit of work the service runs in. |
| `policies` | One or more `Policy`. The first whose `when` holds applies. A command that none applies to needs no approval and runs at once. |
| `command` | The command's type. By default, the type of the service's first argument: a `Command` that pydantic can read and write as JSON (dataclasses, `UUID`, `date`, `Money`, enums, and the like). |
| `permission` | What a maker must hold to submit the command over HTTP: the permission that the direct route would ask for. |
| `submit` | `True`: a direct call submits the command for whoever is acting, then raises `ApprovalPending`. |
| `maker_rule` | An access rule that a maker must pass to submit over HTTP, besides `permission` (`IsSuperuser()` for making superusers). It is asked again when the approved command runs. |
| `parties` | A function that names, for a command, the people it is about (the payee of a payment, the user a grant promotes). They may not approve or reject it (`interested_party`, 403), and their approvals never count. |
| `domain` | The installed domain that the guard belongs to, when you call `guard()` outside `ready()`. |

### What a direct call does

Called directly with a command that needs approval, the service raises
`ApprovalRequired` (403, `approval_required`) and runs nothing: it fails
closed. With `submit=True`, it submits the command and raises
`ApprovalPending` (403, `approval_pending`), whose `request_id` names the
request. A route may answer that with a 202.

The guard checks itself both ways. A decorated service with no guard
registered refuses every call (`ImproperlyConfigured`). `guard()`
refuses a service that is not decorated: without the decorator, a
direct call would skip approval.

Only the run of an approved request passes the guard, and only with the
command that was approved. A guarded service called from inside that run
with another command is refused like any other call. `current_run()`
returns the approved command being run here, if any, and
`approved(action, command)` says whether `command` is it.

`maker_rule` is asked again when the approved command runs, on the maker
as a user, with no credentials. So a rule about the submitting request's
own scopes or step-up passes at submission only, and refuses at the run.

### Policies

```python
from datetime import timedelta

from domainkit.contrib.approvals import Policy
from domainkit.contrib.auth import StepUp

Policy(
    "treasury.payout",
    approvals=2,  # 1 to 10
    group="treasurers",  # or permission="treasury.approve_payout"
    expires_after=timedelta(hours=4),  # default APPROVALS.EXPIRES_AFTER
    approve_rule=StepUp(max_age=timedelta(minutes=5)),  # see below
    when=lambda command: command.amount.amount > 10_000,
)
```

**Approvers** are the holders of a `permission`, the members of a
`group`, or whoever an access rule allows (`approver_rule=IsSuperuser()`):
one of the three. A group needs a principal with `groups` (as the users
of `domainkit.contrib.auth` have) and credentials not limited to scopes.
A permission is checked as `HasPermissions` checks it, scopes included.
A policy that cannot be met is refused when it is made:

```text
ImproperlyConfigured: Policy 'payments.large_refund' names its approvers by a permission, a group, or an access rule: one of the three.
ImproperlyConfigured: Policy 'payments.large_refund' asks for 1 to 10 approvals, not 11.
```

`expires_after` is more than nothing and at most 90 days.

**The maker never approves**, even when they hold the permission or
belong to the group. They never reject their own request either: they
cancel it.

**One person, once.** A decision is stored per request and person (the
principal's id), unique in the database. The same person through another
session, device, or token is the same person, refused with
`already_decided` (409), even from two requests at the same moment.

A request needs `approvals` approvals, and one rejection ends it.

**A policy that is gone stays gone.** A request is decided and run only
under the guard and the policy it was submitted under. Once a deploy
renames or removes either, its pending requests are neither approved nor
rejected (`policy_removed`, 409), never under `APPROVE_RULE` or no rule
instead, and an approved one fails with that code. The maker cancels it,
or it expires.

### Thresholds

`when` reads the command, so a policy may apply to some commands only.
`Threshold` reads amounts per currency. `exceeded_by(amounts)` adds them
up per currency, and says whether any sum is more than its currency's
amount:

```python
from domainkit.contrib.approvals import Threshold
from domainkit.money import Money

large = Threshold({"USD": "10000", "EGP": "500000"})
print(large.exceeded_by([Money.of("6000", "USD"), Money.of("5000", "USD")]))
print(large.exceeded_by([Money.of("9000", "USD"), Money.of("400000", "EGP")]))
print(large.exceeded_by([Money.of("1", "EUR")]))
```

```text
True
False
True
```

A currency without an amount is always over: the control fails closed
for the currencies it does not name (FIN-89). `"*"` gives an amount for
every other currency, read in each one's own units (`{"USD": "10000",
"*": "1000"}`). An amount that is not exact in its currency, or less than
zero, raises `ImproperlyConfigured`.

## Requests

An `ApprovalRequest` is an aggregate (its id is a `RequestId`). It is in
one `RequestStatus`:

| Status | Meaning |
| --- | --- |
| `pending` | Waiting for approvals. |
| `approved` | Approved; its command is queued to run. |
| `executed` | Its command ran. |
| `failed` | Approved, but its command was refused when it ran; `reason` says why. |
| `rejected` | One approver rejected it. |
| `cancelled` | The maker withdrew it. |
| `expired` | Its time ran out. |

Each person's `Decision` holds a `Verdict`: `approve` or `reject`.

Six services change requests. Each takes a command and an
`ApprovalsUnitOfWork`, except `submit_command`:

| Service | Command | What it does |
| --- | --- | --- |
| `submit_command(action, command, maker=...)` | the guard's own | Submits a command object. |
| `submit_request` | `SubmitRequest(action, arguments, maker)` | Submits a command as JSON arguments, as the HTTP route does. |
| `approve_request` | `ApproveRequest(request_id, approver, fingerprint)` | Approves; the approval that completes the request queues its command. |
| `reject_request` | `RejectRequest(request_id, approver, reason)` | Rejects, with a reason of 1 to 500 characters, kept on the request. |
| `cancel_request` | `CancelRequest(request_id, maker, reason="")` | Withdraws a pending request, as its maker. |
| `expire_requests()` | none | Expires the pending requests whose time is up; returns how many. |

`execute_request(request_id, token)` is the task that runs an approved
command. The worker calls it; you do not.

A command that needs no approval is refused by `submit_command` with
`ApprovalNotNeeded` (422, `approval_not_needed`). An action that no guard
has is `UnknownAction` (404).

**What is stored.** The command read back as JSON (`arguments`), never
the JSON as sent: defaults filled in, amounts at their currency's
places. Its `fingerprint` is the SHA-256 of `{"action": ...,
"arguments": ...}` as canonical JSON (keys sorted, no spaces, UTF-8). So
a client can work it out from what it shows. `fingerprint(action,
arguments)` computes it:

```python
from domainkit.contrib.approvals import fingerprint

fingerprint("payments.refund", {"amount": {"amount": "1500.00", "currency": "USD"}, "payment_id": "7f0c"})
```

```text
ff4ecea99aebac4ab769d04b9bb58e2058a06150e6dc128e91d7690e32b672b9
```

The stored JSON is at most `MAX_ARGUMENTS_BYTES`; larger, or not a valid
command, is `InvalidArguments` (422, `invalid_arguments`).

**An approval states the fingerprint** the approver was shown. A request
that holds anything else is refused (`fingerprint_mismatch`, 409).
Nothing changes a request's command after it is submitted: no route or
service does.

**Expiry.** A request waits until `expires_at` (its policy's
`expires_after`, or `EXPIRES_AFTER`). After that, approvals are refused
(`request_not_pending`, 409), and the worker marks it expired every
`EXPIRE_EVERY`. `manage.py approvals expire` does it at once.

### Counting approvals once

The approval that completes a request queues its command through the
outbox, in the same transaction ([messaging](messaging.md)). So it is
queued exactly when the request is approved, and once. Each approval
takes the request's lock first (`uow.lock`: an advisory lock on
PostgreSQL, the write lock on SQLite). So approvals given at the same
moment are counted one after another, and exactly one of them completes
the request. The request's `version` changes with each approval, so a
change that went around the lock is refused too.

### How the approved command runs

The worker runs it under the inbox (ADR 0094), once however often the
message is delivered, after it checks that the command is what was
approved:

- **The request's seal**, and each decision's: an HMAC, under a key
  derived from `SECRET_KEY`, of the request's id, action, fingerprint,
  maker, policy, approvals needed, approvers (permission or group),
  expiry, status, and run token. It is made again with each change of the
  request's state. Each decision's seal covers its request, person,
  verdict, and fingerprint. A row changed or added in the database by
  someone without the key is neither decided (`request_tampered`, 409)
  nor run (it fails with that code).
- **The run token**: a one-time key made when the request was approved,
  sealed with it, carried by the message, and spent by the run. A message
  queued again after the run, or one without the token, runs nothing. So
  does a status set back to `approved`, whose seal no longer holds.
- **The fingerprint** is that of the arguments, and enough people other
  than the maker and the command's parties approved it, each once.
- **The arguments** still make the command (the guard's command may have
  changed in a deploy), and read back the same.
- **The maker** may still make the command (below).

A request that fails a check is `failed`, with the refusal's code as its
`reason` (`request_tampered`, `fingerprint_mismatch`,
`invalid_arguments`, `policy_removed`, `maker_not_allowed`), and runs
nothing. So does a request whose use case refuses it (a `DomainError`,
such as `insufficient_funds`): the maker may submit it again. A use case
that lost a race (`ConcurrentUpdate`) or failed in another way is tried
again by the worker, with its backoff.

Where the use case's unit of work commits to the requests' database,
the request is marked `executed` in that same transaction, with its
first commit. The command's change, the request's status, its audit
row, and the inbox row commit together, or none does. A use case on
another database commits first; the request is marked after it, and a
message run again after that commit marks it then (the inbox refuses the
use case's second commit). Keep a guarded use case to one commit: one
that commits twice is marked with the first.

The command **runs as its maker**. The request context's actor is the
maker's id while it runs, so its own events, and whatever is charged to
the actor (the actor limits of [limits](limits.md)), are the maker's.
`RequestExecuted` lists the approvers, and their `approval_given` rows
name each of them.

`SECRET_KEY_FALLBACKS` verify seals as they verify signatures. Keep an
old key there while requests sealed under it may still run.

!!! warning
    Seals find a row changed or forged by someone without the key. They
    cannot tell a whole row put back as it was earlier (a restored
    backup, an earlier copy of the row with its seal, its token, and its
    outbox message) from the original. Keep the database's writers to the
    application's role, and the audit trail append-only and sealed
    ([audit](audit.md#tamper-evidence)): its
    `approvals.request_executed` row stays, whatever a row of requests
    says.

### What maker-checker does not stop

- **Accounts.** One person is one principal id. A person who may create
  users and give them the approvers' permission (or put them in the
  group) can make a second account and approve their own requests with
  it. Give approver rights only to people who cannot create users or
  change grants. Watch the audit trail for both (`auth.user_created`,
  `auth.user_permission_granted`, `auth.user_joined_group`).
- **Impersonation.** Staff acting as a user can neither submit, approve,
  reject, nor cancel. Requests are made and decided in one's own name
  only: a call whose actor is not its principal gets 403 "Requests are
  made and decided in one's own name only." Without this rule, one staff
  member could submit as one user and approve as two others.
- **Splitting.** A threshold judges one command. Ten postings of 999
  dollars under a 1,000-dollar threshold need no approval. Limit what an
  actor may move in a window with [limits](limits.md), which counts every
  entry booked against the subjects a window bounds; approved entries
  count against their maker. A currency that no threshold names needs
  approval at any amount.
- **A maker who leaves.** An approved command runs only while its maker,
  a user, is active, holds the guard's maker permission, and passes its
  `maker_rule` (SEC-16b-7). Otherwise the request fails
  (`maker_not_allowed`, FIN-93). Deactivate a person, or take the
  permission back, and what they left waiting runs nothing.

## Large postings

With `domainkit.contrib.accounting` installed and `ENTRY_THRESHOLDS`
set, the ledger's own commands need approval over a threshold:

```python
APPROVALS = {
    "ENTRY_THRESHOLDS": {"USD": "10000.00", "EGP": "500000.00"},
    "ENTRY_APPROVALS": 2,
    "ENTRY_APPROVERS": "accounting.add_entry",
}
```

An entry that moves more than its currency's amount (the sum of its
debits in that currency) needs `ENTRY_APPROVALS` approvals by holders of
`ENTRY_APPROVERS`: by default, other clerks who may post entries
themselves. `post_entry` refuses it otherwise (`approval_required`),
whether it is called over HTTP, from code, or from a message
(`defer_entry` dead-letters it).

| Action (constant) | Policy | Body to submit | Makers need |
| --- | --- | --- | --- |
| `accounting.post_entry` (`POST_ENTRY`) | `accounting.large_entry` | What `POST /entries` takes. | `accounting.add_entry` |
| `accounting.capture_hold` (`CAPTURE_HOLD`) | `accounting.large_capture` | `{"hold_id": ..., "counter_account_id": ..., "amount": ...}` | `accounting.capture_hold` |
| `accounting.reverse_entry` (`REVERSE_ENTRY`) | `accounting.large_reversal` | `{"entry_id": ..., "booked_on": ...}` | `accounting.reverse_entry` |

A hold reserves any amount, unapproved; its capture is what moves the
money, into an account that the capture names. So a capture over the
threshold needs approval. A reversal moves as much as the entry it
undoes, back. So undoing a payout that two people approved takes two
people too. Run approved, a reversal counts against limits as a movement
and frees nothing ([limits](limits.md#on-the-ledger)).

Each is checked twice: before `post_entry` takes a lock
(`on_post_entry`), and as every entry is booked (`on_stage_entry`), from
the entry itself. So a path around the command (a capture, lines a hook
could not read) books nothing over the threshold unapproved. A booked
entry passes only as the approved request being run: the posting with
its source and reference, or the capture of its hold. `PostEntry` keeps
its `lines` as a tuple, however they are given. A currency without an
amount needs approval, whatever the amount, unless `"*"` gives one for
every currency. The ledger's own rules do not change.

The run of any approved request whose policy asked at least
`ENTRY_APPROVALS` approvals passes the thresholds for the entries its
command books. So your own guarded payout posts its entry without a
second request. A run of the ledger's own guards books only what it
approved. A policy of fewer approvals still has its large entries
refused.

## Making superusers

With `SUPERUSER_APPROVALS` above 0 and `domainkit.contrib.auth`
installed, the auth admin API refuses to make a user a superuser, on
create or on change (`approval_required`). Taking superuser away is not
guarded. A superuser submits `auth.grant_superuser` (`GRANT_SUPERUSER`)
with `{"user_id": ...}` (`GrantSuperuser`) instead, under the policy
`auth.superuser`. Only a superuser may
submit one, as only a superuser makes one without approvals.

`SUPERUSER_APPROVALS` other superusers approve it (or, with
`SUPERUSER_APPROVERS` set, holders of that permission), never the user
it promotes. The run checks that its maker is still an active
superuser, and fails it otherwise (`maker_not_allowed`). `manage.py
createsuperuser` stays the operator's way to make the first one.

`refuse_if_guarded(action)` raises `ApprovalRequired` when a guard is
registered for `action`. Use it on a path that does a guarded action
without its service, as the auth admin API does.

## Who may approve: step-up

Approving can ask for more than being an approver: a recent second
factor, for example. The rule is an access rule of any kind
([access](../auth/routes.md)): a policy's `approve_rule`, or else
`APPROVALS.APPROVE_RULE`. The setting is the import path of a rule
instance, or of a rule class that takes no arguments. The rule is asked
about the request and the request's object:

```python
# config/rules.py: contrib.auth's step-up rule, or a rule of your own.
from datetime import timedelta

from domainkit.contrib.auth import StepUp

RECENT_PROOF = StepUp(max_age=timedelta(minutes=5))

# config/settings.py
APPROVALS = {"APPROVE_RULE": "config.rules.RECENT_PROOF"}
```

Or on one policy, which then asks it in place of `APPROVE_RULE`:

```python
from domainkit.contrib.approvals import Policy
from domainkit.contrib.auth import StepUp

Policy(
    "treasury.payout",
    group="treasurers",
    approve_rule=StepUp(max_age=timedelta(minutes=5)),
)
```

An approver who has not stepped up within 5 minutes gets `401
step_up_required` from `POST /requests/{id}/approve`. They step up
(`POST /step-up` with a code of their authenticator,
[Stepping up](../auth/step-up.md)) and approve again. Mount
`domainkit.contrib.auth.stepup.router` (and the router of `auth.mfa`, to
enrol) where the API keeps its auth routes.

Any `BasePermission` fits: the step-up rule of `contrib.auth`
(`StepUp(max_age=...)`), your own, or a combination
(`StepUp(max_age=...) & IsUnscoped()`). A policy may ask for a fresher
proof than the rest. A refused approval answers 403 ("You cannot approve
this request now."), unless the rule names its own refusal, as `StepUp`
does (`401 step_up_required`). Rejecting asks only that the person is an
approver: stopping a request should never wait for a second factor.

## Over HTTP

```python
from domainkit.contrib.approvals.api import router as approvals

router.include_router(approvals, prefix="/api/approvals")
```

| Route | Who | What it does |
| --- | --- | --- |
| `GET /requests`, `/requests/{id}` | `approvals.view_approvalrequest` | Requests (`?status=pending`, `action`, `policy`, `maker`), with their command, fingerprint, and decisions. |
| `POST /requests` | `approvals.add_approvalrequest`, and the guard's `permission` and `maker_rule` | Submit `{"action": ..., "arguments": {...}}`: 201, with `Location`. |
| `POST /requests/{id}/approve` | an approver of its policy, and its approve rule | Approve, stating `{"fingerprint": ...}`. |
| `POST /requests/{id}/reject` | an approver of its policy | Reject, with `{"reason": ...}`. |
| `POST /requests/{id}/cancel` | `approvals.add_approvalrequest`, and being its maker | Withdraw it (`{"reason": ...}`, optional). |

Submitting, approving, rejecting, and cancelling also refuse a caller
who acts for someone else (impersonation, above). The maker and the
approvers are the principal's id, whatever authenticates requests.
`domainkit.contrib.approvals.api` also exports `requests`, the resource
router that `router` mounts.

## From the command line

| Action | What it does |
| --- | --- |
| `approvals list [--status S] [--limit N]` | The requests in a status (pending by default), oldest first; `--limit` from 1 to 1,000 (default 50). |
| `approvals show ID` | One request, its command, and its decisions. |
| `approvals expire` | Expire the overdue requests; recorded as the operator's action `operator.approvals.expire`. |

`expire` takes `--operator` ([operators](../reference/commands.md#operators)).
What makers and approvers wrote is shown with control characters
escaped, so a reason cannot act on your terminal:

```text
6c0e...  tests.act  rejected
maker maker, policy tests.acts, needs 2
fingerprint 3f1b...
{"kind": "second"}
reject  ann  2026-10-30T09:12:44+00:00
reason: No.\u0007
```

## Audit and privacy

Every change is an event that the audit trail records with the actor
who made it ([audit](audit.md)):

| Event | Recorded as |
| --- | --- |
| `RequestSubmitted` | `approvals.request_submitted` |
| `ApprovalGiven` | `approvals.approval_given` |
| `RequestApproved` | `approvals.request_approved` |
| `RequestRejected` | `approvals.request_rejected` |
| `RequestCancelled` | `approvals.request_cancelled` |
| `RequestExpired` | `approvals.request_expired` |
| `RequestExecuted` | `approvals.request_executed` |
| `RequestFailed` | `approvals.request_failed` |

The approved command's own events, and `request_executed`, are recorded
with its maker as the actor. The approvers are the actors of
`approval_given`, and are listed in `request_executed`. Reasons are kept
on the request, not in the trail.

`privacy export` lists the requests a person made, with their commands,
and the decisions they took. `privacy erase` keeps the record of who
approved what, which a business must keep (a payment's authorization,
the evidence of who checked whom), but pseudonymized (FIN-94). The
person's id becomes one `erased:` id wherever they are maker or
approver, so a request still shows that its maker and approvers were
different people. The commands and the decisions stay as they were, and
the seals are made again for the pseudonym, so the record still verifies
(only seals that held: an erasure launders no row changed around the
application). Their pending requests are cancelled. An approved request
of theirs not yet run fails (`maker_not_allowed`) rather than runs. The
reasons they wrote, free text, are blanked.

This is pseudonymization, not anonymization. The pseudonym links back
to the person through what else is kept (the trail's actors, the
approvers in `request_executed`, the business's own records). A request
about the person rather than by them keeps their id in its command
(`parties`: the user that `grant_superuser` promotes, a payee). So the
record is still personal data, kept because the law asks for it.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `APPROVALS` key | Default | Meaning |
| --- | --- | --- |
| `EXPIRES_AFTER` | 3 days | How long a request waits for its approvals, unless its policy says (1 minute to 90 days). |
| `EXPIRE_EVERY` | 5 minutes | How often the worker expires the requests whose time is up (at most 1 day). |
| `APPROVE_RULE` | `None` | Import path of an access rule (an instance, or a class that takes no arguments) that whoever approves must pass as well, unless the policy names its own; a path that is not a rule fails `setup()`. |
| `MAX_ARGUMENTS_BYTES` | `65536` | The largest command a request holds, as JSON (1 KiB to 1 MiB). |
| `ENTRY_THRESHOLDS` | `{}` | Per currency, the amount (text) over which `accounting.post_entry`, `capture_hold`, and `reverse_entry` need approval; empty: they are not guarded. A currency it does not name always needs approval, unless `"*"` gives an amount for every one (read in each currency's units). An amount not exact in its currency fails `setup()`. |
| `ENTRY_APPROVALS` | `2` | Approvals a posting over its threshold needs (at most 10). |
| `ENTRY_APPROVERS` | `"accounting.add_entry"` | The permission whoever approves a posting holds. |
| `SUPERUSER_APPROVALS` | `0` | Approvals that making a superuser needs (at most 10); 0: it is not guarded. |
| `SUPERUSER_APPROVERS` | `None` | The permission whoever approves making a superuser holds; `None`: superusers approve, and only they. |

## On PostgreSQL and SQLite

Each approval takes the request's lock: an advisory lock on PostgreSQL,
the database's write lock on SQLite. The tests run the proofs on both,
and on PostgreSQL 20 approvals of one request really run at once:
exactly one completes it.

## Errors and refusals

| Error | When | HTTP |
| --- | --- | --- |
| `ApprovalRequired` | A direct call of a command that needs approval. | 403, `approval_required` |
| `ApprovalPending` | The same, with `submit=True`: the request was submitted. | 403, `approval_pending` |
| `MakerCannotDecide` | The maker tried to approve or reject. | 403, `maker_cannot_decide` |
| `InterestedParty` | A party to the command tried to decide it. | 403, `interested_party` |
| `NotTheMaker` | Someone else tried to cancel. | 403, `not_the_maker` |
| `MakerNotAllowed` | The maker may no longer make the command (at the run). | 403, `maker_not_allowed` |
| `AlreadyDecided` | This person already decided. | 409, `already_decided` |
| `FingerprintMismatch` | The request does not hold what the approver saw. | 409, `fingerprint_mismatch` |
| `RequestNotPending` | The request is no longer pending, or expired. | 409, `request_not_pending` |
| `RequestTampered` | A seal does not hold. | 409, `request_tampered` |
| `PolicyRemoved` | The guard or its policy is gone. | 409, `policy_removed` |
| `InvalidArguments` | The arguments do not make the command, are too large, or a reason is too long. | 422, `invalid_arguments` |
| `ApprovalNotNeeded` | No policy applies to the command. | 422, `approval_not_needed` |
| `UnknownAction` | No guard has that action. | 404, `unknown_action` |
| `RequestNotFound` | No request has that id. | 404, `approval_request_not_found` |

## Common mistakes

- **Decorating the service without registering the guard.** Every call
  is then refused. Call `guard()` in `ready()`.
- **Calling the undecorated function from a route.** It skips approval.
  Guard the service itself, and `guard()` refuses one that is not
  decorated.
- **Letting approvers create users.** One person can then approve with a
  second account. Keep the two rights apart.
- **A use case that commits twice.** The request is marked with the
  first commit. Keep it to one.
- **Removing `SECRET_KEY` while requests wait.** Their seals no longer
  hold. Keep the old key in `SECRET_KEY_FALLBACKS`.

## See also

- [Accounting](accounting.md): the ledger whose postings can be guarded.
- [Limits](limits.md): bound what moves in a window, against splitting.
- [Stepping up](../auth/step-up.md): a fresh second factor.
- [Audit](audit.md): the events of every request.
- [Privacy](../privacy.md): erasure and export.

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
- hold changes of the admin API for approval: making staff, groups,
  permissions, passwords, impersonation, a return, a group's permissions;
- ask approvers for a step-up, or to authorize each approval once;
- have makers and approvers authorize each submission, rejection, and
  cancellation once;
- take those steps from the project's own code, which has no phone to ask;
- choose who reads which requests, and tell the approvers of one;
- add a staff rule or a step-up to the routes of your choice;
- know what maker-checker does not stop.

Every step is a domain event, recorded in the [audit trail](audit.md)
with its actor. The decisions are in
[ADR 0121](../adr/0121-hold-guarded-commands-as-sealed-requests-and-count-approvals-under-the-requests-lock.md).

## When to use it, and when not to

- **Use it when** a command must wait for other people to agree: a large
  payment, a journal entry over a limit, a user made superuser.
- **Do not use it** for the user's own confirmation of an act: a
  [step-up](../auth/step-up.md) or an
  [action authorization](../auth/actions.md) is given by the user alone,
  at once. Do not use it alone against many small commands under a
  threshold: add [limits](limits.md) per window
  ([what maker-checker does not stop](#what-maker-checker-does-not-stop)).
- **An approved command runs later.** The maker's call gets the
  request, not the command's result
  ([what a direct call does](#what-a-direct-call-does)). By default the
  worker runs it; with `by_maker=True`, the maker's same call, made
  again once the request is approved, runs it and gets its result
  ([run by its maker](#run-by-its-maker)).

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

A `@use_case` is guarded the same way, with `@requires_approval` above
it: its call (`refund(uow, ...)`) and its `run`, which routes call, are
both refused, and `guard()` takes the use case, its command included:

```python
@requires_approval("payments.refund")
@use_case
async def refund(uow: PaymentsUnitOfWork, payment_id: UUID, amount: Money) -> None: ...
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
| `parties` | A function that names, for a command, the people it is about (the payee of a payment, the user a grant promotes). They may not approve or reject it (`interested_party`, 403), and their approvals never count. It may be `async def` and read the database (since 2.4): it runs at each decision and at the run. `Guard.aparties_of(command)` awaits it; `parties_of` takes a plain function only. |
| `maker_fields` | The command's fields that hold who acts (`invited_by`, `granted_by`). The submit route sets them to the maker's id, whatever the client sent; a command your code submits (`submit_command`, a direct call with `submit`) must name its maker there (`invalid_arguments` otherwise); and the run checks them again. A name the command has not is refused when the guard is made. |
| `http` | `True`: makers submit the command at `POST /requests`. `False` keeps it off that route (`404 unknown_action`): only your code submits it. |
| `by_maker` | `True`: the approved command runs in its maker's own call, never in the worker (since 2.4). See [Run by its maker](#run-by-its-maker). |
| `everywhere` | `True`: the command acts on rows that belong to no tenant (the ledger, users). Its makers and approvers are checked as where no tenant is served: a member's role makes neither ([rows of no tenant](../tenancy.md#rows-of-no-tenant)). The guards of postings, of making superusers, and of admin changes say so. |
| `show` | What a person deciding the command is shown of it, in place of its arguments by name (since 2.7): a function of the command that returns labelled values, each a text or a list of texts, in the request's language (`{"Pay": "2500.00 EUR to Ada Lovelace"}`). It may be `async def` and read the database (a user by name rather than by id); it runs each time a screen is built, and nothing of it is stored with the request. See [What the approver is shown](#approving-with-an-authorization). |
| `domain` | The installed domain that the guard belongs to, when you call `guard()` outside `ready()`. |

### What a direct call does

Called directly with a command that needs approval, the service raises
`ApprovalRequired` (403, `approval_required`) and runs nothing: it fails
closed. With `submit=True`, it submits the command and raises
`ApprovalPending` (403, `approval_pending`), whose `request_id` names the
request. A route may answer that with a 202. Over HTTP, the error's body
names it `approval_request_id` (since 2.4), beside the body's own
`request_id`, which is the HTTP request's, as in every error
([the fields of an error](../reference/responses.md#the-fields-of-an-error)):

```text
{"type":"about:blank","title":"Forbidden","status":403,"detail":"This command needs approval: it was submitted for approval.","instance":"/payouts","code":"approval_pending","request_id":"9d319f0bc63345f5fdcf152e7013bd03","approval_request_id":"0192f0aa-5b7e-7c3d-9a41-3f6b1e2d4c5a"}
```

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

### Run by its maker

Some commands give their maker something, such as a token, or take
something that must not be stored, such as a password. The worker cannot
hand a token to anyone, and a request keeps its command for good. With
`by_maker=True`, the maker runs the approved command with a call of their
own (since 2.4):

1. A direct call submits the command as the caller's, and raises
   `ApprovalPending` with the request's id. The same call made while the
   request waits raises it again, with the same id: nothing new is
   submitted.
2. The approvers approve it. Nothing is queued: the worker runs nothing.
3. The maker makes the same call again. The service runs as the approved
   request (`current_run()` names it), and the request is `executed`.
   An action authorization the first call carried and did not spend is
   kept for this one where `AUTH.AUTHORIZATIONS.HELD_TTL` is set, while
   the request waits (since 2.7,
   [Held for approval](../auth/actions.md#held-for-approval)).
4. A later call submits a new request.

The call finds the request by its maker and its fingerprint: the oldest
approved request of the caller's, of this action, that holds exactly this
command. Another command, or another person's call, submits a request of
its own. The request is checked as the worker checks a run (its seals,
its arguments, its approvals, its maker), and marked executed before the
service runs. So a call refused after that, by the service or the
database, needs a new approval: check what you can before the call. A
request that fails a check fails, with the refusal's code, and the call
submits a new one. An approved request that its maker does not run
before its `expires_at` is never run. Inside the run of another approved
command, a direct call is refused (`approval_required`), and nothing is
submitted.

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
| `reject_request` | `RejectRequest(request_id, approver, reason)` | Rejects, with a reason of at most 500 characters, kept on the request; the route asks for 1 at least. |
| `cancel_request` | `CancelRequest(request_id, maker, reason="")` | Withdraws a pending request, as its maker. |
| `expire_requests()` | none | Expires the pending requests whose time is up; returns how many. |

`execute_request(request_id, token)` is the task that runs an approved
command. The worker calls it; you do not.

With an act's setting on (`SUBMIT_ACTION`, `APPROVE_ACTION`,
`REJECT_ACTION`, `CANCEL_ACTION`), the service of that step asks for an
authorization, from code too. The project's own code calls the function
the service runs instead ([From the project's own code](#from-the-projects-own-code)).

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

fingerprint(
    "payments.refund",
    {"amount": {"amount": "1500.00", "currency": "USD"}, "payment_id": "7f0c"},
)
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

### Requests and tenants

With [multi-tenancy](../tenancy.md), a request belongs to the tenant it
was submitted in (`tenant_id`), or to none, where no tenant was served.
It is listed, read, approved, rejected, and cancelled where that tenant
is served alone:

- The requests' repository reads the tenant's requests alone, by id and
  by query. Another tenant's request is `404
  approval_request_not_found`, as one that does not exist, on every
  route, its twins included, and `RequestNotFound` from the services,
  whatever keys the caller holds there.
- A request of no tenant is read where no tenant is served. Requests
  stored before the tenant was recorded have none.
- Code that works across tenants says so: inside `all_tenants()` every
  request is read ([Outside a request](../tenancy.md#outside-a-request)).
  Approving, rejecting, and cancelling commit as the request's tenant
  alone, even there: what the commit publishes, the handlers it runs,
  and the run it queues are that tenant's. A maker's call made again
  ([Run by its maker](#run-by-its-maker)) spends a request of the tenant
  it is served as only.
- The approved command runs in the request's tenant alone, whichever
  tenant the message that runs it names: the maker's check, the
  command, and the request's end are that tenant's. Expiry reaches every
  tenant's requests, and `privacy erase` too, each committed as its
  request's tenant alone. `approvers_of` asks as the request's tenant.
- The seal covers the tenant, where the request has one: a request moved
  to another tenant in the database is neither decided nor run
  (`request_tampered`), and expiry leaves one whose stored tenant is not
  a tenant id as it is, logged. Do not set `tenant_id` on a stored request by
  hand: its seal no longer holds. Decide or cancel it where no tenant is
  served, or let it expire.

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
  expiry, status, run token, and tenant, where it has one. It is made
  again with each change of the request's state. Each decision's seal
  covers its request, person, verdict, and fingerprint. A row changed or
  added in the database by someone without the key is neither decided
  (`request_tampered`, 409) nor run (it fails with that code).
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
such as `insufficient_funds`): the maker may submit it again. Failing
makes the request's seal again only if it held, so the failed record of
a row changed around the application still fails its seal (since 2.9).
A use case that lost a race (`ConcurrentUpdate`) or failed in another
way is tried again by the worker, with its backoff.

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

A superuser made active again is a superuser made (since 2.7): with
`SUPERUSER_APPROVALS` above 0, `PATCH /users/{id}` with `"is_active":
true` for a deactivated superuser is refused (`approval_required`), as
making one is. Take superuser away while they are inactive, make them
active, then submit `auth.grant_superuser`.

`SUPERUSER_APPROVALS` other superusers approve it (or, with
`SUPERUSER_APPROVERS` set, holders of that permission), never the user
it promotes. The run checks that its maker is still an active
superuser, and fails it otherwise (`maker_not_allowed`). `manage.py
createsuperuser` stays the operator's way to make the first one, or
`createsuperuser --promote` to make one of an existing account.

**With one superuser, no one can approve.** The maker never approves
their own request, so, with `SUPERUSER_APPROVERS` unset, a request
needs `SUPERUSER_APPROVALS` superusers other than its maker. A project
with a single superuser has none: the request waits until it expires.
The way out is the operator's: `manage.py createsuperuser`, run on the
server, makes another superuser without approvals, who may then
approve. Keep more superusers than `SUPERUSER_APPROVALS`.

**With `SUPERUSER_APPROVERS`, a superuser can approve through a second
account.** With it set, whoever holds the permission approves, and a
superuser may give any permission to any account, through the admin
API, without approvals. So a superuser can give it to a second account
of their own, which then approves their request: the approval proves
two accounts, not two people. Staff who may change users and hold the
permission can give it too. Unset, only another superuser approves, and
no one makes a superuser alone over the API. Set it only where the
permission is given out under a control of its own, and watch the audit
trail for who is given it (`auth.user_permission_granted`,
`auth.group_permission_granted`, `auth.user_joined_group`), as
[for any approver](#what-maker-checker-does-not-stop).

`refuse_if_guarded(action)` raises `ApprovalRequired` when a guard is
registered for `action`. Use it on a path that does a guarded action
without its service. The auth admin API is such a path: before it makes
a superuser it runs the checks of auth's `on_make_superuser` hook, and
approvals registers one that asks `refuse_if_guarded`. Auth imports no
other app for it
([ADR 0140](../adr/0140-auth-asks-other-apps-through-hooks.md)).

## Changes of the admin API

A staff member who makes someone staff, adds them to a group, or sets
their password acts alone. PCI DSS asks that adding and changing user IDs
and their authentication factors be approved, and done with only the
privileges approved (requirement 8.2.4). `ADMIN_APPROVALS` holds the
changes it names for approval, each with the approvals it needs (since
2.4, [ADR 0154](../adr/0154-hold-admin-changes-for-approval-and-show-requests-to-those-they-concern.md)):

```python
APPROVALS = {
    "ADMIN_APPROVALS": {
        "auth.set_staff": 1,
        "auth.set_groups": 1,
        "auth.set_permissions": 1,
        "auth.set_password": 1,
        "auth.impersonate": 2,
        "auth.reactivate": 1,
        "auth.set_group_permissions": 1,
    },
}
```

| Action (constant) | Held when the admin API | The request's arguments | Policy |
| --- | --- | --- | --- |
| `auth.set_staff` (`SET_STAFF`) | makes a user staff: `PATCH /users/{id}` with `"is_staff": true`, for a user who is not; without `auth.reactivate`, also `"is_active": true` for a staff member who is inactive | `{"user_id"}`, and since 2.8 `"values"`: what else the call sets (`SetStaff`) | `auth.staff` |
| `auth.set_groups` (`SET_GROUPS`) | adds a user to a group: `PUT /users/{id}/groups` | `{"user_id", "added", "removed", "groups"}`: names, and each group added by id with its permissions (`SetGroups`, `HeldGroup`) | `auth.groups` |
| `auth.set_permissions` (`SET_PERMISSIONS`) | grants a user a permission: `PUT /users/{id}/permissions` | `{"user_id", "added", "removed"}`, permission keys (`SetPermissions`) | `auth.permissions` |
| `auth.set_password` (`SET_PASSWORD`) | sets a password, or makes it unusable: `POST /users/{id}/password` | `{"user_id"}` (`SetPassword`) | `auth.password` |
| `auth.impersonate` (`IMPERSONATE`) | acts as a user: `POST /users/{id}/impersonate` | `{"user_id", "reason"}` (`Impersonate`) | `auth.impersonation` |
| `auth.reactivate` (`REACTIVATE`, since 2.7) | makes a user who holds access active again: `PATCH /users/{id}` with `"is_active": true` | `{"user_id", "is_staff", "is_superuser", "groups", "permissions", "object_grants", "object_grants_digest"}`: what they come back with, and since 2.8 `"values"`: what else the call sets (`Reactivate`) | `auth.reactivation` |
| `auth.set_group_permissions` (`SET_GROUP_PERMISSIONS`, since 2.7) | grants a group a permission: `PUT /groups/{id}/permissions` | `{"group_id", "name", "added", "removed"}`, permission keys (`SetGroupPermissions`) | `auth.group_permissions` |

The staff member's client goes round once per change:

```http
PATCH /admin/auth/users/6f1c... {"is_staff": true}                       403 approval_pending, "approval_request_id": "0192..."
POST /api/approvals/requests/0192.../approve {"fingerprint": "3f1b..."}  200 (a superuser)
PATCH /admin/auth/users/6f1c... {"is_staff": true}                       200: the user is staff
```

- **The admin API's own checks come first**: the staff member's bounds
  (a group or a permission they lack), the fields, the password's
  validators. Then the project's own checks, then the change is
  submitted as the staff member's request, and the call answers `403
  approval_pending` with its id. The same call made while the request
  waits answers the same id, whatever password it sends.
- **A password the user had before is refused once the request is
  approved, and spends no approval** (since 2.7). The user's password
  history (`AUTH.PASSWORD_HISTORY`) is compared when the change is about
  to be made: under the approved request's lock, the one call that
  claims it has the admin API compare the password
  (`AdminChange.last_check`), and spends the approval only if it is not
  refused. While the request waits, nothing is compared, and a call that
  loses the claim to another made at once answers `approval_pending`,
  whatever its password ([Each change, authorized as one act](../auth/routes.md#each-change-authorized-as-one-act)).
- **Once approved, the same call by the same staff member makes the
  change, once.** The guards run by their maker
  ([Run by its maker](#run-by-its-maker)): the worker runs nothing, an
  impersonation's token goes to the staff member, and the change is made
  by the admin API, with its checks as of then.
- **The request holds the change, not the call.** For groups and
  permissions, it holds what the call adds and takes away, sorted, not
  the list sent. The fingerprint binds it: a call that would add or take
  away anything else, because the user's groups changed meanwhile,
  submits a request of its own.
- **A group is held by its id and what it grants, not by its name**
  (since 2.7, [ADR 0172](../adr/0172-hold-admin-changes-by-what-they-grant-and-check-before-the-hold.md)).
  Each group a request adds is in its `groups`: the id, the name, the
  permissions the group grants as the call found it, and its grants on
  single objects (`object_grants`, how many, and `object_grants_digest`,
  a SHA-256 of them). Before 2.7 an
  approval for "readers" made the user a member of whatever group was
  called "readers" when the call came back, so a group renamed to it, or
  deleted and made again, passed the second person's check (user report
  A21). Now a group renamed, made again, or granted other permissions
  since is another command: the call submits a request of its own,
  which shows what that group grants, and the approved one is never
  spent.
- **What else the call sets is held with the grant** (since 2.8, [ADR
  0179](../adr/0179-bind-the-admin-acts-groups-by-id-and-keep-a-held-confirmation-to-its-request.md)).
  `auth.set_staff` and `auth.reactivate` hold, in `values`, every field
  and flag the `PATCH` sets beside the grant: `{"mobile_number":
  "+447700900123"}`, `{"is_superuser": false}`. The approver sees each
  by its label ("Mobile number: +447700900123"), and the approval binds
  them: the call made again with another number submits a request of
  its own. Before, the request held the grant alone, and an approval of
  Bob's return was spent by a call that also gave Bob another address,
  which no approver saw (user report A17). A request of 2.7, which holds
  none, is found and spent as before by a call that sets nothing else. A
  value the user holds already is not held: it changes nothing, so a
  form saved whole holds what it changes, and the approver sees only
  that. Each text staff typed there is `Typed`, as on the admin API's
  screen (`typed_field`): with auth's `AUTHORIZATIONS.TYPED_LINKS`
  refused, one that holds a link is refused when the change is held, and
  approving a request that holds one is refused ([Text a person
  typed](../auth/actions.md#text-a-person-typed)).
- **A value another user has is refused before the hold** (since 2.8):
  an address that identifies users, a number, a username (`409`). Before,
  the commit refused it after the hold had spent the approval. A value
  the user holds already is not checked again: a form sent whole keeps an
  address two users shared before it identified users. One taken in the
  moment between that check and the commit is still refused by the
  commit, and the approval, spent by the hold by then, stays spent: ask
  for another.
- **A user who holds access comes back with an approval**
  (`auth.reactivate`, since 2.7). Making a user active again gives back
  everything they hold, so `"is_active": true` waits when the user is
  staff or a superuser (as the change leaves them), belongs to a group,
  or holds a permission, directly or on an object (every backend's
  `object_grants`). A user who holds none of these comes back at once.
  The request holds what they come back with: the flags, each group (as
  `SetGroups` holds one), their own permissions, and their grants on
  objects (how many, and a digest). Anything granted to them while they
  wait is another request.
  What a backend grants only an active user, such as a member's keys in
  a tenant (`MembershipBackend`), is not known here: guard it yourself
  ([Grants made elsewhere](#grants-made-elsewhere)). Without
  `auth.reactivate`, `auth.set_staff` holds a staff member's return, as
  it holds making staff. With both held, a call that makes a user staff
  and active again is refused (`403 approval_required`, "Make the user
  staff and active again in two changes: each waits for its own
  approval."): one request for both would spend one hold's approvals on
  the other's change.
- **Granting a group permissions waits too** (`auth.set_group_permissions`,
  since 2.7): every member gains them. The members of the group, as
  stored when a decision is made, do not decide it (`interested_party`).
  Taking permissions away, renaming, deleting, and creating a group (it
  has no members yet) wait for no one. So hold it with `auth.set_groups`:
  alone, a group made with the permissions, then joined, grants them
  unheld. `check` warns where it is held alone (`approvals.W001`, in
  staging and production).
- **A password is never stored**, in the request or anywhere else:
  `SetPassword` names the user alone. Approvers could not judge a
  password; they approve setting one, and the call made once it is
  approved sets one, which the validators check.
- **What only takes away waits for no one**: unmaking staff, taking a
  group or a permission away. Removing access should not wait for a
  second person.
- **Creating a user who is staff is refused** while making staff is held
  (`403 approval_required`, "Making staff needs approval: create the
  user, then make them staff."). Since 2.5 the refusal is auth's
  `on_create_staff` check, which the creation's twin asks too.
- **The approver's screen names the user** (since 2.7): "User:
  alice@example.com (…123456789abc)", their first identifier, cut to fit
  if need be, and the end of their id, which the request binds whole;
  the name is read when the screen is built and never stored with the
  request ("(erased)" once it is gone). Then "Adds" and "Takes away", each group or permission on as
  many lines as it takes, and left out when empty, or an impersonation's
  "Stated reason" (a rejection's own "Reason" shows beside it). With
  auth's `AUTHORIZATIONS.WHOLE` and `APPROVE_ACTION` on, a change that
  the approver's screen could not show whole is refused at the admin
  API's twin, before the staff member confirms it (auth's
  `on_admin_screen`, and `on_admin_group_screen` for a grant to a group),
  or when it is submitted without one (`422 too_much_to_show`): the
  staff member makes it in smaller steps, each approved whole.
- **The holds of what a request grants say it in words** (since 2.7).
  `auth.set_groups` shows each group added in a few words, as many on a
  line as fit: its name, read when the screen is built, the end of its
  id, and what it grants ("readers (…9abc) 3 perms + 2 object grants").
  `auth.reactivate` shows the user, "Superuser" and "Staff" where they
  come back as such, their "Groups" by name, and how many permissions
  of their own and grants on objects they hold ("Also holds").
  `auth.set_group_permissions` shows the "Group" by its name now (the
  name held, if it is gone) and the end of its id, then the keys it adds
  and takes away. A name too long for its line is cut beside the id,
  never refused; with `WHOLE`, what is refused is a grant too large to
  show, never a name. Since 2.8 `auth.set_staff` shows what else the
  call sets, by label, and `auth.reactivate` after what the user comes
  back with.
- **A key that matters is named, not counted** (since 2.8). A count
  ("readers (…9abc) 3 perms", "Also holds: 3 permissions") does not say
  which keys; auth's `AUTHORIZATIONS.NAMED_PERMISSIONS` lists the keys
  a screen names wherever it would count them, by default
  `TENANCY.ALL_TENANTS_PERMISSION` where it is set. `auth.set_groups`
  and `auth.reactivate` show those among what the groups, or the user,
  grant on a line of their own, "Grants: companies.administer_company",
  before the groups; `auth.set_group_permissions` and
  `auth.set_permissions` list them first among the keys added, so a list
  cut and counted keeps them. Keys are named where they are granted
  directly or through a group; a grant on a single object, a role's
  among them, is counted as one ("+ 1 object grant"), as the request
  holds it (how many, and a digest), whatever its key. The line counts as any other: with `WHOLE`
  a grant that no longer fits is refused, and made in smaller steps. A user who comes back in more groups than a screen
  shows is refused with `WHOLE`: take some groups away (that waits for
  no one), make them active again, then add the groups back, each step
  held and shown whole.
- **Superusers approve**, other than the staff member, and never the user
  the change is about (`interested_party`). With `ADMIN_APPROVERS` set,
  the holders of that permission approve instead; as with
  `SUPERUSER_APPROVERS`, a superuser can then give it to a second account
  of their own ([Making superusers](#making-superusers)).
- **Only the admin API submits them**: `POST /requests` answers
  `404 unknown_action`. Staff acting as someone else (an impersonation)
  are refused ("Requests are made and decided in one's own name only.").
- **With `AUTH.ADMIN_ACTION` on, the call is an act too** (since 2.5):
  its authorization is checked before the change is submitted, so a
  change other than the one authorized is refused and never waits. A
  call that waits spends nothing: once the request is approved, the
  staff member sends it again with an authorization, the first one while
  it lives (`AUTH.AUTHORIZATIONS.TTL`) or a new one
  ([Each change, authorized as one act](../auth/routes.md#each-change-authorized-as-one-act)).
  With `AUTH.AUTHORIZATIONS.HELD_TTL` set (since 2.7), the first one is
  kept for that request while it waits or is approved and not yet run,
  within its time and within `HELD_TTL` of the authorization's own
  expiry, so the staff member confirms the change once. Rejected,
  withdrawn, or expired, the request ends the hold, and the
  authorization with it
  ([Held for approval](../auth/actions.md#held-for-approval)). Kept for
  one request, it serves that one alone (since 2.8): a call whose
  command changed since (a group renamed, a user put in a group) is
  refused (`403 action_not_authorized`) where it would submit or wait
  on another request, and never spends one; a new confirmation, showing
  the change as it is now, does. An approved request whose time is up is
  passed over, never refused for.

The admin API asks through auth's `on_admin_change` hook, and, for a
group's changes, `on_admin_group_change` (since 2.7), which approvals
registers its checks with while `ADMIN_APPROVALS` names a change: auth
imports nothing of approvals ([ADR 0140](../adr/0140-auth-asks-other-apps-through-hooks.md)).
Each `AdminChange` carries the values it sets (`values`), such as
`is_staff` true or false and the reason of an impersonation, so a check of
your own can tell making staff from unmaking them
([the admin API](../auth/routes.md#staff-hand-out-only-what-they-have)).

**Your checks run before the hold.** Approvals registers its checks with
`order=HOLD_ORDER` (100), and a hook registered without an order runs
before them, whatever the place of its domain in `INSTALLED_DOMAINS`
(since 2.7). Before, the hold ran in domain order, and spent the staff
member's approval of a change that a check of a domain installed after
approvals then refused (user report A19). A check that must see the
hold's outcome registers with a higher order. At a change's twin
(`AUTH.ADMIN_ACTION`), the hold submits and spends nothing: it is only
asked (`AdminChange.asking`). A check of your own that calls a guard
`by_maker`, as the hold does, should return while `asking`: at a twin it
would submit a request.

**A check ordered after the hold sees an approval spent.** The hold
spends the staff member's approval as it lets the change go on; a check
of a higher order that then refuses leaves it spent, and the staff
member asks again. Refuse in a check of the default order, before the
hold. Never refuse a password change after it: a password the user had
before is refused by the hold, keeping the approval, and any other
password by your check, spending it, so the two answers tell them apart.

### Grants made elsewhere

`ADMIN_APPROVALS` holds the admin API's routes, and nothing else. Code of
your own that grants the same access goes around it: an invitation that
makes a user staff, or puts them in a group, when it is accepted; a
command that grants a permission; a backend of your own, or a tenant's
roles (`TENANCY.MEMBER_PERMISSIONS`), that give a user keys. Guard each
such path with a guard of its own ([Guarding a use case](#guarding-a-use-case)),
`by_maker` where the person's own call should make the change once it is
approved, as the admin API's holds are. A grant that names groups holds
them as `auth.set_groups` does with `held_groups(names)` (since 2.8):
each group by id, with the keys it grants and its grants on objects
(`HeldGroup`), sorted by name. Read again when the grant is made, a
group renamed, made again, or granted more since reads otherwise:
compare the two, and refuse.

```python
from domainkit.contrib.approvals import HeldGroup, held_groups


class InviteStaff(Command):
    email: str
    groups: tuple[HeldGroup, ...]


invite = InviteStaff(email=email, groups=await held_groups(names))
...
if await held_groups(group.name for group in invite.groups) != invite.groups:
    raise InvitationChanged()  # Ask for a new invitation.
```
 A guard that a setting turns on
must be registered whenever its service can be called: a service
decorated `requires_approval` with no guard refuses every call.

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

## Approving with an authorization

A step-up opens a window: for 5 minutes, the approver's session may
approve any request, as often as it likes, and so may a copy of the
session. With `APPROVE_ACTION` on, each approval asks for an
authorization of its own instead: one request, the fingerprint the
approver was shown, once
([Authorizing one act](../auth/actions.md), ADR 0144). It needs
`domainkit.contrib.auth`, and its routes that confirm an authorization:

```python
# config/settings.py
APPROVALS = {"APPROVE_ACTION": True}
```

```python
# The routes, next to approvals' own.
from domainkit.contrib.auth.actions import router as authorizations

router.include_router(authorizations, prefix="/auth")
```

Approvals registers the action `approvals.approve` with auth when it
starts. Its arguments are the decision, `ApproveRequest`: the request's
id, the approver, and the fingerprint they state. Its subject is
`approvals.request:<id>`. The approver's client goes round once per
approval:

```http
POST /api/approvals/requests/6c0e.../approve {"fingerprint": "3f1b..."}                 401 authorization_required
POST /api/approvals/requests/6c0e.../approve/authorization {"fingerprint": "3f1b..."}   201 {"id": "9f0c...", "shown": {...}}
POST /auth/authorizations/9f0c.../confirm {"code": "123456"}                            200 {"authorization": "9f0c....mYd3..."}
POST /api/approvals/requests/6c0e.../approve {"fingerprint": "3f1b..."}
     Action-Authorization: 9f0c....mYd3...                                              200
```

**What the approver is shown** is built by the server from the stored
request, never sent by a page: the title "Approve a request", the action,
then what the guard's `show` says of the command, or else its arguments
by name, in the command's order. Each value is the argument as compact
JSON (a string as itself), printable; a list goes over as many lines as
it takes ("added", "added (2)"), item by item, an object or a list as
compact JSON ([What a screen shows](../auth/actions.md#what-a-screen-shows)).
An argument that is `Money`, or an item of a list that is, shows as its
amount and currency, `1500.00 USD`, as `Money` writes itself (since
2.9). A value of a guard's `show` that JSON has not, such as `Money`, a
`Decimal`, or a `UUID`, shows as its text. A screen
holds six lines of 64 characters. What does not fit is cut and counted
(since 2.7, a list's last line says how many more items: "… and 22
more"), and with more arguments than lines, the last line says how many
more. With auth's `AUTHORIZATIONS.WHOLE` on, it is refused instead (`422
too_much_to_show`): asking to approve such a request, and, with
`APPROVE_ACTION` on, submitting one, so that no request waits for an
approval nobody could confirm whole. The approver reads the whole request
at `GET /requests/{id}`.

**What the maker typed** (since 2.8): a guard's `show` marks it `Typed`
(`("Memo", Typed(command.memo))`), as an impersonation's "Stated reason"
is. With auth's `AUTHORIZATIONS.TYPED_LINKS` set to `"refused"`, a
request whose typed text holds a link, an email address, or a number to
call is refused when it is submitted and when approving it is asked for
(`422 link_not_shown`); arguments shown by name are never marked
([Text a person typed](../auth/actions.md#text-a-person-typed)).

**Asking is refused as approving would be**: a request that is no longer
pending (`request_not_pending`), the maker (`maker_cannot_decide`), a
party to the command (`interested_party`), someone who decided it already
(`already_decided`), and a fingerprint that is not the request's
(`fingerprint_mismatch`). The twin checks the approve route's rules too:
only an approver who passes the approve rule may ask.

**The approval spends it.** `approve_request` spends it as a service
decorated `requires_authorization` does: where the requests share auth's
database, with the approval's commit, so an approval refused on the way
(the request expired meanwhile) spends nothing; elsewhere, just before
the approval. Sent again, it
is `403 action_not_authorized`. So is one sent with another fingerprint,
which also voids it. With the setting on, `approve_request` called from
code, with no request's authorization, is refused
(`AuthorizationRequired`, 401): the approval is the approver's own. Code
the project trusts approves with `record_approval`
([From the project's own code](#from-the-projects-own-code)).

**How it combines with the approve rule.** The authorization takes the
place of the step-up window, and the other rules still apply. A policy's
`approve_rule`, or `APPROVE_RULE`, still asks, on the twin as on the
approval: `IsUnscoped()`, a rule of your own. A rule that holds a
`StepUp` would ask the approver twice, for a window and for the act:
`check` refuses it (`approvals.E001`), inside `&`, `|`, or `~` too. Take
the `StepUp` out when you turn the setting on.

Off, the default, approving is as it was, and
`POST /requests/{id}/approve/authorization` answers `404`, as a route
that is not there: a caller the approve route refuses is refused there
first.

## Submitting, rejecting, and cancelling with an authorization

The other three steps of a request have actions of their own too (since
2.5, [ADR 0163](../adr/0163-authorize-admin-changes-and-requests-steps-as-acts.md)), each off by default and turned on by its own setting, as
`APPROVE_ACTION` is. A project whose web pages asked the phone's
free-text step-up for them (`POST /auth/app-sign-in/step-up`,
deprecated, [Sign-in approvals](../auth/ciba.md#the-pages-words-and-a-window-deprecated))
turns them on instead, and the phone confirms each act:

```python
APPROVALS = {
    "SUBMIT_ACTION": True,
    "APPROVE_ACTION": True,
    "REJECT_ACTION": True,
    "CANCEL_ACTION": True,
}
```

| Setting | Action | Its arguments | Subject | The twin |
| --- | --- | --- | --- | --- |
| `SUBMIT_ACTION` | `approvals.submit` | `SubmitRequest`: the action, the arguments as sent, and the maker | `approvals.action:<action>` | `POST /requests/authorization` |
| `REJECT_ACTION` | `approvals.reject` | `RejectRequest`: the request, the approver, and the reason | `approvals.request:<id>` | `POST /requests/{id}/reject/authorization` |
| `CANCEL_ACTION` | `approvals.cancel` | `CancelRequest`: the request, the maker, and the reason | `approvals.request:<id>` | `POST /requests/{id}/cancel/authorization` |

The client goes round once per act, as for approving:

```http
POST /api/approvals/requests {"action": "treasury.payout", "arguments": {...}}                 401 authorization_required
POST /api/approvals/requests/authorization {"action": "treasury.payout", "arguments": {...}}   201 {"id": "9f0c...", "shown": {...}}
POST /auth/app-sign-in/authorizations/9f0c...                                                  200 {"request": "kq3D1...", ...}
POST /auth/app-sign-in/authorizations/token {"request": "kq3D1..."}                           200 {"authorization": "9f0c....mYd3..."}
POST /api/approvals/requests {"action": "treasury.payout", "arguments": {...}}
     Action-Authorization: 9f0c....mYd3...                                                     201
```

- **What is shown** is built by the server: "Submit a request", "Reject
  a request", or "Cancel a request"; the action; the reason, when one is
  given; then what the guard shows, or the arguments by name, as approving
  shows them. A submission shows the arguments as they would be stored,
  the maker's fields set to the maker. With auth's
  `AUTHORIZATIONS.WHOLE` on, submitting refuses what does not fit whole,
  as approving does; rejecting and cancelling never do: they stop a
  request, so what does not fit is cut and counted. Their own reason is
  the decider's: with `WHOLE` on, one longer than a line is refused (`422
  too_much_to_show`, since 2.8), and with `TYPED_LINKS` refused, one that
  holds a link (`422 link_not_shown`); what the request typed never is.
- **The act is bound.** A submission with other arguments, a rejection
  or a cancellation with another reason, or of another request, is
  refused (`403 action_not_authorized`), and voids the authorization.
- **Asking is refused as the act would be**: an unknown action
  (`unknown_action`), arguments that make no command
  (`invalid_arguments`), a command no policy holds
  (`approval_not_needed`); the maker rejecting (`maker_cannot_decide`),
  someone else cancelling (`not_the_maker`), a request no longer pending.
  The twin asks the route's rules too.
- **The service spends it**, as `approve_request` does: with the
  request's commit where the requests share auth's database. With a
  setting on, `submit_request`, `reject_request`, or `cancel_request`
  called from code, with no request's authorization, is refused
  (`AuthorizationRequired`, 401). Code the project trusts calls
  `record_submission`, `record_rejection`, or `record_cancellation`
  ([below](#from-the-projects-own-code)).
- **Only `POST /requests` is `approvals.submit`.** A guarded service
  called directly, and a change of the admin API held for approval,
  submit as before: their own routes are the acts
  ([the admin API](../auth/routes.md#each-change-authorized-as-one-act)).
- **Rejecting stops a request.** It asks for an authorization only where
  `REJECT_ACTION` is on: leave it off where stopping a request should
  never wait for a second factor.

Off, each step is as it was, and its twin answers `404`, to a caller the
step's route does not refuse first.

## From the project's own code

With an act's setting on, `submit_request`, `approve_request`,
`reject_request`, and `cancel_request` refuse a call from code: the act
is a person's own, and only their request carries its authorization. The
project's own code takes these steps too, with no phone to ask: a
periodic task that withdraws the requests left waiting, a management
command, an event handler that withdraws a request when what it is about
is deleted. It calls the functions those services run (since 2.6,
[ADR 0163](../adr/0163-authorize-admin-changes-and-requests-steps-as-acts.md),
amended):

| Function | Command | What it is |
| --- | --- | --- |
| `record_submission` | `SubmitRequest` | `submit_request`, without `SUBMIT_ACTION`'s authorization |
| `record_approval` | `ApproveRequest` | `approve_request`, without `APPROVE_ACTION`'s |
| `record_rejection` | `RejectRequest` | `reject_request`, without `REJECT_ACTION`'s |
| `record_cancellation` | `CancelRequest` | `cancel_request`, without `CANCEL_ACTION`'s |

Each takes the command and an `ApprovalsUnitOfWork`, as its service
does, returns the request, and asks for no authorization, whatever the
settings say. A periodic task that withdraws, as their makers, the
payouts left waiting for a day:

```python
from contextlib import suppress
from datetime import timedelta

from domainkit import clock
from domainkit.contrib.approvals import (
    ApprovalsUnitOfWork,
    CancelRequest,
    RequestNotPending,
    RequestStatus,
    record_cancellation,
)
from domainkit.contrib.messaging import periodic
from domainkit.ddd import F


@periodic(every=timedelta(hours=1), name="treasury.withdraw_stale")
async def withdraw_stale() -> None:
    async with ApprovalsUnitOfWork() as uow:
        stale = await uow.requests.filter(
            (F("action") == "treasury.payout")
            & (F("status") == RequestStatus.PENDING)
            & (F("submitted_at") < clock.now() - timedelta(days=1))
        )
    for request in stale:
        with suppress(RequestNotPending):  # Decided meanwhile.
            await record_cancellation(
                CancelRequest(
                    request_id=request.id, maker=request.maker, reason="Stale."
                ),
                ApprovalsUnitOfWork(),
            )
```

- **They refuse what the services refuse**, but for the authorization.
  Every step but a submission refuses a seal that does not hold
  (`request_tampered`) and a request no longer pending
  (`request_not_pending`).
  A submission refuses an unknown action (`unknown_action`), arguments
  that make no command or a maker of no characters, or more than 255
  (`invalid_arguments`), and a command no policy holds
  (`approval_not_needed`).
  Approving and rejecting refuse a request whose time is up
  (`request_not_pending`), the maker (`maker_cannot_decide`), a party to
  the command (`interested_party`), a second decision
  (`already_decided`), a guard or policy gone (`policy_removed`), and an
  approver of no characters, or more than 255 (`invalid_arguments`);
  approving refuses another fingerprint (`fingerprint_mismatch`), and
  rejecting a reason over 500 characters.
  Cancelling refuses anyone but the maker (`not_the_maker`) and a reason
  over 500 characters. It withdraws a request whose guard or policy is
  gone, or whose time is up before the worker marks it expired, as
  `cancel_request` does.
  The approval that completes a request queues its command, and each
  step is an event, as from a route.
- **The trail names who ran the code.** The request names the maker and
  the approvers the code gives; the audit row of each step names the
  context's actor ([audit](audit.md)): `system` in a periodic task, the
  actor whose request queued a task or committed an event. A management
  command runs as `system` too, unless it sets `operator_actions`: then
  it runs as the operator given with `--operator`
  ([Operators](../reference/commands.md#operators)). Set it on a command
  that decides or withdraws for someone, so the trail names who ran it.
- **Who acts is the code's to answer for**, as for every service of
  approvals. The routes' rules are not asked: being an approver of the
  policy (its permission, its group, or its approver rule), the approve
  rule, acting in one's own name, the model permissions, and, at
  submission, the guard's permission and `maker_rule`. The run asks
  those of the maker only with `domainkit.contrib.auth` installed and a
  maker that is a user's id, on a connection without credentials: a
  `maker_rule` that reads credentials refuses there, and a maker that
  names no user is never asked
  ([What maker-checker does not stop](#what-maker-checker-does-not-stop)).
  Code that acts for a person it did not authenticate, such as a command
  given a user's id, checks them first: `approvers_of(request)` lists who
  may decide a request now.
- **Never from a route a user reaches.** Such a route would take the
  step without the phone and without the route's rules. Nothing refuses
  a route that asks for nothing: a call from a task and a call from a
  route look the same as they run, and a handler of an event that a
  route commits runs inside that route's request. Routes run the
  services.

A route that asks for the act's authorization and runs a `record_*`
function is worse than one that asks nothing. Its rule takes any
authorization of the act; the service binds it to the request and its
arguments when it spends it, and a `record_*` function spends none. So an
authorization confirmed for one request would take the step on another.
Where that can be told, it is refused:

| The route | What happens |
| --- | --- |
| A `ResourceRouter` route of approvals' rules (`RequestActions`, `ApproveAction`, `RejectAction`, `CancelAction`, `SubmitAction`) | Refused when it is declared (`ImproperlyConfigured`, since 2.6), as is one that runs another step's service. A service of your own passes: the rule cannot tell what it calls. |
| A `ResourceRouter` route that asks `StepUp(for_="approvals.cancel")` | Warns when it is declared (`DomainkitDeprecationWarning`), then runs as the next row. |
| Any other route that asks | Takes the step, then leaves the authorization unspent. Under `DEBUG` and in tests the request fails after the step is committed (`500 authorization_unspent`); elsewhere it answers as the step did, and the authorization is voided and logged ([Authorizing one act](../auth/actions.md)). |

## Over HTTP

```python
from domainkit.contrib.approvals.api import router as approvals

router.include_router(approvals, prefix="/api/approvals")
```

| Route | Who | What it does |
| --- | --- | --- |
| `GET /requests`, `/requests/{id}` | `approvals.view_approvalrequest` | Requests (`?status=pending`, `action`, `policy`, `maker`), with their command, fingerprint, and decisions: those the caller made, and those they may decide ([Who reads which requests](#who-reads-which-requests)). |
| `POST /requests` | `approvals.add_approvalrequest`, and the guard's `permission` and `maker_rule` | Submit `{"action": ..., "arguments": {...}}`: 201, with `Location`; with `SUBMIT_ACTION`, and an `Action-Authorization` of this submission. |
| `POST /requests/{id}/approve` | an approver of its policy, and its approve rule | Approve, stating `{"fingerprint": ...}`; with `APPROVE_ACTION`, and an `Action-Authorization` of this approval. |
| `POST /requests/{id}/approve/authorization` | as for approving | With `APPROVE_ACTION` on: ask for an authorization of approving, stating `{"fingerprint": ...}`: 201, `{"id", "purpose", "shown", "expires_at", "methods"}`. Off: 404. |
| `POST /requests/{id}/reject` | an approver of its policy | Reject, with `{"reason": ...}`; with `REJECT_ACTION`, and an `Action-Authorization` of this rejection. |
| `POST /requests/{id}/reject/authorization` | as for rejecting | With `REJECT_ACTION` on: ask for an authorization of rejecting, with `{"reason": ...}`: 201. Off: 404. |
| `POST /requests/{id}/cancel` | `approvals.add_approvalrequest`, and being its maker | Withdraw it (`{"reason": ...}`, optional); with `CANCEL_ACTION`, and an `Action-Authorization` of this cancellation. |
| `POST /requests/{id}/cancel/authorization` | as for cancelling | With `CANCEL_ACTION` on: ask for an authorization of cancelling: 201. Off: 404. |
| `POST /requests/authorization` | as for submitting | With `SUBMIT_ACTION` on: ask for an authorization of submitting `{"action", "arguments"}`: 201. Off: 404. |

Submitting, approving, rejecting, and cancelling also refuse a caller
who acts for someone else (impersonation, above). The maker and the
approvers are the principal's id, whatever authenticates requests.
`domainkit.contrib.approvals.api` also exports `requests`, the resource
router that `router` mounts.

### Who reads which requests

A request holds its command, and the command may say much: an amount and
an account, a password reset, the reason staff act as someone. Since 2.4,
`GET /requests` and `GET /requests/{id}` show a caller with
`approvals.view_approvalrequest`:

- the requests they made;
- the requests whose policy names them among its approvers: they hold its
  permission, belong to its group, or pass its approver rule (asked about
  them alone, not about the request), whoever made them.

Any other request is `404 approval_request_not_found`, as if it were not
there. `approvals.view_every_approvalrequest` shows every request: give it
to auditors. With multi-tenancy, each of these is a request of the tenant
being served ([Requests and tenants](#requests-and-tenants)), and that
key held only through a member's role shows every request but those of a
guard `everywhere`, which go to those who hold it everywhere. Approving,
rejecting, and cancelling are not narrowed: their own rules answer, as
before. Approval systems work the same way: an
approver's list holds what is waiting for them, and administrators see
everything (ADR 0154).

### Rules of your own on the routes

The routes ask for model permissions, and approving asks the approve rule
too. A project that wants staff alone to submit, reject, and cancel, or a
fresh second factor to submit, mounts a copy of the router with
dependencies of its own on those routes (`select_routes`,
[Rules on the framework's routes](../auth/routes.md#rules-on-the-frameworks-routes)):

```python
from datetime import timedelta

from fastapi import APIRouter, Depends

from domainkit.contrib.approvals.api import router as approvals
from domainkit.contrib.auth import IsAdminUser, StepUp
from domainkit.http.access import Require
from domainkit.http.routers import select_routes

STAFF_ONLY = Depends(Require(IsAdminUser()))
STEP_UP = Depends(Require(StepUp(max_age=timedelta(minutes=5))))

router = APIRouter()
router.include_router(
    select_routes(
        approvals,
        dependencies={
            "POST /requests": [STAFF_ONLY, STEP_UP],
            "POST /requests/{request_id}/reject": [STAFF_ONLY],
            "POST /requests/{request_id}/cancel": [STAFF_ONLY, STEP_UP],
        },
    ),
    prefix="/api/approvals",
)
```

The routes' own rules still apply; the dependencies run after them,
before the route. Mount the copy in place of `approvals`, not beside it.
Keep a step-up off rejecting: stopping a request should never wait for a
second factor. Approving has its own setting (`APPROVE_RULE`, or
`APPROVE_ACTION`). No setting of approvals does this, as the rule is the
project's: `route_names(approvals)` lists the names. To have each
submission or cancellation confirmed once, rather than a step-up window,
turn on `SUBMIT_ACTION` and `CANCEL_ACTION`
([above](#submitting-rejecting-and-cancelling-with-an-authorization)).

## Telling the approvers

Nobody is told that a request waits. `RequestSubmitted` is recorded with
each request, and `approvers_of(request)` (since 2.4) gives the active
users who may decide it now:

- the holders of its permission (`has_perm`: granted to them or to a
  group of theirs; superusers hold every permission);
- the members of its group;
- for an approver rule, the superusers and staff the rule lets through,
  asked about the user alone, on a connection without credentials;
- never its maker, a party to its command, or someone who decided it
  already.

It needs `domainkit.contrib.auth`. A permission that only a backend of
your own grants (`AUTH.BACKENDS`) is not looked for: its holders are not
found. A durable handler tells them, through the outbox
([mail](../mail.md)):

```python
from domainkit.contrib.approvals import (
    ApprovalsUnitOfWork,
    RequestSubmitted,
    approvers_of,
)
from domainkit.contrib.mail import send_mail
from domainkit.ddd import handles


@handles(RequestSubmitted, durable=True, name="payments.tell_approvers")
async def tell_approvers(event: RequestSubmitted) -> None:
    async with ApprovalsUnitOfWork() as uow:
        request = await uow.requests.get(event.request_id)
    addresses = [user.email for user in await approvers_of(request) if user.email]
    if addresses:
        await send_mail(
            f"An approval waits: {request.action}",
            f"Request {request.id} waits for your decision.",
            addresses,
        )
```

Put it in a domain's `controllers.py`. The message names the request, not
its command: the approver reads the command at `GET /requests/{id}`,
where they may. Send a push or a text message the same way
([mail](../mail.md)).

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
| `APPROVE_ACTION` | `False` | Each approval asks for an authorization of its own (`approvals.approve`), bound to the request and its fingerprint, in place of a step-up window ([Approving with an authorization](#approving-with-an-authorization)). It needs `domainkit.contrib.auth` (`setup()` fails without it); `check` refuses a `StepUp` in an approve rule beside it (`approvals.E001`). |
| `SUBMIT_ACTION` | `False` | Submitting at `POST /requests` asks for an authorization of its own (`approvals.submit`), bound to the action and its arguments ([Submitting, rejecting, and cancelling with an authorization](#submitting-rejecting-and-cancelling-with-an-authorization), since 2.5). It needs `domainkit.contrib.auth` (`setup()` fails without it). |
| `REJECT_ACTION` | `False` | Rejecting asks for an authorization of its own (`approvals.reject`), bound to the request and the reason (since 2.5). It needs `domainkit.contrib.auth`. |
| `CANCEL_ACTION` | `False` | Cancelling asks for an authorization of its own (`approvals.cancel`), bound to the request and the reason (since 2.5). It needs `domainkit.contrib.auth`. |
| `MAX_ARGUMENTS_BYTES` | `65536` | The largest command a request holds, as JSON (1 KiB to 1 MiB). |
| `ENTRY_THRESHOLDS` | `{}` | Per currency, the amount (text) over which `accounting.post_entry`, `capture_hold`, and `reverse_entry` need approval; empty: they are not guarded. A currency it does not name always needs approval, unless `"*"` gives an amount for every one (read in each currency's units). An amount not exact in its currency fails `setup()`. |
| `ENTRY_APPROVALS` | `2` | Approvals a posting over its threshold needs (at most 10). |
| `ENTRY_APPROVERS` | `"accounting.add_entry"` | The permission whoever approves a posting holds. |
| `SUPERUSER_APPROVALS` | `0` | Approvals that making a superuser needs (at most 10); 0: it is not guarded. |
| `SUPERUSER_APPROVERS` | `None` | The permission whoever approves making a superuser holds; `None`: superusers approve, and only they. An empty text is `None` too (an unset variable in a compose file). Set, it lets a superuser approve through a second account ([Making superusers](#making-superusers)). |
| `ADMIN_APPROVALS` | `{}` | The changes of the admin API held for approval, each with the approvals it needs (1 to 10): `"auth.set_staff"`, `"auth.set_groups"`, `"auth.set_permissions"`, `"auth.set_password"`, `"auth.impersonate"`, and, since 2.7, `"auth.reactivate"` and `"auth.set_group_permissions"`. Empty: none is ([Changes of the admin API](#changes-of-the-admin-api)). It needs `domainkit.contrib.auth` (`setup()` fails without it). |
| `ADMIN_APPROVERS` | `None` | The permission whoever approves those changes holds; `None`: superusers approve, and only they. An empty text is `None` too. |

## On PostgreSQL and SQLite

Each approval takes the request's lock: an advisory lock on PostgreSQL,
the database's write lock on SQLite. The tests run the proofs on both,
and on PostgreSQL 20 approvals of one request really run at once:
exactly one completes it.

## Errors and refusals

| Error | When | HTTP |
| --- | --- | --- |
| `ApprovalRequired` | A direct call of a command that needs approval. | 403, `approval_required` |
| `ApprovalPending` | The same, with `submit=True`: the request was submitted. | 403, `approval_pending`, with `approval_request_id` |
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

With `APPROVE_ACTION` on, approving answers auth's refusals too:
`401 authorization_required` without an authorization, `403
action_not_authorized` with one that does not hold, and those of asking
for one ([Errors and refusals](../auth/actions.md#errors-and-refusals)).
With auth's `AUTHORIZATIONS.WHOLE` on as well, asking to approve, and
submitting, a request whose screen cannot show it whole answers `422
too_much_to_show` (`TooMuchToShow`), and with `TYPED_LINKS` refused, one
whose typed text holds a link answers `422 link_not_shown`
(`LinkNotShown`).
So do submitting, rejecting, and cancelling with `SUBMIT_ACTION`,
`REJECT_ACTION`, and `CANCEL_ACTION` on: rejecting and cancelling for
their own reason only.

## Common mistakes

- **Decorating the service without registering the guard.** Every call
  is then refused. Call `guard()` in `ready()`.
- **Calling the undecorated function from a route.** It skips approval.
  Guard the service itself, and `guard()` refuses one that is not
  decorated.
- **Calling `record_approval` (or another `record_*`) from a route.** It
  skips the act's authorization and the route's rules on who acts, and a
  `ResourceRouter` route of approvals' rules that runs one is refused
  when it is declared. Routes run the services; tasks, commands, and
  handlers of the project's own call the `record_*` functions.
- **Letting approvers create users.** One person can then approve with a
  second account. Keep the two rights apart.
- **A use case that commits twice.** The request is marked with the
  first commit. Keep it to one.
- **Waiting for the worker to make a change of the admin API.** It never
  does: the staff member sends the same call again once the request is
  approved.
- **Holding the admin API's grants and not your own.** An invitation, a
  command, or a backend that grants access goes around `ADMIN_APPROVALS`:
  guard it too ([Grants made elsewhere](#grants-made-elsewhere)).
- **Removing `SECRET_KEY` while requests wait.** Their seals no longer
  hold. Keep the old key in `SECRET_KEY_FALLBACKS`.

## See also

- [Accounting](accounting.md): the ledger whose postings can be guarded.
- [Limits](limits.md): bound what moves in a window, against splitting.
- [Stepping up](../auth/step-up.md): a fresh second factor.
- [Authorizing one act](../auth/actions.md): what `APPROVE_ACTION`,
  `SUBMIT_ACTION`, `REJECT_ACTION`, and `CANCEL_ACTION` ask for.
- [Audit](audit.md): the events of every request.
- [Privacy](../privacy.md): erasure and export.

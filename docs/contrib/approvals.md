# Approvals: maker-checker for sensitive commands

`domainkit.contrib.approvals` makes a command run only once people other
than whoever asked for it approve it: a large payment, a journal entry
over a limit, a user made superuser. Whoever submits the command is the
**maker**; whoever approves it is a **checker**, and the maker is never
one of them. The command waits as a pending **request**, holding exactly
the arguments it was submitted with and their fingerprint, until enough
checkers approve that fingerprint; then it runs once, through the
outbox, after the worker checks it is still what was approved. Every
step is a domain event, recorded in the audit trail with its actor. The
decisions are [ADR 0121](../adr/0121-hold-guarded-commands-as-sealed-requests-and-count-approvals-under-the-requests-lock.md).

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # approved commands run in the worker
    "domainkit.contrib.audit",  # every decision is audited
    "domainkit.contrib.approvals",
    ...,
]
```

Then `makemigrations` and `migrate`: the app brings two tables,
`domainkit_approval_requests` and `domainkit_approval_decisions`, in the
database `DATABASE_ROUTING['approvals']` names, or `default`.

## Guarding a use case

A use case is guarded in two steps. Its service is decorated, so that
calling it directly is refused whenever its command needs approval:

```python
from domainkit.contrib.approvals import requires_approval


@requires_approval("payments.refund")
async def refund(command: Refund, uow: PaymentsUnitOfWork) -> Payment: ...
```

and its domain registers the guard in `ready()`, with the policies that
decide who approves it and when (the same place every hook is registered,
[ADR 0101](../adr/0101-register-every-domain-hook-and-check-it-when-it-registers.md)):

```python
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

- The **command** is the service's first argument, a `Command` that
  pydantic can read and write as JSON (dataclasses, `UUID`, `date`,
  `Money`, enums, and the like); `command=` names it otherwise.
- The first policy whose `when` holds applies; a command none applies to
  needs no approval and runs at once, as before.
- `permission` is what a maker must hold to submit the command over HTTP:
  the permission the direct route would ask for.
- Called directly with a command that needs approval, the service raises
  `ApprovalRequired` (403, `approval_required`) and runs nothing: it fails
  closed. With `submit=True` it submits the command for whoever is acting
  and raises `ApprovalPending` (403, `approval_pending`), whose
  `request_id` names the request; a route may answer that with a 202.
- A decorated service with no guard registered refuses every call
  (`ImproperlyConfigured`), and `guard()` refuses a service that is not
  decorated: without the decorator, a direct call would skip approval.
- Only the run of an approved request passes the guard, and only with the
  command it approved: a guarded service called from inside it with
  another command is refused like any other.

### Policies

```python
Policy(
    "treasury.payout",
    approvals=2,  # 1 to 10
    group="treasurers",  # or permission="treasury.approve_payout"
    expires_after=timedelta(hours=4),  # default APPROVALS.EXPIRES_AFTER
    approve_rule=RecentSecondFactor(),  # see below
    when=lambda command: ...,
)
```

- **Approvers** are the holders of a permission, or the members of a
  group: one of the two. A group needs a principal with `groups` (as
  `domainkit.contrib.auth`'s users have) and credentials not limited to
  scopes; a permission is checked as `HasPermissions` checks it, scopes
  included.
- **The maker never approves**, also when they hold the permission or
  belong to the group, and never rejects their own request: they cancel
  it.
- **One person, once.** A decision is stored per request and person (the
  principal's id), unique in the database: the same person through
  another session, device, or token is the same person, refused with
  `already_decided` (409), even from two requests at once.
- A request needs `approvals` approvals, and one rejection ends it.
- **Content.** `when` reads the command, so a policy may apply only to
  some of them. `Threshold` reads amounts per currency:
  `Threshold({"USD": "10000", "EGP": "500000"}).exceeded_by(amounts)`
  sums them per currency and says whether any sum is more than its
  currency's limit; a currency it has no limit for is never over.

## Requests

```python
from domainkit.contrib.approvals import (
    ApprovalsUnitOfWork,
    ApproveRequest,
    approve_request,
    submit_command,
)

request = await submit_command("payments.refund", command, maker=str(user.id))
await approve_request(
    ApproveRequest(
        request_id=request.id, approver=str(checker.id), fingerprint=request.fingerprint
    ),
    ApprovalsUnitOfWork(),
)
```

- **What is stored.** The command read back as JSON (`arguments`), never
  the JSON as sent: defaults filled, amounts at their currency's places.
  Its `fingerprint` is the SHA-256 of `{"action": ..., "arguments": ...}`
  as canonical JSON (keys sorted, no spaces, UTF-8), so a client can work
  it out from what it shows. At most `MAX_ARGUMENTS_BYTES` of it.
- **An approval states the fingerprint** the approver was shown; a
  request that holds anything else is refused (`fingerprint_mismatch`,
  409). Nothing changes a request's command after it is submitted: there
  is no route or service that does.
- **Expiry.** A request waits until `expires_at` (its policy's
  `expires_after`, or `EXPIRES_AFTER`); after it, approvals are refused
  (`request_not_pending`), and the worker marks it expired every
  `EXPIRE_EVERY` (`manage.py approvals expire` does it on demand).
- **Rejection** takes a reason (1 to 500 characters), kept on the
  request; **cancellation** is the maker's, with an optional reason.
- A request is `pending`, then `approved` (its command queued),
  `executed` or `failed`, or `rejected`, `cancelled`, or `expired`.

### What runs, and when

The approval that completes a request queues its command through the
outbox, in the same transaction ([messaging](messaging.md)): it is
queued exactly when the request is approved, and once. Each approval
takes the request's lock first (`uow.lock`: an advisory lock on
PostgreSQL, the write lock on SQLite), so approvals given at once are
counted one after another, and exactly one of them completes the request;
the request's `version` changes with each approval, so a change that
went around the lock is refused too.

The worker runs it under the inbox (ADR 0094), once however often the
message is delivered, after checking that it is what was approved:

- the request's **seal** and each decision's hold: an HMAC, under a key
  derived from `SECRET_KEY`, of the request's id, action, fingerprint,
  maker, and approvals needed, and of each decision's request, person,
  verdict, and fingerprint. A row changed or added in the database by
  someone without the key runs nothing;
- the fingerprint is that of the arguments, and enough people other than
  the maker approved it, each once;
- the arguments still make the command (the guard's command may have
  changed in a deploy since), and read back the same.

A request that fails a check is `failed`, with the refusal's code as its
`reason` (`fingerprint_mismatch`, `invalid_arguments`, `unknown_action`
if the guard is gone), and runs nothing. So does one whose use case
refuses it (a `DomainError`, such as `insufficient_funds`): the maker may
submit it again. A use case that lost a race (`ConcurrentUpdate`) or
failed otherwise is retried by the worker with its backoff.

Where the use case's unit of work commits to the requests' database,
the request is marked `executed` in that same transaction, with its
first commit: the command's change, the request's status, its audit
row, and the inbox row commit together, or none does. A use case on
another database commits first; the request is marked after it, and a
message run again after that commit marks it then (the inbox refuses the
use case's second commit). Keep a guarded use case to one commit: one
that commits twice is marked with the first.

`SECRET_KEY_FALLBACKS` verify seals as they verify signatures, so keep
an old key there while requests sealed under it may still run.

## Large postings

```python
APPROVALS = {
    "ENTRY_THRESHOLDS": {"USD": "10000.00", "EGP": "500000.00"},
    "ENTRY_APPROVALS": 2,
    "ENTRY_APPROVERS": "accounting.add_entry",
}
```

With `domainkit.contrib.accounting` installed and `ENTRY_THRESHOLDS`
set, an entry that moves more than its currency's amount (the sum of its
debits in that currency) needs `ENTRY_APPROVALS` approvals by holders of
`ENTRY_APPROVERS`: by default, other clerks who may post entries
themselves. `post_entry` refuses it otherwise (`approval_required`),
whether called over HTTP, from code, or from a message (`defer_entry`
dead-letters it). Its makers submit it as the action `accounting.post_entry`
(`ENTRIES`), with the body `POST /entries` takes, and need
`accounting.add_entry` to. A currency without an amount is not guarded,
and `capture_hold` and `reverse_entry` are not either: they post what an
earlier hold or entry already allowed. Approvals reaches the ledger
through its `on_posting` hook only; the ledger's own semantics are
unchanged.

## Making superusers

With `SUPERUSER_APPROVALS` above 0 and `domainkit.contrib.auth`
installed, the auth admin API refuses to make a user a superuser, on
create or change (`approval_required`); unmaking one is not guarded. A
superuser submits `auth.grant_superuser` (`SUPERUSERS`) with
`{"user_id": ...}` instead, and `SUPERUSER_APPROVALS` holders of
`SUPERUSER_APPROVERS` (superusers hold every permission) approve it.
`manage.py createsuperuser` stays the operator's way to make the first.

## Who may approve: step-up

Approving can ask for more than being an approver: a recent second
factor, say. The rule is an access rule of any kind
([access](../auth/routes.md)): a policy's `approve_rule`, or else
`APPROVALS.APPROVE_RULE`, the import path of a rule instance, or of a
rule class that takes no arguments. It is asked about the request and
the request's object:

```python
# config/rules.py: contrib.auth's step-up rule, or a rule of the project's own.
RECENT_PROOF = StepUp(max_age=timedelta(minutes=5))

# config/settings.py
APPROVALS = {"APPROVE_RULE": "config.rules.RECENT_PROOF"}
```

Any `BasePermission` fits, so `contrib.auth`'s step-up rule
(`StepUp(max_age=...)`), a project's own, or a combination
(`StepUp(max_age=...) & IsUnscoped()`) all work, and a policy may ask for
a fresher proof than the rest (`approve_rule=StepUp(max_age=...)`). A
refused approval answers 403 ("You cannot approve this request now."),
or what the rule itself raises. Rejecting asks only that
the person is an approver: stopping a request should never wait for a
second factor.

## Over HTTP

```python
from domainkit.contrib.approvals.api import router as approvals

router.include_router(approvals, prefix="/api/approvals")
```

| Route | Who | Does |
| --- | --- | --- |
| `GET /requests`, `/requests/{id}` | `approvals.view_approvalrequest` | Requests (`?status=pending`, `action`, `policy`, `maker`), with their command, fingerprint, and decisions. |
| `POST /requests` | `approvals.add_approvalrequest`, and the guard's `permission` | Submit `{"action": ..., "arguments": {...}}`: 201, with `Location`. |
| `POST /requests/{id}/approve` | an approver of its policy, and its approve rule | Approve, stating `{"fingerprint": ...}`. |
| `POST /requests/{id}/reject` | an approver of its policy | Reject, with `{"reason": ...}`. |
| `POST /requests/{id}/cancel` | `approvals.add_approvalrequest`, and being its maker | Withdraw it (`{"reason": ...}`, optional). |

The maker and the approvers are the principal's id, whatever
authenticates requests. Refusals answer with their codes:
`maker_cannot_decide` and `not_the_maker` (403), `already_decided`,
`fingerprint_mismatch`, and `request_not_pending` (409),
`invalid_arguments` and `approval_not_needed` (422), `unknown_action`
and `approval_request_not_found` (404).

## From the command line

`manage.py approvals list` lists the pending requests (`--status`
another status, `--limit`), `show ID` one with its command and
decisions, and `expire` expires the overdue, recorded as the operator's
action `operator.approvals.expire` ([operators](../reference/commands.md#operators)).
What makers and approvers wrote is shown with control characters escaped.

## Audit and privacy

Every change is an event the audit trail records with the actor who
made it ([audit](audit.md)): `approvals.request_submitted`,
`approval_given`, `request_approved`, `request_rejected`,
`request_cancelled`, `request_expired`, `request_executed`, and
`request_failed`. The approved command's own events are recorded with
the actor whose approval queued it, as work a request queues keeps its
actor. Reasons are kept on the request, not in the trail.

`privacy export` lists the requests a person made, with their commands,
and the decisions they took. `privacy erase` keeps the record that a
request was made and decided, but unlinks the person: their pending
requests are cancelled, the commands of their requests and the reasons
they wrote are blanked (the fingerprint stays, and proves what was
approved), and their id becomes one `erased:` id, so a request still
shows that its maker and approvers were different people. A request of
theirs approved but not run yet no longer verifies, and fails rather
than runs.

## Settings

| `APPROVALS` key | Default | Meaning |
| --- | --- | --- |
| `EXPIRES_AFTER` | 3 days | How long a request waits for its approvals, unless its policy says (1 minute to 90 days). |
| `EXPIRE_EVERY` | 5 minutes | How often the worker expires the requests whose time is up (at most 1 day). |
| `APPROVE_RULE` | `None` | Import path of an access rule (an instance, or a class taking no arguments) whoever approves must pass as well, unless the policy names its own; a path that is not a rule fails `setup()`. |
| `MAX_ARGUMENTS_BYTES` | `65536` | The largest command a request holds, as JSON (1 KiB to 1 MiB). |
| `ENTRY_THRESHOLDS` | `{}` | Per currency, the amount (text) over which `accounting.post_entry` needs approval; empty: postings are not guarded. An amount not exact in its currency fails `setup()`. |
| `ENTRY_APPROVALS` | `2` | Approvals a posting over its threshold needs (at most 10). |
| `ENTRY_APPROVERS` | `"accounting.add_entry"` | The permission whoever approves a posting holds. |
| `SUPERUSER_APPROVALS` | `0` | Approvals making a superuser needs (at most 10); 0: it is not guarded. |
| `SUPERUSER_APPROVERS` | `"auth.change_user"` | The permission whoever approves making a superuser holds. |

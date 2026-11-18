# Part 7: four eyes

In [part 6](6-moving-money.md), one person sent money alone. In this
part, a transfer over 1,000 dollars waits until another person approves
it, and that person confirms the approval with a code from their phone.
The audit trail then shows who did what.

## The problem

One person alone should not move a large amount:

- **A mistake goes out.** A clerk types 15,000 for 1,500. Nobody looks
  before the money leaves.
- **One person can steal.** A clerk who sends money to their own account
  needs nobody's help.
- **A stolen session can approve.** Asking for "someone else's OK" is
  not enough if the OK is a click. A step-up opens a window: for a few
  minutes, the session can approve anything, as often as it likes, and so
  can a copy of it. The approver should confirm this one request, as it
  was shown to them, once.
- **Nobody can tell afterwards.** When a transfer is questioned, the
  bank must show who asked for it, who approved it, and when.

Banks call the rule "four eyes", or maker-checker: the person who makes
a request (the maker) never checks it, and a checker approves the
request exactly as it was made.

## What you will build

- Transfers over 1,000 dollars that wait as an approval request
  ([approvals](../contrib/approvals.md)), and run once another person
  approves them.
- The clerk of [part 3](3-permissions.md) as the checker: allowed to
  approve, with an authenticator app.
- Each approval confirmed as one act: the request and its fingerprint,
  with a code ([authorizing one act](../auth/actions.md)).
- The trail of who submitted, approved, and made the transfer
  ([audit](../contrib/audit.md)).

## Step 1: install the apps

The clerk's authenticator app needs the `mfa` extra, which encrypts the
secrets it stores:

```bash
pip install "domainkit[mfa]"
```

Add `mfa` to the extras that `pyproject.toml` lists for domainkit too, so
that CI installs it. Then add the audit trail and approvals at the end of
`src/config/settings/base.py`. Approvals need the audit trail: every
decision is recorded.

```python title="src/config/settings/base.py"
INSTALLED_DOMAINS = [
    *INSTALLED_DOMAINS,
    "domainkit.contrib.audit",
    "domainkit.contrib.approvals",
]
# Each approval is authorized as one act, with a code.
APPROVALS = {"APPROVE_ACTION": True}
```

Mount the routes that turn on an authenticator app and confirm an act,
and the approvals' own routes. Add these lines at the end of
`src/config/urls.py`:

```python title="src/config/urls.py"
from domainkit.contrib.approvals.api import router as approvals_router
from domainkit.contrib.auth.actions import router as authorizations_router
from domainkit.contrib.auth.mfa import router as mfa_router

router.include_router(mfa_router, prefix="/auth")
router.include_router(authorizations_router, prefix="/auth")
router.include_router(approvals_router, prefix="/api/approvals")
```

## Step 2: guard large transfers

A guard takes two steps. First, mark the use case. In
`src/domains/accounts/controllers.py`, put `@requires_approval` above
`@use_case` on `transfer_account`. The function itself stays as it is:

```python title="src/domains/accounts/controllers.py"
from domainkit.contrib.approvals import requires_approval


@requires_approval("accounts.transfer")
@use_case
async def transfer_account(
    uow: AccountsUnitOfWork, account_id: UUID, payee: str, amount: Money
) -> Account:
    """Send ``amount`` to ``payee``, within the account's daily limit."""
    account = await uow.accounts.get_for_update(AccountId(account_id))
    transfer_id = account.transfer(payee=payee, amount=amount)
    await consume(
        uow,
        Movement(amount, {"bank_account": str(account.id)}),
        key=f"accounts.transfer:{transfer_id}",
    )
    return account
```

Checkers need a permission of their own. Give the account one more, in
`src/domains/accounts/domain.py`:

```python title="src/domains/accounts/domain.py"
class Account(AggregateRoot[AccountId]):
    ...

    class Meta:
        """Permissions, as Django's: approving large transfers too."""

        permissions = (("approve_transfer", "Can approve transfer"),)
```

Second, register the guard when the domain starts, with the policy that
says when a transfer needs approval and who may give it. A domain starts
in its `DomainConfig`. Until now, the accounts domain used the default
one. Make the new file `src/domains/accounts/apps.py`:

```python title="src/domains/accounts/apps.py (new file)"
"""How the accounts domain starts: its tables, its handlers, and its guard."""

from typing import Final

from domainkit.apps import DomainConfig
from domainkit.contrib.approvals import Policy, Threshold, guard

from domains.accounts import controllers
from domains.accounts.models import SqlAlchemyAccountsUnitOfWork

LARGE: Final = Threshold({"USD": "1000.00"})
"""A transfer over this waits for another person's approval."""


class AccountsConfig(DomainConfig):
    """The default start, and maker-checker on large transfers."""

    requires = ("domainkit.contrib.approvals",)

    def ready(self) -> None:
        """Map the tables, subscribe the handlers, and guard transfers."""
        super().ready()
        guard(
            "accounts.transfer",
            controllers.transfer_account,
            unit_of_work=SqlAlchemyAccountsUnitOfWork,
            policies=[
                Policy(
                    "accounts.large_transfer",
                    permission="accounts.approve_transfer",
                    when=lambda command: LARGE.exceeded_by([command.amount]),
                ),
            ],
            permission="accounts.change_account",
            submit=True,
        )
```

- `super().ready()` keeps what the default did: it maps the tables and
  subscribes the handlers of `controllers.py`.
- The policy applies to a transfer over 1,000 dollars. It needs one
  approval, by someone who holds `accounts.approve_transfer`. The maker
  never counts, whatever they hold. A transfer the policy does not apply
  to runs at once.
- `Threshold` fails closed: a currency it does not name always needs
  approval.
- `submit=True`: a large transfer sent to the route is submitted for
  approval, and answered `403 approval_pending`. Once approved, the worker
  runs it, as its maker, through the same use case: the lock, the limit,
  and the books, as in part 6.

Make the new tables, and run the checks and the tests:

```bash
python manage.py makemigrations -m "audit trail and approvals"
python manage.py migrate
python manage.py check
pytest
```

`check` prints the domains with `audit` and `approvals` among them, and
pytest still prints `20 passed`: part 6's transfers are 1,000 dollars or
less.

## Step 3: get the checker ready

```bash
python manage.py runserver
```

In the second shell, as the superuser, grant the clerk what a checker
needs: to read accounts, to approve transfers, and to read the requests.
`PUT` sets exactly these:

```bash
curl -X PUT http://127.0.0.1:8000/admin/auth/users/$CLERK/permissions -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"permissions": ["accounts.view_account", "accounts.approve_transfer", "approvals.view_approvalrequest"]}'
```

An approval is confirmed with a code, so the clerk turns on an
authenticator app. Starting it takes the clerk's password, and answers
the app's secret:

```bash
read -rsp "Clerk's password: " CLERK_PASSWORD && export CLERK_PASSWORD
CLERK_SECRET=$(python -c "import json, os; print(json.dumps({'password': os.environ['CLERK_PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/auth/mfa/totp -H "Authorization: Bearer $CLERK_TOKEN" \
    -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['secret'])")
unset CLERK_PASSWORD
```

Add `$CLERK_SECRET` to an authenticator app, such as the one on your
phone. The answer also holds `otpauth_uri`, the same secret for a QR
code. Then confirm the app with the code it shows:

```bash
read -rsp "Code: " CODE && export CODE
curl http://127.0.0.1:8000/auth/mfa/totp/confirm -H "Authorization: Bearer $CLERK_TOKEN" \
  -H "Content-Type: application/json" -d "{\"code\": \"$CODE\"}"
```

The answer lists ten recovery codes. A real clerk keeps them safe, for
the day the phone is lost.

## Step 4: make a large transfer

As the superuser, send 1,500 dollars, and keep the request's id in
`$REQUEST`:

```bash
REQUEST=$(curl -s http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/transfer -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 3e5d7a10-9b2c-4f6e-8d1a-7c4b2e9f0a63" -H "Content-Type: application/json" \
  -d '{"payee": "DE89370400440532013000", "amount": {"amount": "1500.00", "currency": "USD"}}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['approval_request_id'])")   # 403
```

The answer is `403`, with the code `approval_pending`: the transfer did
not run. It waits as a request, which `approval_request_id` names. The
request keeps the transfer exactly as it was sent.

## Step 5: approve it

The clerk reads the request, and keeps its fingerprint:

```bash
curl http://127.0.0.1:8000/api/approvals/requests/$REQUEST -H "Authorization: Bearer $CLERK_TOKEN"
FINGERPRINT=$(curl -s http://127.0.0.1:8000/api/approvals/requests/$REQUEST -H "Authorization: Bearer $CLERK_TOKEN" \
  | python -c "import json, sys; print(json.load(sys.stdin)['fingerprint'])")
```

The request shows its action (`accounts.transfer`), the transfer's
arguments, the maker's id, the policy, `"status": "pending"`, and the
`fingerprint`: the SHA-256 of the action and the arguments. An approval
states the fingerprint the checker read, so a request that changed is
never approved by mistake.

Approving takes an authorization of the act. Without one, the answer is
`401 authorization_required`:

```bash
curl http://127.0.0.1:8000/api/approvals/requests/$REQUEST/approve -H "Authorization: Bearer $CLERK_TOKEN" \
  -H "Content-Type: application/json" -d "{\"fingerprint\": \"$FINGERPRINT\"}"   # 401
```

The clerk asks for one at the route's twin, `.../approve/authorization`,
with the same body, and reads what it shows:

```bash
ASKED=$(curl -s http://127.0.0.1:8000/api/approvals/requests/$REQUEST/approve/authorization \
  -H "Authorization: Bearer $CLERK_TOKEN" -H "Content-Type: application/json" \
  -d "{\"fingerprint\": \"$FINGERPRINT\"}" \
  | python -c "import json, sys; print(json.load(sys.stdin)['id'])")
curl http://127.0.0.1:8000/auth/authorizations/$ASKED -H "Authorization: Bearer $CLERK_TOKEN"
```

`shown` is what the server built from the stored request, never words a
page sent: the title `Approve a request`, the action, the account, the
payee, and the amount. The clerk confirms it with a new code of the app,
and keeps the authorization:

```bash
read -rsp "Code: " CODE && export CODE
APPROVAL=$(curl -s http://127.0.0.1:8000/auth/authorizations/$ASKED/confirm -H "Authorization: Bearer $CLERK_TOKEN" \
  -H "Content-Type: application/json" -d "{\"code\": \"$CODE\"}" \
  | python -c "import json, sys; print(json.load(sys.stdin)['authorization'])")
```

An app shows each code for 30 seconds, and the server takes each one
once: if the code you used last is still shown, wait for the next one.
Now the clerk approves, with the authorization in the
`Action-Authorization` header:

```bash
curl http://127.0.0.1:8000/api/approvals/requests/$REQUEST/approve -H "Authorization: Bearer $CLERK_TOKEN" \
  -H "Action-Authorization: $APPROVAL" -H "Content-Type: application/json" \
  -d "{\"fingerprint\": \"$FINGERPRINT\"}"
```

The answer is the request, with `"status": "approved"` and the clerk's
decision. The approval spent the authorization: sent again, it is `403
action_not_authorized`. Stop the server.

## Step 6: run it, and read the trail

The approval queued the transfer in the outbox. Run the worker:

```bash
python manage.py runworker --once
python manage.py audit log --limit 10
```

The worker ran the transfer as its maker, and its event's handler booked
it. `audit log` prints the newest entries first. Each line is the time,
the actor (a user's id), the action, the outcome, the target, and the
request id. Read from the bottom up:

- `auth.mfa_enabled`: the clerk, whose id is `$CLERK`, turned on the app;
- `approvals.request_submitted`: the superuser sent the transfer, which
  waited;
- `auth.access` `denied`: the clerk's first try, without an
  authorization;
- `auth.action_authorized`, `approvals.approval_given`,
  `approvals.request_approved`, and `auth.action_authorization_used`: the
  clerk's approval, confirmed and spent;
- `accounts.account_transferred`, `approvals.request_executed`, and
  `accounting.entry_posted`: the transfer, run as the superuser, its
  maker.

The work the approval queued keeps the request id of the approval, so
the lines of one decision read together. In production, the trail is
sealed and anchored, so a row changed later is found
([audit](../contrib/audit.md#tamper-evidence)).

## Step 7: test it

The test helpers of `domainkit.testing` do what the clerk did: turn on
an app (`enrol_totp`), and ask for an authorization and confirm it with
a code (`authorize_act`). The server takes each code once, so the test
freezes the clock, and each code moves it on to the next 30 seconds, with
no wait. Make the new file
`tests/domains/accounts/test_large_transfers.py`:

```python title="tests/domains/accounts/test_large_transfers.py (new file)"
"""A large transfer runs once another person approves it, as one act."""

from domainkit.contrib.audit.testing import audit_entries
from domainkit.contrib.auth.testing import create_user
from domainkit.contrib.messaging.testing import Outbox
from domainkit.testing import FreezeTime, TestClient, authorize_act, enrol_totp

URL = "/api/v1/accounts/accounts"
PASSWORD = "correct-horse-battery-42"


def usd(amount: str) -> dict[str, str]:
    """``amount`` US dollars, as JSON."""
    return {"amount": amount, "currency": "USD"}


def test_a_large_transfer_waits_for_another_person(
    admin_client: TestClient,
    client: TestClient,
    outbox: Outbox,
    freeze_time: FreezeTime,
) -> None:
    """Pending; the maker cannot approve; the checker's approval runs it, once."""
    freeze_time()  # Each code moves the frozen clock to its own 30 seconds.
    item = admin_client.post(URL, json={"name": "Savings"}).headers["location"]
    admin_client.post(f"{item}/deposit", json={"amount": usd("5000.00")})
    maker = client.run(create_user, "accounts.change_account", password=PASSWORD)
    checker = client.run(
        create_user,
        "accounts.approve_transfer",
        "approvals.view_approvalrequest",
        password=PASSWORD,
    )
    as_maker = client.authenticated_as(maker)
    as_checker = client.authenticated_as(checker)
    enrol_totp(as_maker, PASSWORD)
    app = enrol_totp(as_checker, PASSWORD)

    sent = as_maker.post(
        f"{item}/transfer", json={"payee": "DE89", "amount": usd("1500.00")}
    )
    request = f"/api/approvals/requests/{sent.json()['approval_request_id']}"
    fingerprint = {"fingerprint": as_checker.get(request).json()["fingerprint"]}
    by_the_maker = as_maker.post(f"{request}/approve/authorization", json=fingerprint)
    approval = authorize_act(as_checker, f"{request}/approve", app, json=fingerprint)
    approved = as_checker.post(
        f"{request}/approve",
        json=fingerprint,
        headers={"Action-Authorization": approval},
    )
    outbox.run_queued()

    assert (sent.status_code, sent.json()["code"]) == (403, "approval_pending")
    assert by_the_maker.status_code == 403
    assert approved.json()["status"] == "approved"
    assert as_checker.get(request).json()["status"] == "executed"
    assert admin_client.get(item).json()["balance"] == usd("3500.00")
    [given] = client.run(audit_entries, action="approvals.approval_given")
    [made] = client.run(audit_entries, action="accounts.account_transferred")
    assert (given.actor, made.actor) == (str(checker.id), str(maker.id))
```

Run the tests:

```bash
pytest
```

pytest prints `21 passed`.

## What you learned

- `@requires_approval` and `guard()` hold a use case's command as a
  request when a policy applies. Once approved, the worker runs it, as
  its maker, through the same use case.
- The maker never approves their own request, and a checker approves the
  fingerprint they read.
- With `APPROVE_ACTION`, each approval is authorized as one act: the
  checker asks, reads what is shown, confirms with a code, and the
  approval spends the authorization.
- The audit trail records each step with its actor, in the transaction
  of the step.

Next, [part 8](8-telling-others.md) tells another service about each
transfer.

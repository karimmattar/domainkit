# Maker-checker: large payouts approved, each act confirmed

Clerks send payouts to suppliers. Over 10,000.00, a second person
approves the payout first, and each of them confirms, with a code, the
exact act they take.

## The problem

A clerk sends 25,000.00 to a supplier. One person should not be able to
move that much alone, so a second person approves it: four eyes, or
maker-checker. An "approve" button alone proves little:

- the session that clicks it may not be the approver's own: a stolen
  token, or a script on a page of the same site;
- what the approver read may not be what runs: the IBAN changed between
  the screen and the server;
- the clerk's session alone can send a payout the clerk never saw.

So each person confirms the act itself: this request, with these
arguments, once. And afterwards the trail must say who made the payout,
who approved it, and with what.

This example guards payouts with
[`domainkit.contrib.approvals`](../../docs/contrib/approvals.md): over
the threshold, a payout runs only once someone other than its maker
approved it. The maker confirms the request they submit, and the
approver the request they approve, with a code of their authenticator
app, bound to what they were shown: the payee, the IBAN, and the amount
([action authorizations](../../docs/auth/actions.md)). Rejecting and
cancelling stop a request at once. The audit trail names who took each
step.

## What is here

`domainkit init payoutsAPI` made the project, and `manage.py startdomain
payouts` its domain. These files were then changed:

| File | What changed |
| --- | --- |
| `src/config/settings/base.py` | The audit trail and approvals are installed; submitting and approving are acts (`APPROVALS`). |
| `src/config/settings/test.py` | The tests run on PostgreSQL when `PAYOUTS_API_TEST_DATABASE_URL` names one. |
| `src/config/urls.py` | The authenticator app's routes and the routes that confirm an act, at `/auth`; approvals at `/api/approvals`. |
| `src/domains/payouts/domain.py` | The `Payout` aggregate, its rules (an IBAN's check digits among them), and its permissions. |
| `src/domains/payouts/controllers.py` | `send_payout`, guarded twice, and the act that confirms it. |
| `src/domains/payouts/routes.py` | `POST /api/v1/payouts`, which asks for the act. |
| `src/domains/payouts/apps.py` | The guard of large payouts, and the act, registered. |
| `tests/` | The tests, and the people they act as (`tests/people.py`). |
| `pyproject.toml`, `.github/workflows/ci.yml` | mypy, strict, as `init --profile fintech` adds it; the `mfa` extra. |

## Run it

From a checkout of domainkit:

```bash
cd examples/maker-checker
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e "../..[mfa,postgres,redis,sqlite,testing]"
pip install --group dev
export PAYOUTS_API_SETTINGS_MODULE=config.settings.local
python manage.py makemigrations -m "payouts"
python manage.py migrate
pytest
```

The example holds no migrations: `makemigrations` writes them for the
domainkit you installed. In a project of your own, made from this one,
commit the migrations it writes. `init` writes a `.env` that names the
settings module, and `.env` is never committed, so the `export` line does
its job. An approved payout runs in the worker: `python manage.py
runworker` beside `runserver`.

## One use case, guarded twice

In `src/domains/payouts/controllers.py`:

```python
@requires_approval(SEND_PAYOUT)
async def send_payout(command: SendPayout, uow: PayoutsUnitOfWork) -> Payout:
    """Send the payout; over the threshold, only as an approved request's run."""
    payout = Payout.send(command.payee, command.iban, command.amount)
    async with uow:
        await uow.payouts.add(payout)
        await uow.commit()
    return payout
```

`requires_approval` refuses a direct call whose command needs approval
(`403 approval_required`). Which commands need it, and who approves them,
is the guard the domain registers when it starts. In
`src/domains/payouts/apps.py`:

```python
guard(
    controllers.SEND_PAYOUT,
    controllers.send_payout,
    unit_of_work=SqlAlchemyPayoutsUnitOfWork,
    policies=[
        Policy(
            "payouts.large_payout",
            approvals=1,
            permission="payouts.approve_payout",
            when=controllers.large,
        ),
    ],
    permission="payouts.add_payout",
    show=controllers.shown,
)
```

Over `THRESHOLD`, a payout needs one approval from a holder of
`payouts.approve_payout`, never its maker. A currency the threshold does
not name always needs approval: the control fails closed.

The threshold judges one payout at a time: the example has no cap on
what a clerk sends in a day. Three payouts of 9,999.99 each go through
without an approver. A project counts what each person moves in a window
with [`domainkit.contrib.limits`](../../docs/contrib/limits.md), which
refuses the payout that would pass the day's limit. `show` is what
an approver reads of the request, the same lines the maker confirms,
with the IBAN as banks store it:

```python
def shown(command: SendPayout) -> dict[str, str]:
    """What a person deciding on the payout reads: how much, to whom, where."""
    amount = f"{command.amount.amount} {command.amount.currency.code}"
    return {"Pay": f"{amount} to {command.payee}", "IBAN": compact_iban(command.iban)}
```

## A payout under the threshold: the maker confirms it

The payout route asks for an authorization of the act, and spends it
once. In `src/domains/payouts/routes.py`:

```python
payouts.command_route(  # POST /payouts, and its twin POST /payouts/authorization
    controllers.SendPayout,
    body=schema_for(controllers.SendPayout, write=True),
    service=requires_authorization(controllers.SEND)(controllers.send_payout),
    rule=HasPermissions("payouts.add_payout") & StepUp(for_=controllers.SEND),
)
```

`StepUp(for_=...)` refuses a request that carries no authorization of
the act. The route's twin, `POST /payouts/authorization`, builds the same
command from the same body and answers with what to confirm. The maker's
client goes round once per payout:

```http
POST /api/v1/payouts {"payee": "Acme Supplies Ltd", "iban": "DE89...", "amount": {...}}   401 authorization_required
POST /api/v1/payouts/authorization {same body}                                            201 {"id": "9f0c...", "shown": {"title": "Send a payout", "details": [...]}}
POST /auth/authorizations/9f0c.../confirm {"code": "123456"}                               200 {"authorization": "9f0c....mYd3..."}
POST /api/v1/payouts {same body}   Action-Authorization: 9f0c....mYd3...                    201
```

The authorization holds for that payee, IBAN, and amount, once, for 5
minutes. Sent again it is `403 action_not_authorized`, and so is a body
with another IBAN, which also voids it. Over the threshold the route
refuses the payout (`403 approval_required`), confirmed or not.

## A payout over the threshold: submitted, approved, run

The maker submits it to approvals, and the approver approves it. Both
steps are acts. In `src/config/settings/base.py`:

```python
APPROVALS = {"SUBMIT_ACTION": True, "APPROVE_ACTION": True}
```

```http
POST /api/approvals/requests/authorization {"action": "payouts.send_payout", "arguments": {...}}   201  (the maker)
POST /auth/authorizations/9f0c.../confirm {"code": "123456"}                                       200
POST /api/approvals/requests {same body}   Action-Authorization: ...                               201 {"id": "6c0e...", "fingerprint": "3f1b...", "status": "pending"}
POST /api/approvals/requests/6c0e.../approve/authorization {"fingerprint": "3f1b..."}               201  (the approver)
POST /auth/authorizations/4a7d.../confirm {"code": "654321"}                                       200
POST /api/approvals/requests/6c0e.../approve {"fingerprint": "3f1b..."}   Action-Authorization: ...  200 {"status": "approved"}
```

The maker's screen shows "Submit a request", the action, and the
guard's lines; the approver's shows "Approve a request" and the same
lines. The approval states the fingerprint of the arguments the approver
read, and its authorization is for that request alone: confirmed for one
request, it approves no other. The approval queues the payout in the
same transaction, and the worker sends it, as its maker, once.

`tests/people.py` takes these steps through the application's own
routes, with `domainkit.testing`'s helpers: `enrol_totp` turns an
authenticator app on, and `authorize_act` asks for an act at its twin
and confirms it with a code:

```python
def confirm(self, act: str, body: dict[str, Any]) -> dict[str, str]:
    """Ask for an authorization of the act at ``act``, confirm it with a code.

    Returns the header that sends it with the act.
    """
    authorization = authorize_act(self.api, act, self.app, json=body)
    return {"Action-Authorization": authorization}
```

## Rejecting and cancelling

An approver rejects a request with a reason
(`POST /api/approvals/requests/{id}/reject {"reason": "No such invoice."}`),
and its maker withdraws it (`.../cancel`). Either ends the request, and
nothing runs. Neither asks for a code: stopping a payout should never
wait for a second factor. `REJECT_ACTION` and `CANCEL_ACTION` would turn
them into acts too.

## The trail

Every step is an event, recorded in the audit trail with its actor. The
test of a large payout reads it back. In
`tests/domains/payouts/test_approvals.py`:

```python
assert trail == [
    {
        ("auth.action_authorized", maker.id),  # The maker confirmed the request,
        ("approvals.request_submitted", maker.id),  # submitted it,
        ("auth.action_authorization_used", maker.id),  # and so spent the act.
    },
    {
        ("auth.action_authorized", approver.id),  # The approver confirmed,
        ("approvals.approval_given", approver.id),
        ("approvals.request_approved", approver.id),  # the one approval needed,
        ("auth.action_authorization_used", approver.id),
    },
    {
        ("payouts.payout_sent", maker.id),  # And the worker sent it, as the maker.
        ("approvals.request_executed", maker.id),
    },
]
```

The test's clock is frozen, and it moves the clock on between the maker,
the approver, and the worker, so the trail reads as three moments. The
entries of one moment share it, and the trail keeps no order among them.

`python manage.py audit log` lists it, newest first.

## What the tests prove

| Test | What it shows |
| --- | --- |
| `test_a_payout_is_sent_once_its_maker_confirms_it` | Asked, shown, confirmed, and spent once. |
| `test_a_payout_is_sent_only_as_its_maker_confirmed_it` | No authorization, or another IBAN: nothing is sent. |
| `test_the_payout_route_refuses_a_large_payout` | Over the threshold, the route sends nothing. |
| `test_a_large_payout_runs_once_another_person_approves_it` | Submitted, approved, run by the worker as the maker; the trail. |
| `test_the_maker_never_approves_their_own_payout` | Holding the approvers' permission is not enough. |
| `test_an_approval_is_for_the_request_its_approver_confirmed` | An authorization approves one request. |
| `test_a_rejected_payout_is_never_sent`, `test_the_maker_withdraws_a_request` | Rejecting and cancelling end a request. |
| `test_an_iban_is_sent_as_typed_and_kept_as_banks_store_it` | An IBAN typed in groups of four is taken. |
| `tests/domains/payouts/test_model.py` | The payout's own rules, the IBAN's check digits among them. |

domainkit's CI runs them on SQLite and on PostgreSQL, with the project's
ruff, mypy, `check`, and migrations, for every change to the framework.

## Read next

- [Approvals](../../docs/contrib/approvals.md): policies, groups,
  expiry, and large ledger postings.
- [Authorizing one act](../../docs/auth/actions.md): the phone as the
  screen that confirms, and what a screen may show.
- [Payments ledger](../payments-ledger/README.md): wallets in
  double-entry books.

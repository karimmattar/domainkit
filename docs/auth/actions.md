# Authorizing one act, with its arguments

A step-up (`StepUp(max_age=...)`) opens every guarded route of a login for
a while, with any arguments, as often as the client likes. An action
authorization opens one act, with the arguments the user was shown, once.
The user confirms "End Alice's membership of Acme", and the server ends
that membership, and nothing else: a copy of the session gains nothing
from the user's approval but that act.

The design is [ADR 0144](../adr/0144-authorize-one-act-not-a-window-of-time.md):
a stored row, spent once, and a code over what was confirmed, as
[transactions](step-up.md#transactions-authorized-for-one-amount-and-payee)
are bound, for any act.

On this page you learn how to:

- declare an act, register it, and guard its route;
- authorize a `@use_case`, on one route and not another;
- ask for an authorization, confirm it, and send it with the act;
- choose what confirming proves, and when the act spends it;
- confirm another way, from an app of your own;
- authorize an act in a test.

## A first example

Declare the act: its purpose, the command its service takes, what it is
on, and what the user is shown. Decorate the service, register the
action in your domain's `ready()`, and ask for it on the route:

```python
from uuid import UUID

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import (
    Action,
    Shown,
    register_action,
    requires_authorization,
)
from domainkit.ddd import Command
from domainkit.http.access import HasPermissions


class EndMembership(Command):
    """End someone's membership of an organization."""

    organization_id: UUID
    membership_id: UUID


async def show(command: EndMembership) -> Shown:
    async with OrganizationsUnitOfWork() as uow:
        membership = await uow.memberships.get(command.membership_id)
        organization = await uow.organizations.get(command.organization_id)
    return Shown(
        "End a membership",
        {"Member": membership.display_name, "Organization": organization.name},
    )


END_MEMBERSHIP = Action(
    "organizations.end_membership",
    command=EndMembership,
    subject=lambda command: f"organizations.membership:{command.membership_id}",
    show=show,
)


@requires_authorization(END_MEMBERSHIP)
async def end_membership(command: EndMembership, uow: OrganizationsUnitOfWork) -> None:
    async with uow:
        membership = await uow.memberships.get(command.membership_id)
        membership.end()
        await uow.commit()


# In OrganizationsConfig.ready():
register_action(END_MEMBERSHIP)

memberships.command_route(
    EndMembership,
    service=end_membership,
    method="DELETE",
    rule=HasPermissions("organizations.end_membership")
    & StepUp(for_="organizations.end_membership"),
)
```

Mount the routes that confirm, next to the one that logs in:
`account_router()` mounts them (`authorizations`), or
`router.include_router(actions.router, prefix="/auth")`. The round trip,
for a web client:

```http
DELETE /organizations/7/memberships/42                       401 authorization_required
POST   /organizations/7/memberships/42/authorization         201 {"id": "9f0c...", "purpose": "organizations.end_membership",
                                                                  "shown": {...}, "expires_at": "...", "methods": ["otp"]}
POST   /auth/authorizations/9f0c.../confirm {"code": "123456"} 200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
DELETE /organizations/7/memberships/42
       Action-Authorization: 9f0c....mYd3...                 204
```

The same request again with the same header is `403
action_not_authorized`: the authorization was spent.

## Declaring the act

| `Action` argument | Meaning |
| --- | --- |
| `purpose` | A lower-case name of at most 100 characters (`organizations.end_membership`); one action per purpose. |
| `command` | The class the service takes, or the `@use_case` whose command it is (*new in 2.5*). The digest is computed from it, read back through pydantic as JSON. |
| `subject` | `command -> "<type>:<id>"`: what the act is on, an internal id of at most 200 characters (letters, digits, and `_ . : / -`), kept for the trail. |
| `show` | `command -> Shown`, a coroutine or a plain function: what the user is shown, from the command and the stored state, in the request's language. `(command, uow) -> Shown` reads in the unit of work the act runs in (*new in 2.5*, [below](#a-use-case-as-the-act)). |
| `ignore` | Fields of the command that change on every call (a time, a fresh id), left out of the digest. |
| `ttl` | How long its authorizations work, in place of `AUTH.AUTHORIZATIONS.TTL`: more than 0, 30 minutes at most. |
| `methods` | What confirming must prove, in place of `AUTH.AUTHORIZATIONS.METHODS`: `otp`, `pwd`, and `phone`, holding `otp` or `phone`. |

`Shown(title, details)` is what a screen shows: a title of at most 64
printable characters, and at most six labels and values of at most 64
each, in order (a mapping, or pairs). Anything longer is a `ValueError`:
shorten what you show. It is built when the authorization is asked for,
and kept: every screen shows the kept words, never words a page sent.

`register_action(action)` in `ready()` registers it, a hook point of
auth's ([ADR 0140](../adr/0140-auth-asks-other-apps-through-hooks.md)).
Two actions of one purpose are refused.

## A use case as the act

*New in 2.5* ([ADR 0161](../adr/0161-authorize-a-use-case-as-a-copy-and-read-what-is-shown-in-its-unit-of-work.md)).
In the simple layout, a use case is a `@use_case` in `controllers.py`,
which may not import the unit of work on the database (`models.py`).
The action lives beside it all the same: its `command` is the use case,
and its `show` takes the unit of work the act runs in, and reads
through its port:

```python
# controllers.py
from domainkit.contrib.auth.actions import Action, Shown
from domainkit.ddd import use_case


@use_case
async def end_membership(uow: OrganizationsUnitOfWork, membership_id: UUID) -> None:
    """End someone's membership of an organization."""
    membership = await uow.memberships.get(MembershipId(membership_id))
    membership.end()


async def show_ending(command, uow: OrganizationsUnitOfWork) -> Shown:
    membership = await uow.memberships.get(MembershipId(command.membership_id))
    return Shown("End a membership", {"Member": membership.display_name})


END_MEMBERSHIP = Action(
    "organizations.end_membership",
    command=end_membership,
    subject=lambda command: f"organizations.membership:{command.membership_id}",
    show=show_ending,
)
```

`requires_authorization(END_MEMBERSHIP)` given a use case returns a copy
whose `run`, and so its call, spends the authorization. The use case
itself stays as it was. Give the copy to the route that asks, and the use
case to the routes that run it without an act, such as the phone's,
where the user's own device is the proof:

```python
# routes.py
memberships.use_case(
    requires_authorization(controllers.END_MEMBERSHIP)(controllers.end_membership),
    method="DELETE",
    rule=StepUp(for_=controllers.END_MEMBERSHIP),
)
phone_memberships.use_case(controllers.end_membership, method="DELETE")
```

- **The twin lends the route's unit of work.** A `show` with a
  parameter named `uow` is given it open, as one whose `commit` is
  refused, and it is left without a commit: `show` only reads. A block
  `async with uow:` in it enters nothing. A `ResourceRouter`'s twin
  gives its route's; a route of your own names the class, and a new one
  is made: `request_action_authorization(request, END_MEMBERSHIP,
  command, unit_of_work=OrganizationsUnitOfWork)`. Without it, asking is
  refused with `ImproperlyConfigured`.
- **The route that asks runs the copy.** A `ResourceRouter` refuses,
  when it declares the route, one that asks `StepUp(for_=...)` and runs
  the use case itself, or a service that spends another act's
  authorization: it would run the act with arguments nobody confirmed. A
  service left undecorated warns (`DomainkitDeprecationWarning`): it
  may call one that is decorated, as in 2.4, and 3.0 refuses it.
- **One command.** A use case is guarded only for its own command, not
  for a class it derives from (`ImproperlyConfigured`).
- **A `show` of the command alone**, or with parameters of its own
  (`language="en"`), is called as before, and opens what it reads
  itself.

## The route asks: `StepUp(for_=...)`

`StepUp(for_=...)` takes the `Action`, or its purpose, in place of
`max_age`; never both, and no `methods`: the action names them. It is an
`ActionStepUp`, a rule like any other, combined with `&`.

- No user, or staff acting as the user (an impersonation): refused, as
  `StepUp` refuses.
- No `Action-Authorization` header: `401 authorization_required`. The
  `WWW-Authenticate` header says so as RFC 9470 writes it, with the
  purpose; the body names the purpose too, as `errors[0].msg`.
- A header that does not hold: `403 action_not_authorized`, which never
  says why: unknown, another user's or another login's, for another
  purpose, unconfirmed, spent, expired, or a code that does not cover the
  row.
- Otherwise the rule allows, after one query by id, and keeps the
  authorization on the request for the act. It writes nothing: a rule may
  be asked more than once, beside other rules, and a later one may refuse.

A `ResourceRouter` gives each command route that asks a twin, `POST
<the route's path>/authorization`, which builds the command as the route
does and answers `201` with the authorization: its `id`, `purpose`,
`shown`, `expires_at`, and `methods`, what confirming it must prove
(*new in 2.5*). A page reads `methods` to offer the ways that may
confirm it, a code, the phone, or a passkey, without asking `GET
/auth/authorizations/{id}` again. A route of your own gets a twin of
your own, that builds the command and calls
`request_action_authorization`:

```python
from fastapi import Request

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.contrib.auth.actions import (
    ActionAuthorizationOut,
    request_action_authorization,
)


@router.post("/memberships/{membership_id}/end/authorization", status_code=201)
async def ask_to_end(
    membership_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    command = EndMembership(organization_id=ORGANIZATION, membership_id=membership_id)
    asked = await request_action_authorization(request, END_MEMBERSHIP, command)
    return ActionAuthorizationOut.of(asked)
```

Asking needs credentials without scopes, which can be told apart from the
user's others: a login token, a browser session, or a JSON Web Token login
(`400 step_up_unsupported` otherwise). It needs a user with a way to
confirm what the act asks: a confirmed authenticator app for a code, or,
with [devices](devices.md#confirming-one-act), a bound device or a
passkey (`409 no_second_factor` otherwise). A login holds at most ten
unconfirmed authorizations: asking for an eleventh ends the oldest.

`check` refuses `StepUp(for_=...)` on a WebSocket or an event stream
(`auth.E111`): a connection is asked again and again, and has no act to
spend it on.

## Confirming it

`GET /auth/authorizations/{id}` shows an authorization to the login that
asked: `{"id", "purpose", "shown", "expires_at", "confirmed", "methods"}`.
Another login, even the user's, gets `404`.

`POST /auth/authorizations/{id}/confirm` takes `{"code"}`, and
`"password"` where the act asks `pwd`, as a transaction is confirmed: wrong
codes count against `AUTH.MFA.RATE`, wrong passwords against
`AUTH.LOGIN_RATE`. It answers `{"authorization": "<id>.<code>",
"expires_at"}`, for the act's `Action-Authorization` header.

What confirming must prove is the action's `methods`, or
`AUTH.AUTHORIZATIONS.METHODS` (`["otp"]`). A bound device's key (`hwk`)
and a passkey (`mfa`) stand for `otp`, as for a step-up. `phone` is a
confirmation on a bound device's own screen: the phone's approval, or the
app's own act. A code never proves it, nor does a passkey; `["phone"]`
takes those two only. A code where the act asks the phone is `409
authorization_needs_other_factors`.

Only the phone has a screen the session does not control. A code proves
the user is there, and binds the confirmation to this authorization; what
the user read was on the page.

### On the phone

With [sign-in approvals](ciba.md) installed, the user's phone confirms
an authorization of the session's: the page asks it, the phone shows the
act as the server keeps it and signs its digest, and the page redeems
the authorization ([Confirming one act](ciba.md#confirming-one-act)):

```http
POST /auth/app-sign-in/authorizations/9f0c...            200 {"request": "kq3D1...", "expires_in": 120,
                                                              "interval": 5, "match_code": "47"}
POST /auth/app-sign-in/authorizations/token {"request"}  400 authorization_pending, then
                                                         200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
```

- The request is counted and bounded as the phone's step-up is
  (`START_RATE` per user, `MAX_PENDING`, `NUMBER_MATCHING`, `409
  phone_unavailable`), and only the session that asked redeems it, or
  withdraws it (`POST /auth/app-sign-in/authorizations/cancel
  {"request"}`, since 2.5): unanswered, the phone no longer lists it,
  and the authorization stays to be confirmed; approved, it stays to be
  redeemed. A user's unanswered asks of the last `REQUEST_TTL`,
  withdrawn ones too, are at most twice `MAX_PENDING`.
- The phone lists it with an `authorization` object: the purpose, the
  kept title and details, what confirming it must prove (`methods`),
  and the digest, read from the row.
- It approves with the digest, in a request its device signs: another
  digest denies it (`409 authorization_changed`). The row keeps the
  device, its key, the bytes it signed, its signature, and when.
- It proves `hwk` and `phone`, for every user with a bound device: an
  act whose methods are `["phone"]` takes it, and asking needs no
  authenticator app.

### The mobile app's own act, and a passkey

With `domainkit.contrib.devices` installed, two more ways confirm an
authorization ([Confirming one act](devices.md#confirming-one-act)):

- **The mobile app's own act**: `POST /auth/devices/authorizations/{id}`
  `{"digest"}`, signed by one of the user's bound devices, for an act the
  app makes itself and shows itself. The authorization must be the app's
  own login's, the login that asked and spends it: another session's is
  `404`, and goes to the phone. A digest other than the act's is `409
  authorization_changed`. It proves `hwk` and `phone`, and the device's
  key, what it signed, and its signature are kept on the row, as the
  phone's approval is.
- **A passkey**: `POST /auth/passkeys/authorizations/{id}/options`
  answers a challenge made for this authorization, linked to its row, and
  `POST /auth/passkeys/authorizations/{id}` takes the assertion. Only the
  session that asked uses either; the challenge works once, for this
  authorization alone. It proves `hwk` and `mfa`, never `phone`.

Each answers `{"authorization": "<id>.<code>", "expires_at"}`, as a code
does.

## The act spends it

The decorated service takes the authorization the rule kept, computes the
digest and the subject again from the command it is given, and spends it:

- **With the act**, when the service's unit of work is on auth's
  database: the spend joins its transaction and commits with its first
  commit, with `ActionAuthorizationUsed` among its events. It is spent if
  and only if the act commits, so an act refused on the way (`409
  concurrent_update`, a business rule) can be sent again, until the
  authorization expires.
- **Before the act**, otherwise: in a transaction of its own, just before
  the service runs. At most once: an act that then fails needs a new
  authorization.

Around it:

- **Other arguments, or another subject**, void the authorization, in a
  transaction of its own, and the act is refused (`403
  action_not_authorized`). Fields of `ignore` may differ.
- **A decorated service called with no authorization for its purpose**
  (from another route, a batch, a task) raises `AuthorizationRequired`
  (401): the act is the user's own. Code that acts for the system calls a
  service of its own.
- **`spend_authorization(uow, action, command)`** does the same in code
  that is not a service: in the open transaction of `uow` on auth's
  database, else now, alone.
- **An authorization the rule accepted that no service spent** is voided
  when a response that succeeded starts, and logged on the security log
  (`auth.authorization_unspent`, with the route). Under `DEBUG` and in
  tests, the request fails (`500 authorization_unspent`), so a forgotten
  decorator shows in your tests. A response that failed (a body that does
  not validate, another rule's refusal) leaves it: no act ran.

`check` refuses a route that asks for a purpose no registered action has
(`auth.E109`), or whose action no decorated service spends (`auth.E110`).

## How it is bound

- **The row**: `domainkit_authorizations`, on auth's database. Its id is
  a random nonce; it keeps the user, the login's digest, the purpose, the
  subject, the act's digest, what is shown, when it was asked and expires,
  when it was confirmed and with what (`amr`), the confirming device with
  its key, what it signed, and its signature, when it was spent, and the
  links to the phone's request and to a passkey's challenge.
- **The digest**: `domainkit.ddd.fingerprint(purpose, arguments)`, the
  SHA-256 of `{"action": <the purpose>, "arguments": ...}` as canonical
  JSON: the function approvals computes its fingerprints with.
- **The code**: an HMAC-SHA256, under a key derived from `SECRET_KEY` for
  `auth.authorization` (its fallbacks verify), of a JSON array of
  `domainkit.authorization.v1`, the id, the user, the login's digest, the
  purpose, the subject, the digest, the SHA-256 of what was shown, and the
  expiry. It is never stored, and a row changed after its confirmation
  fails it.
- **Once**: one `UPDATE ... WHERE consumed_at IS NULL` spends it. Of two
  acts at once, one wins.
- **The login**: only the login that asked spends it, sees it, and
  confirms it with a code. Another login of the same user cannot.
- **Expiry**: `AUTH.AUTHORIZATIONS.TTL` (5 minutes, 30 at most), from
  asking to spending.

## The trail, privacy, and the purge

- `auth.action_authorized` (`ActionAuthorized`) when one is confirmed, and
  `auth.action_authorization_used` (`ActionAuthorizationUsed`) when one is
  spent: the user, the authorization, the purpose, the subject, the
  digest, the methods, and the device. The digest lets an auditor match an
  act to its authorization.
- Refusals are `auth.authorization`, with `invalid_authorization`,
  `invalid_code`, `changed`, `used`, or `unspent`.
- Neither the arguments nor what was shown reach the trail: either may be
  personal data.
- Authorizations past their expiry are purged hourly and by
  `cleartokens`. Deleting or erasing a user deletes theirs, and `privacy
  export` lists them: the purpose, the subject, and the times.

## Approvals: authorizing each approval

[Approvals](../contrib/approvals.md) asks its approvers for a step-up, if
anything. With `APPROVALS["APPROVE_ACTION"]` on, approving a request asks
for an authorization of approvals' own action, `approvals.approve`, in
place of the step-up window:

- its arguments are the decision (`ApproveRequest`): the request's id,
  the approver, and the fingerprint they state. Its subject is
  `approvals.request:<id>`;
- what is shown is built from the stored request: "Approve a request",
  the action, and the arguments by name, each cut to 64 characters, six
  values in all at most;
- the approve route's twin, `POST /requests/{id}/approve/authorization`,
  asks for one, and refuses what approving would refuse: the maker, a
  request no longer pending, a fingerprint that is not the request's;
- `approve_request` spends it, with the approval's commit where the
  requests share auth's database.

The other approve rules still apply; one that holds a `StepUp` is
refused by `check` (`approvals.E001`). See
[Approving with an authorization](../contrib/approvals.md#approving-with-an-authorization).

## For other apps: confirming another way

An app that confirms its own way (the phone of
[sign-in approvals](ciba.md), a passkey of [devices](devices.md)) uses
`domainkit.contrib.auth.integration`:

| Name | What it does |
| --- | --- |
| `on_action_confirmation(ActionConfirmation(name, methods, available))` | Registers a way to confirm, in `ready()`: what it proves (`frozenset({"hwk", "phone"})`), and `available(user_id)`, whether a user has it now. Asking needs one the user has; `check` refuses an act none proves (`auth.E108`). Auth registers the code. |
| `action_authorization(authorization_id, user_id, *, login=None)` | The authorization, to show it: its purpose, `shown`, digest, expiry, user, and login's digest. `AuthorizationNotFound` (404) for none, another user's, or another login's. |
| `link_action_authorization(authorization_id, *, user_id, sign_in_request_id=None, passkey_challenge=None)` | Links what will confirm it: the phone's request, a passkey's challenge. |
| `linked_action_authorization(sign_in_request_id, user_id, *, login=None)` | The authorization a phone's request was linked to, as `action_authorization` answers it. |
| `confirm_action_with(authorization_id, methods, *, user_id, login=None, digest=None, device_id=None, algorithm=None, public_key=None, signature=None, signed=None, signed_at=None)` | Confirms it with what the app proved, and returns `<id>.<code>`. `digest` must be the act's (`409 authorization_changed`); `methods` must prove what it asks (`409 authorization_needs_other_factors`). The device's key, what it signed, and its signature are kept on the row, and with `domainkit.contrib.evidence` installed, on the evidence ledger, which outlives the row. |
| `confirmed_action_authorization(authorization_id, user_id, *, login=None)` | `<id>.<code>` of a confirmed authorization, unspent and live, for an app that confirms in one request and hands it over in another (the phone's token route). It is never stored: computed again from the row. |
| `login_digest(credentials)` | The login's digest, as the row and the phone's requests keep it. |
| `action_for(purpose)` | The registered `Action`; its `required()` is what confirming must prove. |
| `register_action(action)` | Registers an `Action`, in `ready()`. |

## Options

| `AUTH` key | Default | What it does |
| --- | --- | --- |
| `AUTHORIZATIONS.TTL` | 5 minutes | How long an authorization works, from asking to spending; 30 minutes at most. |
| `AUTHORIZATIONS.METHODS` | `["otp"]` | What confirming one proves, unless its action names others: `otp`, `pwd`, `phone`. |
| `MFA.RATE` | `"10/hour"` | Wrong codes per user, confirmations included. |
| `LOGIN_RATE` | `"10/minute"` | Wrong passwords, where the act asks `pwd`. |

[The settings tables](../auth.md#settings) give the full rows.

## Errors and refusals

| Status and code | When |
| --- | --- |
| `401 authorization_required` | The route asks `StepUp(for_=...)` and the request sent no `Action-Authorization`; a decorated service called with none (`AuthorizationRequired`). |
| `403 action_not_authorized` | Any authorization that does not hold for this act, these arguments, and this login. |
| `400 step_up_unsupported` | Asking with credentials limited to scopes, or that cannot be told apart. |
| `409 no_second_factor` | Asking, for a user with no way to confirm what the act asks. |
| `409 authorization_confirmed`, `409 authorization_expired` | Confirming twice, or one ended; confirming too late. |
| `409 authorization_needs_other_factors` | A code or a passkey where the act asks the phone; the phone asked for an act that asks the password. |
| `409 authorization_changed` | The phone, or another app (the app's own act), confirms a digest other than the act's. |
| `404 authorization_not_found` | Another user's or another login's authorization. |
| `401 invalid_code`, `400 password_required`, `400 wrong_password`, `429` | As for a step-up. |
| `500 authorization_unspent` | Under `DEBUG` and in tests: a rule accepted one, and no service spent it. |

## Checks

| Id | What it refuses |
| --- | --- |
| `auth.E108` | An action whose methods no installed way to confirm proves (`["phone"]` with neither the phone's app nor devices). |
| `auth.E109` | A route that asks `StepUp(for_=...)` for a purpose no registered action has. |
| `auth.E110` | A route whose action no service decorated `requires_authorization` spends. |
| `auth.E111` | `StepUp(for_=...)` on a WebSocket or an event stream. |

## Testing

*New in 2.5.* `domainkit.testing` authorizes an act through the
application's own routes, as `confirm_sca` confirms a transaction
([Moving money](../testing.md#moving-money-second-factors-approvals-the-trail-key-services)):

```python
from domainkit.testing import authorize_act, enrol_totp

app = enrol_totp(as_alice, PASSWORD)
act = f"/api/organizations/7/memberships/{membership_id}"
authorization = authorize_act(as_alice, act, app)  # the twin, then a code
as_alice.delete(act, headers={"Action-Authorization": authorization})
```

- `authorize_act(client, url, app=None, *, json=None, password=None,
  prefix="/auth")` asks `POST <url>/authorization` with the act's body,
  and confirms the answer.
- `confirm_authorization(client, authorization_id, app=None, *,
  password=None, prefix="/auth")` confirms one asked elsewhere.
- With `app`, a code of the authenticator confirms it, and `password`
  where the act asks it. Without, auth confirms it in the test as
  proving the act's `methods`: what the phone or a passkey proves, for
  an act a code cannot confirm. Only under `ENVIRONMENT = "test"`; the
  client must be entered (the `client` fixture is).
- Asking is not skipped: the user needs a way to confirm what the act
  asks (`409 no_second_factor` otherwise), an authenticator app
  (`enrol_totp`), or for the phone a bound device.

## Common mistakes

- **The guarded copy on a route that does not ask.** Every call is
  refused `401 authorization_required`, and there is no twin to ask at.
  Put `StepUp(for_=...)` on the route that runs the copy.
- **The guarded copy where the use case should run unguarded**, or the
  reverse: the phone's route and tasks take the use case itself.
- **A `show` that puts more on the phone than the user needs to decide.**
  It may be personal data, and the user reads less of it.
- **An `ignore` list that unbinds what matters.** Leave out only what
  changes on every call.
- **A service that commits twice.** It spends with the first commit.
- **Spending in a route rather than in the service.** A rule that spent
  would burn the authorization when a later rule refuses, or the act fails.

## See also

- [Stepping up](step-up.md): the window, for acts whose arguments do not
  matter, and transactions authorized for one amount and payee.
- [Sign-in approvals](ciba.md): the user's phone.
- [Approvals](../contrib/approvals.md): someone else agrees, later; with
  `APPROVE_ACTION`, each approval is authorized as one act.

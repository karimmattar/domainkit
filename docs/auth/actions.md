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
- ask for an authorization, confirm it, and send it with the act;
- choose what confirming proves, and when the act spends it;
- confirm another way, from an app of your own.

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
                                                                  "shown": {...}, "expires_at": "..."}
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
| `command` | The class the service takes. The digest is computed from it, read back through pydantic as JSON. |
| `subject` | `command -> "<type>:<id>"`: what the act is on, an internal id of at most 200 characters (letters, digits, and `_ . : / -`), kept for the trail. |
| `show` | `command -> Shown`, a coroutine or a plain function: what the user is shown, from the command and the stored state, in the request's language. |
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
does and answers `201`. A route of your own gets a twin of your own, that
builds the command and calls `request_action_authorization`:

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
confirm what the act asks: a confirmed authenticator app for a code
(`409 no_second_factor` otherwise). A login holds at most ten unconfirmed
authorizations: asking for an eleventh ends the oldest.

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

## For other apps: confirming another way

An app that confirms its own way (the phone of
[sign-in approvals](ciba.md), a passkey of [devices](devices.md)) uses
`domainkit.contrib.auth.integration`:

| Name | What it does |
| --- | --- |
| `on_action_confirmation(ActionConfirmation(name, methods, available))` | Registers a way to confirm, in `ready()`: what it proves (`frozenset({"hwk", "phone"})`), and `available(user_id)`, whether a user has it now. Asking needs one the user has; `check` refuses an act none proves (`auth.E108`). Auth registers the code. |
| `action_authorization(authorization_id, user_id, *, login=None)` | The authorization, to show it: its purpose, `shown`, digest, expiry, user, and login's digest. `AuthorizationNotFound` (404) for none, another user's, or another login's. |
| `link_action_authorization(authorization_id, *, user_id, sign_in_request_id=None, passkey_challenge=None)` | Links what will confirm it: the phone's request, a passkey's challenge. |
| `confirm_action_with(authorization_id, methods, *, user_id, login=None, digest=None, device_id=None, algorithm=None, public_key=None, signature=None, signed=None, signed_at=None)` | Confirms it with what the app proved, and returns `<id>.<code>`. `digest` must be the act's (`409 authorization_changed`); `methods` must prove what it asks (`409 authorization_needs_other_factors`). The device's key, what it signed, and its signature are kept on the row, and with `domainkit.contrib.evidence` installed, on the evidence ledger, which outlives the row. |
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
| `409 authorization_needs_other_factors` | A code where the act asks the phone. |
| `409 authorization_changed` | Another app confirms a digest other than the act's. |
| `404 authorization_not_found` | Another user's or another login's authorization. |
| `401 invalid_code`, `400 password_required`, `400 wrong_password`, `429` | As for a step-up. |
| `500 authorization_unspent` | Under `DEBUG` and in tests: a rule accepted one, and no service spent it. |

## Checks

| Id | What it refuses |
| --- | --- |
| `auth.E108` | An action whose methods no installed way to confirm proves (`["phone"]` without the phone's app). |
| `auth.E109` | A route that asks `StepUp(for_=...)` for a purpose no registered action has. |
| `auth.E110` | A route whose action no service decorated `requires_authorization` spends. |
| `auth.E111` | `StepUp(for_=...)` on a WebSocket or an event stream. |

## Common mistakes

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
- [Approvals](../contrib/approvals.md): someone else agrees, later.

# 0144. Authorize one act, not a window of time

- Status: Accepted; amended 2026-11-16 (at the end); amended by [0162](0162-twins-name-their-method-and-skip-the-acts-own-dependencies.md), [0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md), [0164](0164-let-the-asker-withdraw-a-phone-request-count-sessions-per-user-and-word-each-kind.md), [0173](0173-show-an-act-whole-or-refuse-it.md) and [0174](0174-bind-an-act-to-the-record-it-decides-on-and-keep-a-held-acts-authorization.md)
- Date: 2026-11-15

## Context

A project that runs an identity service on the framework guards its
sensitive changes with `StepUp(max_age=timedelta(minutes=5))`, and steps
its web sessions up from the user's phone. It reported that what the
user approves and what the approval opens are two different things (user
report A6).

### What exists

- **`StepUp(max_age=...)`**
  ([ADR 0122](0122-step-up-in-a-rule-and-bind-authorizations-with-a-stored-row-and-a-code.md))
  checks two things of the credentials: what their last step-up proved
  (`methods`), and how long ago (`max_age`). The step-up is kept per
  login token or browser session, in `domainkit_step_ups`. It names no
  act. One step-up opens every route behind `StepUp`, for that login,
  with any arguments, as often as the client likes, until `max_age`
  passes.
- **The phone's step-up**
  ([ADR 0129](0129-complete-the-phone-as-identity.md)):
  `POST /app-sign-in/step-up` takes `{"message"}`, at most 64 printable
  characters that the page chose, and the phone shows them as
  `binding_message`. Once approved, the session is stepped up (`amr`
  `["hwk"]`): the same window. Nothing ties the words to what the session
  does next. The phone says "Add payee DE89…", and the server allows five
  minutes of every guarded change.
- **Transactions** (`auth.sca`, ADR 0122, and
  [Transactions authorized for one amount and payee](../auth/step-up.md#transactions-authorized-for-one-amount-and-payee)):
  the one place where an approval is bound to an act. A row holds the
  user, the amount, the payee, and the purpose. Confirming it returns
  `<id>.<code>`, an HMAC of those values, and the use case spends it once,
  on those values only. The phone confirms one with the amount and payee
  read from the row, never from the page
  ([Asking the phone from a signed-in session](../auth/ciba.md#asking-the-phone-from-a-signed-in-session)).
  It is built for money: two fixed fields, a `Money` and a payee.
- **Approvals**
  ([ADR 0121](0121-hold-guarded-commands-as-sealed-requests-and-count-approvals-under-the-requests-lock.md)):
  other people, never the maker, approve a held command by its
  fingerprint (the SHA-256 of the action and its arguments as canonical
  JSON), and the worker runs it later. It answers "someone else agrees",
  not "the user confirms what they are doing now". Its own approvers step
  up with `StepUp(max_age=...)`
  ([Who may approve](../contrib/approvals.md#who-may-approve-step-up)).

### What a copied session can do today

Whoever holds a copy of a signed-in session (its cookies, taken from a
device, a proxy, or a log; or a login token) is that session. The copy
can:

- **Ride the user's step-up.** The step-up is kept by the session's
  digest, so the copy is stepped up when the user is. For `max_age` after
  the user approves one act on the phone, the copy may make every guarded
  change, with arguments of its own.
- **Ask the phone in words of its own.** `POST /app-sign-in/step-up` with
  "Confirm your sign-in" reaches the user's phone. The phone shows the
  copy's address and browser, and with `NUMBER_MATCHING` the user must
  type the two digits the copy's page shows. So the copy needs the user's
  help: a call or a message that asks for the number, as push approvals
  are abused ([ADR 0137](0137-a-new-phone-asks-and-the-bound-one-approves.md)).
  One such approval opens the window.
- **Not move money.** A transaction's authorization holds for its amount
  and payee only, and the phone shows both from the server's row.

Binding the step-up to the credentials, as today, does not help: the copy
is the credentials. What helps is to bind the approval to the act and its
arguments, and to spend it once.

### What others do

- **PSD2's dynamic linking** [1]: the payer is shown the amount and the
  payee, the authentication code is specific to both, and any change
  makes it fail. `auth.sca` does this for payments.
- **OAuth's Rich Authorization Requests** (RFC 9396) [2] carry what is
  authorized as typed data (`authorization_details`, each with a `type`
  and the fields of that type), not as a scope string, so that the server
  can show it to the user and bind the grant to it. The RFC's own example
  is a payment's initiation.
- **The step-up challenge** (RFC 9470) [3], which `StepUp` follows, asks
  for an authentication level or a recent authentication (`acr_values`,
  `max_age`): a window of time, by design.
- **CIBA's `binding_message`** [4] interlocks two devices for one request:
  words both screens show, not a description the server stands behind.
  The phone's step-up borrowed it.
- **WebAuthn Level 1** [5] defined `txAuthSimple` and `txAuthGeneric`, for
  the authenticator itself to show a text and sign it. No browser
  implemented them, and Level 2 [6] dropped them. A passkey can still sign
  a challenge made for one act; showing the act is left to the page.

### Options

| Option | What it binds | What it costs |
| --- | --- | --- |
| A shorter `max_age` (60 seconds) | Nothing more | A prompt for nearly every act, and the window is still open to the copy. |
| A window per purpose: `StepUp(max_age=..., purpose=...)` | The kind of act | Every act of that kind, with any arguments, as often as wanted, until the window passes. |
| Words built by the server for the phone's step-up, the window kept | What the user reads | The phone tells the truth about the first act; the window still opens the others. |
| Approvals for the user's own acts | The command, by its fingerprint | The act runs later, through the outbox, by the worker; the maker may not approve; a request per act, and messaging. Built for a second person. |
| A signed token over the arguments, nothing stored (ADR 0122's design A) | The arguments | Not once: 20 of 20 concurrent uses won in ADR 0122's proof. |
| A rule that spends the authorization (the report's `StepUp(for_=...)` alone) | The kind of act, once | A rule runs before the body becomes the command, outside the act's transaction, and beside the rules it is combined with. It would hash what it parsed, not what the act does; burn the authorization when another rule refuses; and could not commit with the act. |
| **An authorization per act: a purpose, a subject, and a digest of the arguments, built and shown by the server, asked for by a rule, spent once by the act** | The act and its exact arguments, once | A request and a confirmation per act; a new table; one more round trip for clients; each act declares what it shows. |

## Decision

Build action authorizations: ADR 0122's design C (a stored row, spent
once, and an HMAC over what was confirmed) for any act, in
`domainkit.contrib.auth.actions`. A rule on the route asks for one; the
act spends it.

### A domain declares the act

```python
from uuid import UUID

from domainkit.contrib.auth.actions import Action, Shown, requires_authorization
from domainkit.ddd import Command


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
async def end_membership(
    command: EndMembership, uow: OrganizationsUnitOfWork
) -> None: ...
```

- `Action(purpose, *, command, subject, show, ignore=(), ttl=None, methods=None)`:
  - `purpose`: a lower-case name, as a transaction's purpose is; one
    action per purpose.
  - `command`: the type the service takes. The digest is computed from it.
  - `subject`: what the act is on, `"<type>:<id>"`, from the command, at
    most 200 characters. It is an internal id
    ([ADR 0018](0018-evidence-subjects-are-internal-ids.md)), kept for the
    trail and for listing.
  - `show`: what the user is shown, built from the command and the stored
    state.
  - `ignore`: fields of the command that change on every call (a time, a
    fresh id), left out of the digest.
  - `ttl` and `methods`: in place of `AUTH.AUTHORIZATIONS.TTL` and
    `METHODS`, for this act.
- `register_action(END_MEMBERSHIP)` in `ready()`: a hook point of auth's
  ([ADR 0140](0140-auth-asks-other-apps-through-hooks.md)).
- `check` refuses a route that asks for a purpose no registered action
  has, or whose action no decorated service spends.

### The route asks: `StepUp(for_=...)`

```python
memberships.command_route(
    EndMembership,
    service=end_membership,
    method="DELETE",
    rule=HasPermissions("organizations.end_membership")
    & StepUp(for_="organizations.end_membership"),
)
```

- `StepUp(for_=...)` takes a purpose, or the `Action`. It takes `for_` or
  `max_age`, not both.
- It refuses what `StepUp` refuses today: no user, and staff acting as
  the user.
- A request without an `Action-Authorization` header (`<id>.<code>`,
  [below](#the-binding)) is refused with
  `401 authorization_required`, the purpose in the body, and RFC 9470's
  `error="insufficient_user_authentication"` in `WWW-Authenticate`.
- A header that does not hold is refused with `403
  action_not_authorized`, which says nothing of why: unknown, another
  user's or another login's, for another purpose, unconfirmed, spent,
  expired, or a code that does not cover the row.
- Otherwise the rule allows, after one query by id, and keeps the
  authorization on the request for the act. It writes nothing: a rule may
  be asked more than once, beside other rules.
- `check` refuses it on a WebSocket or an event stream: a connection is
  asked again and again, and has no act to spend it on.

### Asking for one: the server builds it from the act

`request_action_authorization(request, action, command)` stores an
authorization for the request's user and login, and builds each part
itself:

- **the subject**, from `action.subject(command)`;
- **the digest**: the SHA-256 of `{"action": <the purpose>, "arguments": ...}` as
  canonical JSON, the arguments being the command read back through
  pydantic, less `ignore`. It is approvals' `fingerprint` (ADR 0121),
  moved to the core so that both apps compute it one way;
- **what is shown**: `action.show(command)` returns `Shown(title,
  details)`, a title of at most 64 printable characters and at most six
  label and value pairs of at most 64 each. It is built when asked, in
  that request's language, and kept: every screen shows the kept words,
  never words the page sent;
- **the login**: the digest of the request's credentials, as the phone's
  step-up keeps it.

A `ResourceRouter` gives each command route that asks `StepUp(for_=...)`
a twin, `POST <the route's path>/authorization`. The twin takes the same
body and path, builds the command as the act's route does (`provide`
included), asks the act's rules (with `StepUp(for_=...)` passing), and
answers `201 {"id", "purpose", "shown", "expires_at"}`. A plain route's
twin is the project's, and calls the function. Asking needs credentials
without scopes, which can be told apart (else `400 step_up_unsupported`,
as for a step-up), and a user with something to confirm with (else `409
no_second_factor`). A login holds at most ten unconfirmed authorizations;
asking for an eleventh ends the oldest.

The round trip, for a web client:

```http
DELETE /organizations/7/memberships/42                          401 authorization_required
POST   /organizations/7/memberships/42/authorization            201 {"id": "9f0c...", "shown": {...}}
POST   /auth/app-sign-in/authorizations/9f0c...                 200 {"request", "expires_in", "interval", "match_code"}
POST   /auth/app-sign-in/authorizations/token {"request"}       200 {"authorization": "9f0c....mYd3...", "expires_at"}
DELETE /organizations/7/memberships/42
       Action-Authorization: 9f0c....mYd3...                    204
```

### Confirming it: the phone, a code, or a passkey

Confirming must prove the action's `methods`, or else
`AUTH.AUTHORIZATIONS.METHODS` (`["otp"]` by default, which `hwk` and
`mfa` stand for, as for a step-up). They may also name `phone`: confirmed
on a bound device's own screen, the phone's approval or the app's own
act, which neither a code nor a passkey proves. `["phone"]` takes those
two only.

- **The phone** (`domainkit.contrib.ciba`):
  `POST /app-sign-in/authorizations/{id}` asks it, as a request of kind
  `authorization`, counted and bounded as a step-up is (`START_RATE`,
  `MAX_PENDING`, `NUMBER_MATCHING`, `409 phone_unavailable`). The phone
  lists it with an `authorization` object: the purpose, the kept title
  and details, read from auth's row, and the digest. It approves with
  `POST /app-sign-in/requests/{id}/approve` `{"match_code"?, "digest"}`
  (the digest for this kind only), signed by its device, so the signature
  covers the digest the phone was given. Another digest denies the
  request (`409 authorization_changed`). The row keeps the device, its
  public key, the time it signed, and its signature, so that the approval
  can be checked again once the device is unbound; with the evidence
  ledger installed ([ADR 0011](0011-immudb-ledger-for-evidence.md)), they go into it
  too, which outlives the row. Then
  `POST /app-sign-in/authorizations/token` `{"request"}` answers
  `<id>.<code>`, to the session that asked only. It proves `hwk` and
  `phone`.
- **The mobile app's own act**: `POST /devices/authorizations/{id}`
  `{"digest"}`, signed by a bound device, for an act the app makes itself
  and shows itself. It proves `hwk` and `phone`, and is kept as the
  phone's approval is.
- **A code**: `POST /auth/authorizations/{id}/confirm` `{"code",
  "password"?}`, as a transaction is confirmed, counted against
  `AUTH.MFA.RATE` and `AUTH.LOGIN_RATE`. It proves `otp`, and `pwd` with
  the password.
- **A passkey**: `POST /passkeys/authorizations/{id}/options` answers a
  WebAuthn challenge made for this authorization (the row keeps it), and
  `POST /passkeys/authorizations/{id}` takes the assertion. It proves
  `hwk` and `mfa`.
- `GET /auth/authorizations/{id}` shows it to the session that asked; the
  code and passkey pages show the same kept words.
- `confirm_action_with(authorization, methods)` in `auth.integration`, as
  `confirm_transaction_with`, for an app that proves a factor auth does
  not check itself.

Of these, only the phone has a screen the session does not control. A
code or a passkey proves the user is there, and binds the confirmation to
this authorization; what the user read was on the page.

### The binding

- **The row**: `domainkit_authorizations`, on auth's database: the id (a
  random nonce), the user, the login's digest, the purpose, the subject,
  the digest, what is shown, when it was asked, when it expires, when it
  was confirmed and with what (`amr`), the device and its signature, when
  it was spent, and the links to the phone's request and to a passkey's
  challenge.
- **The code**: confirming returns `<id>.<code>`, the code an
  HMAC-SHA256, under a key derived from `SECRET_KEY` for
  `auth.authorization`, of a JSON array of the encoding's name
  (`domainkit.authorization.v1`), the id, the user, the login's digest,
  the purpose, the subject, the digest, the SHA-256 of what was shown, and
  the expiry. As for a transaction, the code is never stored, and a row
  changed after its confirmation fails it.
- **Once**: one `UPDATE ... SET consumed_at = ... WHERE id = ... AND
  consumed_at IS NULL` spends it. Of two acts at once, one wins.
- **The login**: only the login that asked spends it. Another login of
  the same user (another browser, the phone's own) cannot.
- **Expiry**: `AUTH.AUTHORIZATIONS.TTL` (5 minutes, 30 at most), from
  asking to spending, as `AUTH.SCA.TTL`.

### The act spends it

The decorated service takes the authorization the rule kept on the
request. It computes the code again from the command it is given (its
digest and its subject) and from the row, and spends it:

- **With the act**, when the service's unit of work is on auth's
  database: the spend joins its transaction and commits with its first
  commit, as approvals marks a request executed (ADR 0121). It is spent
  if and only if the act commits, so an act refused on the way (`409
  concurrent_update`, a business rule) can be sent again, until the
  authorization expires.
- **Before the act**, on another database: in a transaction of its own,
  just before the service runs. At most once: an act that then fails
  needs a new authorization, as a transaction does.

Around it:

- **Other arguments, or another subject**, void the authorization, in a
  transaction of its own, and the act is refused with `403
  action_not_authorized`.
- **A decorated service called with no authorization for its purpose**
  (from another route, a batch, a task, or code) is refused
  (`AuthorizationRequired`): the act is the user's own. Code that acts
  for the system calls a service of its own.
- **`spend_authorization(uow, action, command)`** does the same in code
  that is not a service.
- **An authorization the rule accepted and no service spent** by the end
  of the request is voided then, and logged (`auth.authorization_unspent`,
  with the route); under `DEBUG` and in tests, the request fails. A
  forgotten decorator shows in the project's tests, and in production the
  authorization still works only once.

### The trail

- `ActionAuthorized` when one is confirmed: the user, the authorization,
  the purpose, the subject, the digest, the methods, and the device.
- `ActionAuthorizationUsed` when one is spent, with the same names; spent
  with the act, it commits with the act's own events.
- Refusals through `on_refusal` (ADR 0140), as `auth.authorization`:
  `invalid_authorization`, `invalid_code`, `changed`, `used`, and
  `unspent`.
- A phone's denial or report is ciba's `SignInDenied` or `SignInReported`,
  as for every request. A report pauses the pushes, as it does today.
- Neither the arguments nor what was shown reach the trail: either may be
  personal data. The digest lets an auditor match an act to its
  authorization.
- Authorizations past their expiry are purged hourly and by
  `cleartokens`. Deleting or erasing a user deletes theirs, and `privacy
  export` lists them (the purpose, the subject, and the times).

### Beside the window, transactions, and approvals

- **`StepUp(max_age=...)` is unchanged**, for acts whose arguments do not
  matter, and for projects that keep it. An authorization is not a
  step-up: confirming one records no step-up and opens no window, and a
  step-up passes no `StepUp(for_=...)`.
- **The phone's free-text step-up** (`POST /app-sign-in/step-up`) is
  deprecated in 2.4 and removed in 3.0. Through 2.x it works as before,
  answers with a `Deprecation` header (RFC 9745), and warns once a
  process (`DeprecationWarning`); its documentation says what it is: the
  page's words, and a window.
- **`auth.sca` is unchanged in 2.x**: its table, its encoding, its routes,
  and its names. Action authorizations are its design for any act, and
  share its parts: the factor checks, the phone's confirmation, and the
  code. A payment may be declared as an action instead, with the amount,
  the currency, and the payee in the command and in what is shown. The
  code is then specific to both, any change voids it, and the user is
  shown both, so PSD2's Article 5 holds as with `auth.sca`. 3.0 folds
  `auth.sca` into action authorizations: one table, one encoding, and
  `request_authorization` a built-in action.
- **Approvals answers another question**, and the two compose: a maker's
  submission or an approver's decision may be an act that asks for an
  authorization. With `APPROVALS["APPROVE_ACTION"]` (off by default),
  approving a request asks for an authorization of approvals' own,
  `approvals.approve`, bound to the request and its fingerprint and
  showing the action and the request's arguments, in place of the
  approver's step-up window. Otherwise approvals is unchanged.

## Consequences

- **An approval is worth one act.** A copy of the session gains nothing
  from the user's approval but that act, with those arguments, once. The
  phone says what the server will do, in the server's words.
- **What it does not stop**: a copy that asks for an act of its own, and
  a user who approves it without reading. The phone shows the server's
  words, the copy's address and browser, and needs the page's number;
  "this wasn't me" reports it. Stopping the copy itself, by binding the
  session to a key the browser holds (the Device Bound Session
  Credentials proposal), is left out.
- **Users approve every act.** The window let one approval cover several
  acts in a row; now each needs its own. Each act costs a round trip, a
  push or a code, one read of the row, and one write.
- **A code or a passkey confirms on the page's screen.** A page that lies
  (a script injected into it) can show one act and ask for another's
  confirmation. The phone is the stronger choice; a code is still bound to
  the act, which the window is not.
- **Migrations**: one new table, `domainkit_authorizations`, on auth's
  database (`makemigrations`, then `migrate`). No existing table changes:
  the phone's request and a passkey's challenge are linked from the new
  row, and ciba's `kind` column takes `authorization`. As for every new
  table, deleting a user and the purge need it migrated.
- **New names**: `AUTH.AUTHORIZATIONS` (`TTL`, `METHODS`), the module's
  names, `register_action` and `confirm_action_with` in
  `auth.integration`, `APPROVALS["APPROVE_ACTION"]`, the method name
  `phone`, the routes above, two events, a check for
  `StepUp(for_=...)`, and the canonical fingerprint in the core
  (approvals' `fingerprint` stays, as the same function).
- **What projects change**, to use it: declare an `Action` per act and
  register it; decorate its service; put `StepUp(for_=...)` on its route
  in place of `StepUp(max_age=...)`; teach the web client the round trip;
  teach the mobile app to show `kind` `authorization` and to send the
  digest when it approves. A project that does none of this changes
  nothing.
- **To watch**: `show` functions that put more on the phone than the user
  needs to decide, `ignore` lists that unbind what matters, and services
  that commit twice (they spend with the first commit).

## Decided at review

The ADR was proposed with seven open questions; the review of
2026-11-15 answered each. `domainkit_tests/test_action_authorisation_grounds.py`
pins, on the code before this decision, the facts questions 1, 3, 4, and
5 turned on.

1. **The rule asks, the act spends.** One request asks a rule more than
   once (`StepUp` again for each object), a later rule may refuse after
   it, and an act may fail after it: a rule that spent would burn the
   authorization in each case. Spent with the act, a failed act may be
   sent again. `Require` is unchanged.
2. **3.0 folds `auth.sca` into action authorizations.** 2.x keeps both,
   unchanged.
3. **The free-text step-up is deprecated in 2.4 and removed in 3.0**, as
   [above](#beside-the-window-transactions-and-approvals).
4. **An action may require the phone**: `methods=["phone"]`, which a
   code or a passkey does not prove. The default takes the phone, a code,
   or a passkey.
5. **The device's signature is kept**, with its public key, on the row
   and in the evidence ledger where installed. The key is not a secret;
   it is kept as long as the proof.
6. **A `ResourceRouter` mounts the twin** (`POST <path>/authorization`)
   for every command route that asks: such a route is of no use without
   one, and `StepUp(for_=...)` already says it asks. The twin shows in
   OpenAPI and in the routes `check` lists.
7. **Approvals offers `approvals.approve`**, opt-in
   (`APPROVALS["APPROVE_ACTION"]`); the step-up window stays the default
   in 2.x.

## References

1. Commission Delegated Regulation (EU) 2018/389 (PSD2's regulatory
   technical standards), Article 5, *Dynamic linking*:
   <https://eur-lex.europa.eu/eli/reg_del/2018/389/oj>
2. RFC 9396, *OAuth 2.0 Rich Authorization Requests*:
   <https://www.rfc-editor.org/rfc/rfc9396>
3. RFC 9470, *OAuth 2.0 Step Up Authentication Challenge Protocol*:
   <https://www.rfc-editor.org/rfc/rfc9470>
4. OpenID Connect Client-Initiated Backchannel Authentication Flow, Core
   1.0, `binding_message`:
   <https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html>
5. W3C, *Web Authentication: An API for accessing Public Key Credentials,
   Level 1*, the transaction authorization extensions:
   <https://www.w3.org/TR/2019/REC-webauthn-1-20190304/>
6. W3C, *Web Authentication, Level 2*:
   <https://www.w3.org/TR/webauthn-2/>

## Amendment (2026-11-16): built-in acts for the admin API and approvals

[ADR 0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md)
adds built-in actions beside `approvals.approve`, each opt-in:
`auth.admin_change` for every change of the admin API
(`AUTH.ADMIN_ACTION`), and `approvals.submit`, `approvals.reject`, and
`approvals.cancel` (`SUBMIT_ACTION`, `REJECT_ACTION`, `CANCEL_ACTION`).
`check` accepts a route whose act code spends with
`spend_authorization`, once that code says so with
`spent_in_code(action)` (`auth.E110`).

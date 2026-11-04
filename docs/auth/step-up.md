# Stepping up, and transactions authorized for one amount and payee

Some acts ask more of a user than a login: adding a payee, changing the
address that mail goes to, approving a payment. Two tools answer them.
Both use the second factor of [Two-factor logins](logging-in.md#two-factor-logins)
(`auth.mfa`):

- **Stepping up**: the rule `StepUp(max_age=...)` asks that the
  credentials authenticated again, with a code, within `max_age`. It
  guards any route, action, or WebSocket.
- **Transactions authorized for one amount and payee** (PSD2's dynamic
  linking): the user confirms one transaction, and the authorization
  holds for that amount and that payee, once.

The design, and the proof of concept that chose it, are in
[ADR 0122](../adr/0122-step-up-in-a-rule-and-bind-authorizations-with-a-stored-row-and-a-code.md).

On this page you learn how to:

- guard a route with `StepUp`, and step up from a client;
- choose what a step-up proves: a code, or the password and a code;
- request, confirm, and spend an authorization for one transaction;
- build a domain of your own on auth's step-ups and re-authentication.

## A first example

Mount the step-up router next to the one that logs in, and guard what
asks for a step-up:

```python
from datetime import timedelta

from fastapi import APIRouter, Depends

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.stepup import router as step_up_router
from domainkit.http.access import HasPermissions, Require

router = APIRouter()
router.include_router(step_up_router, prefix="/auth")


@router.post(
    "/payees",
    dependencies=[
        Depends(
            Require(
                HasPermissions("bank.add_payee") & StepUp(max_age=timedelta(minutes=5))
            )
        )
    ],
)
async def add_payee() -> dict[str, str]:
    return {"added": "DE89370400440532013000"}
```

A user who logged in, but did not step up, is refused with `401` and a
`WWW-Authenticate` header as RFC 9470 writes it. The client knows to step
up and send the request again:

```text
401 Bearer realm="api", error="insufficient_user_authentication", max_age="300"
{"detail": "Confirm it is you: authenticate again to do this.", "code": "step_up_required"}
```

The client asks the user for a code from the authenticator app, and
posts it:

```http
POST /auth/step-up
{"code": "123456"}
```

```json
{"auth_time": "2026-11-04T18:28:29.365051Z", "amr": ["otp"]}
```

Sent again within 5 minutes, `POST /payees` answers
`200 {"added": "DE89370400440532013000"}`.

## Stepping up

`StepUp` is a rule like any other (`domainkit.http.access`). Combine it
with `&`, `|`, and `~`. Give it to a `ResourceRouter`'s `rule=` (of the
resource, or of one command), to a WebSocket route, or to another domain
that takes a rule.

| `StepUp` argument | Meaning |
| --- | --- |
| `max_age` | How recent the step-up must be: more than 0, and a day at most. |
| `methods` | What it must have proved; `None`: `AUTH.STEP_UP.METHODS`. It must hold `otp`, each method once. |

A bad `max_age` or `methods` raises `ImproperlyConfigured` when the rule
is made.

`POST /step-up` (`domainkit.contrib.auth.stepup.router`) takes the code,
and the password where it is asked:

| Body | Answer |
| --- | --- |
| `{"code"}`, and `"password"` where asked | `200 {"auth_time", "amr"}`, and for a JSON Web Token login `access_token`, `token_type`, and `expires_in`. |

### What a step-up proves

What a step-up proves is `AUTH.STEP_UP.METHODS`: a code of the
authenticator app (`["otp"]`, the default), or the password with it
(`["pwd", "otp"]`, with the password sent as `"password"`). A route may
ask for more: `StepUp(max_age=..., methods=("pwd", "otp"))`. The names
are RFC 8176's, as OpenID Connect's `amr` claim has them.

With [`domainkit.contrib.devices`](devices.md), two more things step up:

- **a passkey** (`["hwk", "mfa"]`) is two factors on its own, so it proves
  whatever `StepUp` asks;
- **a bound device's key** (`["hwk"]`) is a possession factor, as the
  authenticator app is. It proves the code (`otp`), but not the password.

`StepUp(methods=("pwd", "otp"))` with `AUTH.STEP_UP.METHODS = ["otp"]`
would refuse everyone, because no step-up proves `pwd`. `check` names the
route, and staging and production refuse to start:

```text
auth.E106: POST /payees asks StepUp for pwd, which no step-up proves (AUTH.STEP_UP.METHODS is ['otp']): it refuses everyone. Add the methods to AUTH.STEP_UP.METHODS, or leave methods out.
```

### Where a step-up is kept

- **A login token or a browser session** keeps its last step-up in
  `domainkit_step_ups`, by the token's digest. `StepUp` reads it, in one
  query, on the routes that ask. Another login of the same user has
  stepped up nothing.
- **A JSON Web Token login** gets a new access token in the answer
  (`access_token`, `token_type`, `expires_in`), of the same login, with
  the `auth_time` and `amr` claims. Send it from then on. Other services
  that check the token read the claims themselves. A refresh issues
  tokens without them, so a JSON Web Token's step-up lasts as long as that
  access token (`AUTH.JWT.ACCESS_TOKEN_TTL`, 5 minutes by default) or
  `max_age`, the shorter.

### What is not a step-up

- **A login**, with a second factor or without: `StepUp` asks for a
  factor proven again for this act.
- **A recovery code.** It stands in for a lost app at login only, and is
  not spent trying.
- **Credentials limited to scopes** cannot step up (`403`).
- **A client certificate, or a class of your own**, cannot keep a step-up
  (`400 step_up_unsupported`).
- **Staff acting as the user** ([impersonation](routes.md#impersonation)):
  `StepUp` refuses them, because what a step-up guards is the user's own.
- **A user without a second factor** gets `409 no_second_factor`.

### Counts, the trail, and live connections

- **Counts.** Wrong codes count against `AUTH.MFA.RATE` for the user,
  with every other code check. Wrong passwords count against
  `AUTH.LOGIN_RATE` with the user's other password checks (and with the
  account's wrong logins, with `AUTH.PASSWORD_CHECKS_COUNT_AS_LOGINS`). So
  stepping up is no way around either. A code is spent once.
- **The trail.** Each step-up is recorded as `auth.stepped_up`, with the
  methods (the event `SteppedUp`). Each refusal is recorded as
  `auth.step_up`, with `invalid_code` or `wrong_password`, when
  `domainkit.contrib.audit` is installed.
- **WebSockets and event streams.** The handshake is checked, and so is
  each check of an open connection (`LIVE_REAUTH_INTERVAL`), with the
  step-up read afresh. The connection closes once its step-up is older
  than `max_age`, as when its login is revoked or idles out.
- **With other rules.** A plain refusal answers first:
  `StepUp(...) & HasPermissions("bank.add_payee")` answers a user without
  the permission with `403`, recorded as a denial, and only a user who
  has it with `401 step_up_required`, whatever the order of the rules.
  Given to `check_object_permissions`, `StepUp` is asked about the object
  as about the request.
- **Keeping.** `max_age` is a day at most. The hourly purge
  (`domainkit.contrib.messaging`) and `cleartokens` delete step-ups older
  than that. Deleting or erasing a user deletes theirs.

Another authentication (an OpenID Connect provider's tokens) writes its
own rule: `StepUp` reads this domain's credentials. A rule's
`refusal(request, user)` answers with the error that tells a client what
to do ([Protecting routes](routes.md#protecting-routes)).

## Transactions authorized for one amount and payee

PSD2's regulatory technical standards (Article 5) ask that the
authentication of a payment be bound to its amount and its payee. The
user is shown both, the code their authentication produces is specific
to both, and any change to either makes it fail. In domainkit:

1. a use case asks for an authorization (`request_authorization`);
2. the user sees the amount and the payee, and confirms with a second
   factor (`GET /auth/sca/{id}`, `POST /auth/sca/{id}/confirm`);
3. the use case spends the authorization on that very transaction
   (`consume_authorization`).

```python
from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import BaseModel

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.contrib.auth.sca import consume_authorization, request_authorization
from domainkit.contrib.auth.sca import router as sca_router
from domainkit.money import Money

router = APIRouter()
router.include_router(sca_router, prefix="/auth")


class TransferIn(BaseModel):
    amount: Money
    payee: str


@router.post("/transfers/authorize")
async def authorize_transfer(
    body: TransferIn, user: AuthenticatedUser
) -> dict[str, str]:
    challenge = await request_authorization(
        user.id, body.amount, body.payee, purpose="transfer"
    )
    return {"challenge": str(challenge.id)}


@router.post("/transfers")
async def transfer(
    body: TransferIn,
    user: AuthenticatedUser,
    authorization: Annotated[str, Header(alias="X-Authorization")],
) -> dict[str, str]:
    await consume_authorization(  # 403 transaction_not_authorized otherwise
        authorization,
        user_id=user.id,
        amount=body.amount,
        payee=body.payee,
        purpose="transfer",
    )
    return await send_transfer(body.amount, body.payee)  # The use case acts once spent.
```

Between the two, the client shows the user what they confirm, and sends
the code:

```http
GET /auth/sca/9f0c...        200 {"id": "9f0c...", "amount": {"amount": "250.00", "currency": "EUR"},
                                  "payee": "DE89370400440532013000", "purpose": "transfer",
                                  "expires_at": "...", "confirmed": false}
POST /auth/sca/9f0c.../confirm {"code": "123456"}
                             200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
```

| Name | What it does |
| --- | --- |
| `request_authorization(user_id, amount, payee, *, purpose)` | Stores a transaction for the user to confirm, for `AUTH.SCA.TTL`; returns a `ScaChallenge`. |
| `consume_authorization(authorization, *, user_id, amount, payee, purpose)` | Spends the authorization on this transaction, once; returns the `ScaChallenge`. Raises `TransactionNotAuthorized` (403). |
| `ScaChallenge` | The stored request: its `id`, the user, `amount`, `payee`, `purpose`, and when it expires, was confirmed, and was used. |
| `router` | `GET /sca/{id}` and `POST /sca/{id}/confirm`, for credentials without scopes. |
| `InvalidTransaction` | The amount, payee, or purpose cannot be authorized as given (422). |

How it holds:

- **The binding.** The request is a row of `domainkit_sca_challenges`:
  the user, the amount and its currency (`Money`, two columns), the
  payee, the purpose, and the expiry. Confirming it returns `<id>.<code>`.
  The code is an HMAC-SHA256, under a key derived from `SECRET_KEY` (its
  fallbacks verify), of one canonical encoding of the row's id (a random
  nonce), the user, the currency's code, the amount in fixed point with
  the currency's places, the payee exactly as given, and the purpose.
  `consume_authorization` computes the code again from the values it is
  given, and marks the row used in one statement.
- **What fails.** Another amount (`100.01`), the same number in another
  currency, another payee (one more space, another case), another
  purpose, another user, an unconfirmed or expired request, and a second
  use. Each is `403 transaction_not_authorized`, which says nothing of
  why. A substituted amount or payee also voids the authorization, so the
  attempt costs the attacker the authorization. Of twenty uses at once,
  one wins. A row edited after its confirmation fails too: the code
  covers what was confirmed, not what is stored.
- **Spend it first.** Call `consume_authorization` before the
  transaction acts, in a transaction of its own (the auth database may be
  another). A transfer that then fails needs a new authorization, never a
  second use of the old one: at most once, never twice.
- **Settings.** `AUTH.SCA.TTL` (5 minutes, 30 at most) bounds the whole
  flow, from the request to its use. `AUTH.SCA.METHODS` is what
  confirming proves, as `STEP_UP.METHODS`. Codes count against
  `AUTH.MFA.RATE`.
- **Amounts.** More than zero, in a currency of four places or fewer
  (the column's scale: every ISO 4217 currency). A payee of 1 to 255
  printable characters. A purpose that is a lower-case name (`transfer`,
  `payments.payout`). Anything else is `InvalidTransaction` (422).
- **The trail.** `auth.transaction_authorized` and
  `auth.transaction_authorization_used` (the events
  `TransactionAuthorized` and `TransactionAuthorizationUsed`) name the
  user, the request, and the purpose, not the payee, which may be
  personal data. Refusals are `auth.sca` with `invalid_code`,
  `invalid_authorization`, `changed`, or `used`. Requests past their
  expiry are purged hourly and by `cleartokens`. Deleting or erasing a
  user deletes theirs.

## For other domains: `auth.integration`

`domainkit.contrib.auth.integration` holds what domains built on auth use
to log a user in, step them up, and hear about them.
`domainkit.contrib.identity` and `domainkit.contrib.devices` use these
names, and they keep their meaning across releases. A domain of yours
that adds a way to log in, or a new factor, uses them the same way:

```python
from fastapi import Request

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.integration import (
    MfaChallengeOut,
    TokenOut,
    challenge,
    issue_login,
    reauthenticate,
)


async def add_hardware_key(
    request: Request, user: AbstractBaseUser, password: str | None
) -> None:
    # The password, or a step-up within REAUTH_AGE; never under impersonation.
    await reauthenticate(request, user, password, "add_hardware_key")
    ...


async def logged_in(user: AbstractBaseUser) -> TokenOut | MfaChallengeOut:
    pending = await challenge(user)  # the second factor's challenge, if any
    if pending is not None:
        return pending
    issued = await issue_login(user, factor_proven=False)
    return TokenOut(access_token=issued.access_token, expires_in=issued.expires_in)
```

| Name | What it is |
| --- | --- |
| `issue_login(user, *, factor_proven=True)` | Records the login and issues tokens of the first authentication class. Past `MAX_LOGINS`, the oldest logins end. |
| `challenge(user)` | The second factor's challenge (`MfaChallengeOut`), or `None` when the user has no second factor. |
| `TokenOut`, `MfaChallengeOut` | The answers of a login: tokens, or `{"mfa_required", "mfa_token", "expires_in"}`. |
| `reauthenticate(request, user, password, action)` | The user proved it is them again: the password, or a step-up within `REAUTH_AGE` (10 minutes). Raises `ReauthenticationRequired` (`401 reauthentication_required`), always under impersonation. |
| `check_password_again(request, user, password, action)` | Checks the password, counted with the user's other password checks: `400 password_required`, `400 wrong_password`, or `429`. |
| `proof_of(connection)` | The credentials' step-up (`Proof(auth_time, methods)`), read once a request; `None` if none. |
| `stepping_credentials(request)` | The request's credentials if they can keep a step-up; else `400 step_up_unsupported`. |
| `record_step_up(uow, credentials, user, proof)` | Keeps a step-up where the credentials keep it, and records `SteppedUp`. |
| `step_up_answer(credentials, proof)` | The answer of a step-up (`StepUpOut`), with a JSON Web Token login's new token. |
| `impersonated(connection)` | Whether staff act as the user on this connection. |
| `SteppedUp`, `UserDeleted`, `UserSignedUp` | Events to react to. `UserDeleted` lets a domain delete what it keeps of a user. |
| `PASSWORD_MAX_LENGTH`, `REAUTH_AGE`, `utc_now()` | The longest password a body takes (4,096), the age a step-up stands for the password, and the current time in UTC. |

`Proof` and `StepUpRequired` are exported from `domainkit.contrib.auth`.

## Options

| `AUTH` key | Default | What it does |
| --- | --- | --- |
| `STEP_UP.METHODS` | `["otp"]` | What a step-up proves; it must hold `otp`. |
| `SCA.METHODS` | `["otp"]` | What confirming a transaction proves. |
| `SCA.TTL` | 5 minutes | How long a transaction's authorization request works, from the request to its use; 30 minutes at most. |
| `MFA.RATE` | `"10/hour"` | Wrong codes per user, step-ups and transactions included. |

[The settings tables](../auth.md#settings) give the full rows.

## Errors and refusals

| Status and code | When |
| --- | --- |
| `401 step_up_required` | `StepUp` refuses; the `WWW-Authenticate` header says `max_age`. |
| `401 invalid_code` | A wrong code, or one used already. |
| `409 no_second_factor` | The user has no second factor to step up with. |
| `400 step_up_unsupported` | The credentials cannot keep a step-up. |
| `403` | Credentials limited to scopes. |
| `400 password_required`, `400 wrong_password` | The password, where `METHODS` asks for it. |
| `429` | A count is full. |
| `403 transaction_not_authorized` | Any authorization that does not hold for this transaction. |
| `409 transaction_confirmed`, `409 transaction_expired` | Confirming twice; confirming too late. |
| `404` | Another user's transaction request. |
| `422` (`InvalidTransaction`) | An amount, payee, or purpose that cannot be authorized. |
| `auth.E106` | A route asks for methods that no step-up proves. |

## Common mistakes

- **Spending the authorization after the transfer.** A failure in
  between would let it be used again. Spend it first.
- **Passing a payee you normalized differently.** The payee must be the
  same text, exactly, at request and at use.
- **Asking `StepUp` for `pwd` while `STEP_UP.METHODS` leaves it out.**
  Nobody passes; `auth.E106` names the route.
- **Keeping an old JSON Web Token after a step-up.** The new access token
  in the answer carries the step-up; send it.

## See also

- [Two-factor logins](logging-in.md#two-factor-logins): turning on the
  second factor.
- [Passkeys and bound devices](devices.md): other ways to step up.
- [Protecting routes](routes.md): combining `StepUp` with other rules.
- [Approvals](../contrib/approvals.md#who-may-approve-step-up): a
  step-up for approvers.
- [Money](../money.md): the `Money` type of an authorization.

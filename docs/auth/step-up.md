# Stepping up, and transactions authorized for one amount and payee

Some acts ask more of a user than a login: adding a payee, changing the
address mail goes to, approving a payment. Two tools answer them, both on
the second factor of [Two-factor logins](logging-in.md#two-factor-logins) (`auth.mfa`):

- **Stepping up**: a rule, `StepUp(max_age=...)`, asks that the
  credentials authenticated again, with a code, within `max_age`. It
  guards any route, action, or WebSocket.
- **Transactions authorized for one amount and payee** (PSD2's dynamic
  linking): the user confirms one transaction, and the authorization holds
  for that amount and payee, once.

The design and the proof of concept that chose it are in
[ADR 0122](../adr/0122-step-up-in-a-rule-and-bind-authorizations-with-a-stored-row-and-a-code.md).

## Stepping up

Mount the router next to the one that logs in, and guard what asks for it:

```python
from datetime import timedelta

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.stepup import router as step_up_router
from domainkit.http.access import HasPermissions, Require

router.include_router(step_up_router, prefix="/auth")

@router.post(
    "/payees",
    dependencies=[Depends(Require(HasPermissions("bank.add_payee") & StepUp(max_age=timedelta(minutes=5))))],
)
async def add_payee(...): ...
```

`StepUp` is a rule like any other (`domainkit.http.access`): combine it
with `&`, `|`, and `~`, give it to a `ResourceRouter`'s `rule=` (of the
resource, or of one command), to a WebSocket route, or to another app
that takes a rule. A request it refuses gets `401 step_up_required`,
with `WWW-Authenticate` as RFC 9470 writes it, so a client knows to step
up and send the request again:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="api", error="insufficient_user_authentication", max_age="300"

{"detail": "Confirm it is you: authenticate again to do this.", "code": "step_up_required"}
```

The client asks the user for a code, and posts it:

```http
POST /auth/step-up
{"code": "123456"}

200 {"auth_time": "2027-01-04T10:01:00Z", "amr": ["otp"]}
```

What a step-up proves is `AUTH.STEP_UP.METHODS`: a code of the
authenticator app (`["otp"]`, the default), or the password with it
(`["pwd", "otp"]`, sent as `"password"`). A route may ask for more:
`StepUp(max_age=..., methods=("pwd", "otp"))`. The names are RFC 8176's,
as OpenID Connect's `amr` claim has them.

- **Where it is kept.** A login token or a browser session keeps its last
  step-up in `domainkit_step_ups`, by the token's digest, and `StepUp`
  reads it, in one query, on the routes that ask; another login of the
  same user has stepped up nothing. A JSON Web Token login gets a new
  access token in the response (`access_token`, `token_type`,
  `expires_in`), of the same login, with the `auth_time` and `amr`
  claims: send it from then on. Other services that check the token read
  the claims themselves. A refresh issues tokens without them, so a JSON
  Web Token's step-up lasts as long as that access token
  (`AUTH.JWT.ACCESS_TOKEN_TTL`, 5 minutes by default) or `max_age`, the
  shorter.
- **What is not a step-up.** A login, with a second factor or without:
  `StepUp` asks for a factor proven again for the act. A recovery code
  (it stands in for a lost app at login only, and is not spent trying).
  Credentials limited to scopes cannot step up (`403`), nor can a client
  certificate or a class of a project's own (`400 step_up_unsupported`).
  A user without a second factor gets `409 no_second_factor`.
- **Counts and the trail.** Wrong codes count against `AUTH.MFA.RATE` for
  the user with every other code check, and wrong passwords against
  `AUTH.LOGIN_RATE` with the user's other password checks, so stepping up
  is no way around either; a code is spent once. Each step-up is recorded
  (`auth.stepped_up`, with the methods), and each refusal (`auth.step_up`,
  `invalid_code` or `wrong_password`), when `domainkit.contrib.audit` is
  installed.
- **WebSockets and event streams.** The handshake is checked; an open
  connection keeps what its start proved, and its re-checks
  (`LIVE_REAUTH_INTERVAL`) pass the rule. The credentials themselves are
  still checked again, so a revoked login closes it.
- **Keeping.** `max_age` is a day at most; the hourly purge
  (`domainkit.contrib.messaging`) and `cleartokens` delete step-ups older
  than that. Deleting or erasing a user deletes theirs.

Another authentication (an OpenID Connect provider's tokens) writes its
own rule: `StepUp` reads this app's credentials, and a rule's
`refusal(request, user)` answers with the error that tells a client what
to do ([Protecting routes](routes.md#protecting-routes)).

## Transactions authorized for one amount and payee

PSD2's regulatory technical standards (Article 5) ask that the
authentication of a payment be bound to its amount and its payee: the
user is shown both, the code their authentication produces is specific to
both, and any change to either makes it fail. A use case asks for an
authorization, the user confirms it with a second factor, and the use
case spends it on that transaction:

```python
from domainkit.contrib.auth.sca import (
    TransactionNotAuthorized,
    consume_authorization,
    request_authorization,
)
from domainkit.contrib.auth.sca import router as sca_router
from domainkit.money import Money

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
) -> TransferOut:
    await consume_authorization(  # 403 transaction_not_authorized otherwise
        authorization,
        user_id=user.id,
        amount=body.amount,
        payee=body.payee,
        purpose="transfer",
    )
    return await send_transfer(body.amount, body.payee)  # The use case acts once spent.
```

Between the two, the client shows the user what they confirm and sends
the code:

```http
GET /auth/sca/9f0c...        200 {"id": "9f0c...", "amount": {"amount": "250.00", "currency": "EUR"},
                                  "payee": "DE89370400440532013000", "purpose": "transfer",
                                  "expires_at": "...", "confirmed": false}
POST /auth/sca/9f0c.../confirm {"code": "123456"}
                             200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
```

- **The binding.** The request is a row of `domainkit_sca_challenges`:
  the user, the amount and its currency (`Money`, two columns), the payee,
  the purpose, and the expiry. Confirming it returns `<id>.<code>`: the
  code is an HMAC-SHA256, under a key derived from `SECRET_KEY` (its
  fallbacks verify), of one canonical encoding of the row's id (a random
  nonce), the user, the currency's code, the amount in fixed point with
  the currency's places, the payee exactly as given, and the purpose.
  `consume_authorization` recomputes the code from the values it is
  given, and marks the row used in one statement.
- **What fails.** Another amount (`100.01`), the same number in another
  currency, another payee (a space more, another case), another purpose,
  another user, an unconfirmed or expired request, and a second use: each
  is `403 transaction_not_authorized`, which says nothing of why. A
  substituted amount or payee voids the authorization too, so the attempt
  costs the attacker the authorization. Of twenty uses at once, one wins.
  A row edited after its confirmation fails as well: the code covers what
  was confirmed, not what is stored.
- **Spend it first.** Call `consume_authorization` before the
  transaction acts, in a transaction of its own (the auth database may be
  another). A transfer that then fails needs a new authorization, never a
  second use of the old one: at most once, never twice.
- **Settings.** `AUTH.SCA.TTL` (5 minutes, 30 at most) bounds the whole
  flow, from the request to its use; `AUTH.SCA.METHODS` is what confirming
  proves, as `STEP_UP.METHODS`. Codes count against `AUTH.MFA.RATE`; a
  wrong one is `401 invalid_code`, a confirmed request confirmed again
  `409 transaction_confirmed`, and one past its time `409
  transaction_expired`. Another user's request is `404`.
- **Amounts.** More than zero, in a currency of four places or fewer (the
  column's scale: every ISO 4217 currency); a payee of 1 to 255 printable
  characters; a purpose that is a lower-case name (`transfer`,
  `payments.payout`). Anything else is `InvalidTransaction` (422).
- **The trail.** `auth.transaction_authorized` and
  `auth.transaction_authorization_used` name the user, the request, and
  the purpose (not the payee, which may be personal data); refusals are
  `auth.sca` with `invalid_code`, `invalid_authorization`, `changed`, or
  `used`. Requests past their expiry are purged hourly and by
  `cleartokens`; deleting or erasing a user deletes theirs.

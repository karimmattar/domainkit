# Sign-in approvals

`domainkit.contrib.ciba` lets the user's phone approve sign-ins. A web
page, or another service, starts a sign-in request; the phone the user
bound shows who asks and from where; the user approves or denies it; the
page polls until then and gets a login. For other services it is an
OpenID Provider for CIBA (OpenID Connect's Client-Initiated Backchannel
Authentication). With one setting, the mobile app becomes the user's
identity: a web login works only once the phone approved it.

On this page you learn how to:

- install the domain and mount its routes;
- sign in on the web by push or by QR code;
- answer from the phone;
- make the mobile app the user's identity;
- let other services ask the phone, as OIDC clients;
- know what is limited, and what is stored.

ADR [0128](../adr/0128-approve-sign-ins-from-the-users-mobile-app.md)
records why it works this way.

## A first example

Install the `devices` extra, which the bound devices need, and `push`
for the push backends (FCM and APNs):

```bash
pip install "domainkit[devices,push]"
```

Install the domain after auth, messaging, notifications, and devices, run
`makemigrations` and `migrate`, and mount its routes:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.messaging",
    "domainkit.contrib.notifications",
    "domainkit.contrib.devices",
    "domainkit.contrib.ciba",
]
NOTIFICATIONS = {"PUSH_BACKEND": "..."}  # FCM, APNs: see Mail and notifications
CIBA = {"APPROVE_LOGINS": True}  # every key is optional
```

```python
from domainkit.contrib.ciba.api import router as sign_in_router
from domainkit.contrib.devices.api import router as devices_router

router.include_router(devices_router, prefix="/auth")
router.include_router(sign_in_router, prefix="/auth")
```

The app has bound the phone (`POST /auth/devices`) and registered its
push token (`POST /auth/devices/push-tokens`), as
[Passkeys and bound devices](devices.md) shows. The web page starts a
sign-in by push:

```http
POST /auth/app-sign-in/push
{"identifier": "ann@example.com"}
```

```json
{"request": "kq3D1...", "expires_in": 120, "interval": 5, "match_code": "47"}
```

The page shows `47`, keeps `request`, and polls every `interval` seconds:

```http
POST /auth/app-sign-in/token
{"request": "kq3D1..."}
```

```json
{"code": "authorization_pending", "detail": "The sign-in waits for the phone; ask again later."}
```

The phone gets a push that names the request and nobody. It lists what
waits, with requests signed by its bound key:

```http
GET /auth/app-sign-in/requests
X-Device-Id: 7b1c...
X-Device-Time: 1767225600123
X-Device-Signature: MEUCIQ...
```

```json
[{"id": "0b6f...", "kind": "sign_in", "channel": "push", "client": null, "binding_message": null, "address": "203.0.113.7", "browser": "Mozilla/5.0 ...", "same_network": false, "match_required": true, "transaction": null, "created_at": "...", "expires_at": "..."}]
```

The user types the number the page shows, and approves:

```http
POST /auth/app-sign-in/requests/0b6f.../approve
{"match_code": "47"}
```

The page's next poll gets tokens, as `/login` gives them. `POST
/auth/app-sign-in/session` gives a browser session in cookies instead, as
`/session` does.

## How a sign-in works

A sign-in request starts on one of two channels:

- **Push.** The initiator names the user (`/app-sign-in/push`, or a CIBA
  client's `login_hint`). Each app of the user that gave a push token
  gets a notification. Its text is `PUSH_TITLE` and `PUSH_BODY`; its data
  is `{"type": "domainkit.sign_in", "request_id": "..."}`. It holds no
  personal data, since it goes through Apple's or Google's servers: the
  app fetches the details itself.
- **QR code.** The initiator names nobody (`/app-sign-in/qr`). Its page
  renders `qr` as a QR code. The phone that scans it sends it to
  `/app-sign-in/requests/claim`, and the request becomes its user's. A
  code works once, and can be scanned for `QR_TTL` (60 seconds,
  `qr_expires_in`); the page shows a new one if no phone scanned it.
  Once scanned, the phone has `REQUEST_TTL` to answer, as for a push:
  `expires_in` covers both, so keep polling after the code is scanned.

Then the phone approves or denies, and the initiator polls with the
handle it got (`request`, or CIBA's `auth_req_id`). The poll answers:

| Answer | Meaning |
| --- | --- |
| tokens | Approved. The handle is spent: a second poll gets `sign_in_unknown`. |
| `authorization_pending` | Not answered yet. Poll again after `interval`. |
| `slow_down` | This poll came before `interval` was up. The interval grows by 5 seconds, from now on. |
| `access_denied` | The phone denied it. |
| `expired_token` | Its time is up: `REQUEST_TTL` (2 minutes) to answer a push or a scanned QR code, or `QR_TTL` for a QR code no phone scanned. |
| `sign_in_unknown` | No request has the handle, or it was redeemed, or it is another initiator's. |

## The phone's routes

Each route needs the user's credentials without scopes, and a request
signed by one of their bound devices (`DeviceSigned`): a stolen token
alone approves nothing. A refused signature is `401
device_signature_refused`.

| Route | Body | Answer |
| --- | --- | --- |
| `GET /auth/app-sign-in/requests` | | The requests waiting for an answer, oldest first. |
| `POST /auth/app-sign-in/requests/claim` | `{"code"}`: what the QR code holds | The request, now the user's. |
| `POST /auth/app-sign-in/requests/{id}/approve` | `{"match_code"?}` | `204`. |
| `POST /auth/app-sign-in/requests/{id}/deny` | `{"report"?}` | `204`. |

A request shows:

- `kind`: `sign_in`, `step_up` (an action of a signed-in session), or
  `transaction` ([below](#asking-the-phone-from-a-signed-in-session));
- `client`: the name of the service that asks, or `null` for your own
  pages;
- `binding_message`: the text that service shows too, or what a step-up
  is for;
- `transaction`: a transaction's `amount`, `payee`, and `purpose`, read
  from auth's record; `null` for the other kinds;
- `address` and `browser`: where the sign-in was started;
- `same_network`: whether the phone is on that address now;
- `match_required`: whether approving needs the page's number.

Show all of it, and make "Deny" as easy to reach as "Approve".

`report: true` says the user did not start the sign-in: someone who knows
their identifier did. Every request of the user still waiting is denied,
`SignInReported` goes to the audit trail, and no push reaches the user
for `REPORT_COOLDOWN` (15 minutes).

## Web sign-in routes

These routes are public. The two that start a sign-in take the login
captcha (`AUTH.CAPTCHA`), and are limited to `START_RATE` per client
address (`429 rate_limited`).

| Route | Body | Answer |
| --- | --- | --- |
| `POST /auth/app-sign-in/push` | `{"identifier"}` | `request`, `expires_in`, `interval`, `match_code`. |
| `POST /auth/app-sign-in/qr` | | `request`, `expires_in`, `interval`, `qr`. |
| `POST /auth/app-sign-in/token` | `{"request"}` | Tokens, or a poll's answer. |
| `POST /auth/app-sign-in/session` | `{"request"}` | A browser session (cookies and `csrf_token`), or a poll's answer. |

Keep `request` in the page's memory: never in the QR code, a URL, or
storage another page reads. Whoever holds it gets the login once the
phone approves.

The answer to `/push` is the same whoever the identifier names. The page
cannot tell a user who exists from one who does not, or who cannot be
asked now (see [What holds](#what-holds)).

## Asking the phone from a signed-in session

A web session that is signed in already can ask the user's phone to
approve an action, or to confirm a transaction. The routes need
credentials without scopes, and only the session that asked redeems the
answer: the request is bound to its credentials.

| Route | Body | Answer |
| --- | --- | --- |
| `POST /auth/app-sign-in/step-up` | `{"message"}` | `request`, `expires_in`, `interval`, `match_code`. |
| `POST /auth/app-sign-in/step-up/token` | `{"request"}` | The step-up, `amr` `["hwk"]`, as `/auth/step-up` answers it; or a poll's answer. |
| `POST /auth/app-sign-in/transactions/{challenge_id}` | | `request`, `expires_in`, `interval`, `match_code`. |
| `POST /auth/app-sign-in/transactions/token` | `{"request"}` | `{"authorization", "expires_at"}`, as `/auth/sca/{id}/confirm` answers; or a poll's answer. |

The message of a step-up names the action in words the user recognizes,
such as `"Add payee DE89 3704 0044"`: at most 64 printable characters.
The phone lists the request with `kind` `step_up` and the message in
`binding_message`, and approves it as a sign-in, with the page's number
under `NUMBER_MATCHING`. Once approved, the session's step-up proves
`hwk`, which stands for an authenticator's code: a route behind
`StepUp(...)` lets it through, as after `/auth/step-up`.

```python
from datetime import timedelta

from fastapi import Depends

from domainkit.contrib.auth import StepUp
from domainkit.http.access import Require


@router.post("/payees", dependencies=[Depends(Require(StepUp(timedelta(minutes=5))))])
async def add_payee(payee: PayeeIn) -> PayeeOut: ...
```

A transaction is one of the user's `auth.sca` transactions
([Transactions authorized for one amount and payee](step-up.md#transactions-authorized-for-one-amount-and-payee)).
The phone lists it with `kind` `transaction` and a `transaction` object:
the amount, payee, and purpose read from auth's record, never text the
page sent. Once approved, the authorization is spent as any other: on
that amount and payee, once. A transaction whose `AUTH.SCA.METHODS` asks
for the password too is refused with `409 transaction_needs_other_factors`:
a key in the phone proves possession, not what the user knows.

A handle redeems only as what it was asked for: a step-up is never a
login, and a login request never steps a session up. Staff acting as the
user cannot ask their phone (`401 reauthentication_required`). A user
whose phone cannot be asked now (none bound, `MAX_PENDING` waiting, or a
recent report) gets `409 phone_unavailable`.

## The mobile app as identity

With `CIBA = {"APPROVE_LOGINS": True}`, a user who bound a device logs in
only:

- from that device, which signs the login request (`/login`, `/session`,
  `/mfa/login`, the passkey login, an identity provider's login);
- or once approved from it, through `/app-sign-in`.

Any other login of that user is `403 phone_approval_required` ("Approve
this sign-in from your mobile app."). A user with no bound device logs in
as before: the app's first login has to bind it. So the cycle runs from
the phone: sign up and verify there ([Signing up](logging-in.md)), log
in, bind the device, and approve every web sign-in afterwards.

The app's own logins sign their request with the device key, like any
other request:

```http
POST /auth/login
X-Device-Id: 7b1c...
X-Device-Time: 1767225600123
X-Device-Signature: MEUCIQ...
{"identifier": "ann@example.com", "password": "..."}
```

`DEVICES = {"DEVICE_LOGIN": True}` adds `POST /auth/devices/login`: the
signature alone logs the device's user in, with no password. The app
keeps the key behind the phone's biometrics, so unlocking it is the
login, as banking apps do. Refused signatures count against
`AUTH.LOGIN_RATE` per address.

A user who lost every bound phone cannot log in anywhere, and recovers
through support only: no code and password alone move an account to a
new phone. Staff check who is asking, then run `unbinddevices`:

```bash
python manage.py unbinddevices ann
```

Every device and push token of the user goes, and every session ends.
The user logs in with their other factors, and binds the new phone; in
[strict mode](#strict-mode-every-login-from-the-phone) the new phone
enrolls.

A user who moves to a new phone signs the new app in as the web does:
the new app starts `/app-sign-in/push` (the old phone gets the push) or
shows `/app-sign-in/qr` (the old phone scans it), and binds itself once
approved. Only a user who lost every bound phone needs `unbinddevices`.

Logins issued before you turn the setting on stay valid. To end them,
log users out everywhere.

A sign-in approved from the phone runs every login gate too, as `/login`
does: the approval stands for the device's signature in this app's gate,
and the gates of your own domains still judge it.

### Strict mode: every login from the phone

With `CIBA = {"APPROVE_LOGINS": "always"}`, a user with no bound device
does not log in either: `403 phone_enrollment_required`. Every login is
then one of these:

- signed by a bound device;
- approved from one, through `/app-sign-in`;
- the enrollment of the user's first device (`POST /devices/enroll`,
  [Enrolling a first device](devices.md#enrolling-a-first-device)).

No web page logs in with a password. A user signs up and verifies
(`/signup`, a code), enrolls the phone, and from then on the phone is the
only way in: its signed logins, or `DEVICE_LOGIN` behind the phone's
biometrics. Turn enrollment on with it:

```python
CIBA = {"APPROVE_LOGINS": "always"}
DEVICES = {"ENROLLMENT": True, "DEVICE_LOGIN": True}
```

`ciba.E005` refuses `"always"` without `DEVICES.ENROLLMENT`: nobody could
log in at all. Users who logged in before you turned it on keep their
logins until they end.

### Managing logins from the phone

In both modes, the phone is where a user sees and ends their logins
(since 2.2). The app calls auth's routes, signed by its device key like
its other requests:

| Request | Answer |
| --- | --- |
| `GET /auth/tokens` | Each login, with how it signed in (`push`, `qr`, `password`, ...), its browser or app, its address, and `current` for the phone's own ([Where each login came from](logging-in.md#where-each-login-came-from)). |
| `DELETE /auth/tokens/{id}` | `204`: that login ends, wherever it is used. |
| `POST /auth/logout` with `{"others": true}` | `204`: every login but the phone's ends. Personal tokens stay. |
| `POST /auth/logout` with `{"everywhere": true}` | `204`: every token of the user ends, the phone's too. |

```http
GET /auth/tokens
Authorization: Bearer dkt_...
X-Device-Id: 7b1c...
X-Device-Time: 1767225600123
X-Device-Signature: MEUCIQ...
```

For a user with a bound device, the same requests unsigned, from a web
session or anything else, are `403 phone_required` ("Manage your logins
from your mobile app."). A stolen web session cannot list the logins or
end the phone's, and the phone sees it and ends it. A web page can still
log itself out (`POST /auth/logout` without a body, or `DELETE` of its
own token), and create personal tokens. A user with no bound device
manages their logins as before.

The check is auth's `on_manage_logins` hook
([Who manages logins](logging-in.md#who-manages-logins)); the app
registers it when it starts.

### Login gates

The setting works through auth's login gates
(`domainkit.contrib.auth.gates`). Every auth route that issues a login
runs the gates on its request and user first. A gate refuses by raising.
A domain registers one in `ready()`:

```python
from domainkit.contrib.auth.gates import on_login_gate


async def office_hours(request: Request, user: AbstractBaseUser) -> None:
    if not open_now():
        raise PermissionDenied("Logins open at 8.")


class OfficeConfig(DomainConfig):
    def ready(self) -> None:
        on_login_gate(office_hours)
```

Gates fail closed. While one is registered, a login issued without
passing them raises `LoginNotGated`, so a route that forgets them cannot
issue. A login route of your own calls them before it issues:

```python
from domainkit.contrib.auth.integration import issue_login, pass_login_gates


@router.post("/kiosk/login", dependencies=[Public])
async def kiosk_login(body: KioskIn, request: Request) -> TokenOut:
    user = await check_kiosk_card(body)
    await pass_login_gates(request, user)
    issued = await issue_login(user)
    ...
```

Code that issues a login on the server side, with no request to judge,
wraps it in `gates_passed(user.id)`.

## OIDC clients

Other services (a call centre, a point of sale, a partner's site) ask the
phone through the OpenID Provider's routes. Mount them, and their
metadata at the root:

```python
from domainkit.contrib.ciba.oidc import router as ciba_router
from domainkit.contrib.ciba.oidc import well_known

router.include_router(ciba_router, prefix="/oauth")
router.include_router(well_known)
```

The ID tokens are signed with `AUTH.JWT`'s key, which must be asymmetric,
with an `ISSUER` (`ciba.E002` refuses to start otherwise):

```python
AUTH = {
    "JWT": {
        "ALGORITHM": "ES256",
        "SIGNING_KEY": env("JWT_PRIVATE_KEY"),
        "VERIFYING_KEY": env("JWT_PUBLIC_KEY"),
        "ISSUER": "https://id.example.com",
    },
}
```

Register each client with the `ciba` command. Generate its secret, and
pipe it in: the command reads it from the standard input, never prints
it, and keeps only its SHA-256. Hand the secret to the client yourself.

```bash
openssl rand -base64 32 > acme.secret
python manage.py ciba add "Acme Bank" --scope payments.add_payment < acme.secret
```

```text
client_id: ciba_2kQ...
```

A client proves itself one of three ways:

- `client_secret_basic` (the default) or `client_secret_post`: a secret of
  at least 32 characters, kept as its SHA-256.
- `private_key_jwt` (`--method private_key_jwt --jwks file.json`), as
  FAPI asks: the client signs an assertion (RFC 7523) with one of its
  public keys. It must use an asymmetric algorithm, and name the client
  in `iss` and `sub`. Its `aud` is the issuer or the endpoint's URL, its
  `exp` at most `ASSERTION_MAX_AGE` (5 minutes) away. Its `jti` works
  once. It needs PyJWT (`domainkit[jwt]`).

The client asks for a sign-in:

```http
POST /oauth/bc-authorize
Authorization: Basic Y2liYV8ya1E...
Content-Type: application/x-www-form-urlencoded

scope=openid%20payments.add_payment&login_hint=ann%40example.com&binding_message=TX-7731
```

```json
{"auth_req_id": "f4M...", "expires_in": 120, "interval": 5}
```

The phone shows "Acme Bank" and "TX-7731", which the client shows too.
The client polls the token endpoint:

```http
POST /oauth/token
Authorization: Basic Y2liYV8ya1E...
Content-Type: application/x-www-form-urlencoded

grant_type=urn:openid:params:grant-type:ciba&auth_req_id=f4M...
```

```json
{"access_token": "Ws1...", "token_type": "Bearer", "expires_in": 600, "scope": "openid payments.add_payment", "id_token": "eyJ..."}
```

| Route | What it does |
| --- | --- |
| `POST /oauth/bc-authorize` | CIBA's authentication request: `scope` with `openid`, exactly one `login_hint` (an identifier) or `id_token_hint` (an ID token this provider gave the client), and a `binding_message` of at most 64 printable characters. `requested_expiry` is accepted, not honoured. |
| `POST /oauth/qr-authorize` | This framework's extension: the same without a hint. Answers `qr` too, for the client's page to show. |
| `POST /oauth/token` | The CIBA grant: polls, then the tokens. |
| `GET /.well-known/openid-configuration` | The provider's metadata: the endpoints, `poll` as the only delivery mode, the client authentication methods. |
| `GET /.well-known/jwks.json` | The public keys of the ID tokens: the current one and `AUTH.JWT.PREVIOUS_KEYS`. |

The access token is opaque. It carries the scopes asked, narrowed to
those the client may have (`--scope`) and the user holds: it never
carries more than the user may do. It works for `ACCESS_TOKEN_TTL` (10
minutes), comes with no refresh token, and shows in the user's token
list as `ciba:<client_id>`.

The ID token carries `iss`, `sub` (the user's id), `aud` (the client),
`iat`, `exp` (`ID_TOKEN_TTL`), `auth_time`, `amr` (`["hwk"]`, a key held
in hardware), and `at_hash`.

The endpoints take form bodies, each parameter once, and answer errors in
OAuth's format (`{"error", "error_description"}`), never cached. A client
that does not prove itself is `401 invalid_client`. The token endpoint
answers a poll's refusals by CIBA's names (`invalid_grant` for an unknown
handle). Each client starts at most `CLIENT_RATE` requests
(`429 rate_limited`). An unknown `login_hint` gets a request like any
other, not `unknown_user_id`.

## Settings

`CIBA = {...}`; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `APPROVE_LOGINS` | `False` | Make the mobile app the user's identity ([above](#the-mobile-app-as-identity)); `"always"` refuses users with no bound device too ([strict mode](#strict-mode-every-login-from-the-phone)). |
| `REQUEST_TTL` | 2 minutes (at most 10) | How long a push request waits for the phone. |
| `QR_TTL` | 60 seconds (at most 5 minutes) | How long a QR code can be scanned; once scanned, `REQUEST_TTL` to answer it. |
| `INTERVAL` | 5 (1 to 60) | Seconds between polls; sooner gets `slow_down`. |
| `MAX_PENDING` | 3 (1 to 20) | Push requests a user may have waiting; past it, no push is sent. |
| `REPORT_COOLDOWN` | 15 minutes (at most 1 day) | How long no push reaches a user who reported a request. |
| `NUMBER_MATCHING` | `True` | Web push sign-ins show two digits the phone must type. |
| `REQUIRE_BINDING_MESSAGE` | `True` | CIBA clients must send a `binding_message`. |
| `QR_SAME_NETWORK` | `"warn"` | When the phone claiming a QR code is on another address than the page: tell the app (`same_network: false`), or refuse (`"require"`, `403 different_network`). |
| `QR_FORMAT` | `"{code}"` | What a QR code holds, at most 300 characters; `{code}` once, such as `"https://id.example.com/qr#{code}"` for a universal link. |
| `START_RATE` | `"10/minute"` | Sign-ins the public routes start per client address; `None` for none. |
| `CLIENT_RATE` | `"60/minute"` | Requests each CIBA client starts; `None` for none. |
| `ACCESS_TOKEN_TTL` | 10 minutes (at most 1 hour) | How long a CIBA client's access token works. |
| `ID_TOKEN_TTL` | 5 minutes (at most 1 hour) | How long its ID token works. |
| `ASSERTION_MAX_AGE` | 5 minutes (at most 1 hour) | The furthest a `private_key_jwt` assertion may expire. |
| `PUSH_TITLE`, `PUSH_BODY` | `"Approve sign-in?"`, `"Open the app to approve or deny it."` | The notification's text. Keep personal data out of it. |

`DEVICES.DEVICE_LOGIN` and `DEVICES.ENROLLMENT` (both `False`) are the
devices app's ([above](#the-mobile-app-as-identity)).

## What holds

- **The poll handle is the initiator's alone.** It is 256 random bits,
  kept as its SHA-256, and never in the QR code or the push. Whoever
  scans or sees the code cannot poll. It is redeemed once, by one
  guarded update.
- **A QR code is separate, and works once.** It is 256 random bits, made
  by the server, kept as its SHA-256, cleared when claimed, and valid for
  `QR_TTL`.
- **The phone never names a user.** The request becomes the user's whose
  credentials and device signature the phone sent.
- **Every answer is one guarded update.** Two phones, two polls, or a
  phone and a poll racing never both win: the loser is refused.
- **Push bombing is bounded.** A user gets at most `MAX_PENDING` pushes
  waiting, none for `REPORT_COOLDOWN` after a report, and a web page
  needs the number it shows. A wrong number denies the request: guessing
  gets one try.
- **Nothing tells who exists.** A push to an unknown identifier, an
  inactive user, a user with no bound device, one at `MAX_PENDING`, or
  one in the cooldown is a decoy. It answers like any request, and nobody
  can approve it.
- **What the phone sees, and what the page sees.** The phone sees where
  the sign-in started; the page sees only states and tokens, never who
  the user is.
- **Pushes can be crowded out.** Someone who knows a user's identifier can
  keep `MAX_PENDING` push requests waiting for them, so the user's own
  push sign-ins become decoys until those expire or are denied. QR codes
  still work, and a report pauses the pushes. Lower `START_RATE` if this
  is abused.
- **A relayed QR code is caught, not prevented.** An attacker can show
  their own page's code to a victim. The phone shows the page's address
  and browser, and warns when it is not on the same network. Passkeys'
  own cross-device flow (the browser's QR code, with Bluetooth proving
  proximity) is the phishing-resistant choice where it fits.

The sign-in requests live on auth's database (the application refuses to
start otherwise). An hourly task forgets them a day after they end, and
assertion ids once expired. Deleting a user forgets their requests, and
`privacy export` and `privacy erase` cover them: each keeps the address
and browser that started it.

## Without HTTP

The routes call functions that a use case may call too, in a
`CibaUnitOfWork` that it commits:

```python
from domainkit.contrib.ciba import CibaUnitOfWork, Requester, poll, start_push

async with CibaUnitOfWork() as uow:
    user = await uow.users.find_by_identifier("ann@example.com")
    started = await start_push(uow, user, requester=Requester(None, None))
    await uow.commit()  # the push leaves with the commit

approved = await poll(started.handle, client_id=None)  # commits itself
```

| Function | What it does |
| --- | --- |
| `start_push(uow, user, *, requester, client_id=None, scope="", binding_message=None, match=False)` | A push request; a decoy when `user` is None or cannot be asked. Returns `Started` (`handle`, `request_id`, `expires_in`, `interval`, `match_code`). |
| `start_qr(uow, *, requester, client_id=None, scope="", binding_message=None)` | A QR request; `Started.qr` holds the code. |
| `claim(uow, user_id, code, *, address)` | The QR request, now the user's. Raises `QrCodeExpired` or `DifferentNetwork`. |
| `approve(uow, user_id, request_id, device_id, *, match_code=None)` | The approved request, or `None` for a wrong number (commit, then raise `WrongMatchCode`). Raises `SignInNotFound`. |
| `deny(uow, user_id, request_id, *, report=False)` | Denies, and with `report` every other waiting request. Raises `SignInNotFound`. |
| `poll(handle, *, client_id)` | The approved request, redeemed; else a `PollRefused`. |

`CibaUnitOfWork` is a `DevicesUnitOfWork` with `requests`, `clients`, and
`assertions`. `register(...)` and `rotate_secret(...)` in
`domainkit.contrib.ciba.clients` do what the `ciba` command does.

## Errors and refusals

| Exception | Status and code | When |
| --- | --- | --- |
| `PhoneApprovalRequired` | `403 phone_approval_required` | `APPROVE_LOGINS`: a web login of a user with a bound device. |
| `PhoneEnrollmentRequired` | `403 phone_enrollment_required` | `APPROVE_LOGINS = "always"`: any login of a user with no bound device. |
| `PhoneRequired` | `403 phone_required` | `APPROVE_LOGINS`: listing a user's logins, or ending one but the request's own, unsigned by their bound device. |
| `SignInNotFound` | `404 sign_in_not_found` | The phone answers a request that is not its user's, was answered, or expired. |
| `QrCodeExpired` | `422 qr_code_expired` | The QR code is unknown, was claimed, or expired. |
| `DifferentNetwork` | `403 different_network` | `QR_SAME_NETWORK = "require"`, and the phone is elsewhere. |
| `WrongMatchCode` | `422 wrong_match_code` | Not the page's number: the request is denied. |
| `AuthorizationPending`, `SlowDown`, `SignInExpired`, `AccessDenied`, `SignInUnknown` | `400 authorization_pending`, `slow_down`, `expired_token`, `access_denied`, `sign_in_unknown` | A poll's answers ([above](#how-a-sign-in-works)). |
| `DeviceLoginOff` | `404 device_login_off` | `POST /devices/login` without `DEVICES.DEVICE_LOGIN`. |
| `PhoneUnavailable` | `409 phone_unavailable` | A step-up or a transaction asked of a user whose phone cannot be asked now. |
| `TransactionClosed` | `409 transaction_closed` | A transaction confirmed or used already, or expired. |
| `TransactionNeedsOtherFactors` | `409 transaction_needs_other_factors` | `AUTH.SCA.METHODS` asks for the password too. |

## Common mistakes

- **Putting the poll handle in the QR code or a URL.** Whoever scans or
  sees it gets the login. Keep it in the page; the QR code is `qr`.
- **Approving from the push alone.** The push names a request id, and no
  one. Fetch the request, show its client, message, address, and browser,
  then let the user choose.
- **Turning `NUMBER_MATCHING` off for web push sign-ins.** The user then
  approves whatever arrives, and an attacker who knows an identifier gets
  in on a tired tap.
- **A shared-secret `AUTH.JWT` with the OIDC routes.** Clients cannot
  verify HS256 ID tokens without your secret; `ciba.E002` refuses it.
- **`APPROVE_LOGINS` without a support path.** A user who lost their only
  phone needs `unbinddevices`, after you check who is asking.

## See also

- [Passkeys and bound devices](devices.md): binding the phone, signed
  requests, push tokens.
- [Logging in](logging-in.md): the routes the login gates guard.
- [Mail and notifications](../mail.md#text-messages-and-push-notifications):
  the push backends.
- [ADR 0128](../adr/0128-approve-sign-ins-from-the-users-mobile-app.md):
  the research and the decisions.

# Passkeys and bound devices

`domainkit.contrib.devices` keeps keys that a user's devices hold. The
server stores only public keys. Passkeys and devices' signatures are
checked with `cryptography` alone, which the `devices` extra installs.
The domain has three parts:

- **passkeys** (WebAuthn), which log a user in with no password and no
  second factor, and step them up;
- **bound devices**: a key that a mobile app makes in the phone's Secure
  Enclave or Android Keystore. The device signs requests with it, and
  steps its user up;
- **push tokens**, so a use case can notify a user on every app of
  theirs.

On this page you learn how to:

- install the domain and set the relying party;
- add a passkey, log in with it, and step up with it;
- bind a device, sign requests with it, and guard a route with
  `DeviceSigned`;
- confirm one act from the mobile app, or with a passkey;
- register push tokens and notify a user.

## A first example

Install the `devices` extra:

```bash
pip install "domainkit[devices]"
```

Install the domain after auth, run `makemigrations` and `migrate`, and
mount its routes:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.devices",
]
DEVICES = {
    "RP_ID": "example.com",
    "ORIGINS": ("https://app.example.com",),
}
```

```python
from fastapi import APIRouter, Depends

from domainkit.contrib.devices import DeviceSigned
from domainkit.contrib.devices.api import router as devices_router
from domainkit.http.access import Require

router = APIRouter()
router.include_router(devices_router, prefix="/auth")


@router.post("/payments", dependencies=[Depends(Require(DeviceSigned()))])
async def pay() -> dict[str, str]:
    return {"paid": "250.00 EUR"}
```

The app binds its key, with the user's password:

```http
POST /auth/devices
Authorization: Bearer <a login token>

{"name": "Alice's phone", "public_key": "MCowBQYDK2VwAyEA...", "password": "correct horse battery"}
```

```json
{"id": "03567b13-a14b-4ec8-b349-3c1f1b2c3d87", "name": "Alice's phone", "created_at": "2026-11-04T18:37:36.335414Z", "last_used_at": null}
```

Then each request to `/payments` must be signed by that device
([Bound devices](#bound-devices) shows how). What the server answers:

```text
POST /payments, not signed           401 device_signature_refused  Sign this request with this device's key.
POST /payments, signed               200 {'paid': '250.00 EUR'}
POST /payments, the same signature   401 device_signature_refused
```

The device tables must live on the same database as auth's: the
application refuses to start when routing sends them elsewhere. Deleting
a user (`UserDeleted`) removes their passkeys, devices, and push tokens.

## Settings

`DEVICES = {...}`; every key is optional. The model `DevicesSettings`
(`domainkit.contrib.devices.settings`) validates it.

| Key | Default | Meaning |
| --- | --- | --- |
| `RP_ID` | `None` | The relying party: your site's domain, which every origin is within. `None`: no passkeys (`404 passkeys_off`). |
| `RP_NAME` | `None` | The name a browser shows when a passkey is made; `None`: `RP_ID`. |
| `ORIGINS` | `()` | The pages passkeys are used on (`https://app.example.com`), an app's too (`android:apk-key-hash:...`). Required with `RP_ID`. |
| `CHALLENGE_TTL` | 5 minutes (at most 10) | How long a challenge works; the browser's timeout too. |
| `MAX_PASSKEYS` | 10 (1 to 100) | Passkeys a user holds at once (`409 too_many_keys` past it). |
| `MAX_DEVICES` | 10 (1 to 100) | Bound devices a user holds at once (`409 too_many_keys` past it). |
| `MAX_PUSH_TOKENS` | 20 (1 to 100) | Push tokens a user holds; a new one past it ends the oldest. |
| `SIGNATURE_WINDOW` | 2 minutes (at most 10) | How far a signed request's time may be from the server's. |
| `SIGNATURE_STORE` | `"database"` | Where the signed requests seen within the window are kept: `"database"`, or `"cache"` (the default cache, shared by every process) ([Bound devices](#bound-devices)). |
| `BOUND_NOTICE` | `True` | Mail the user when a device is bound to their account ([The user is told](#the-user-is-told)). |
| `DEVICE_LOGIN` | `False` | `POST /devices/login`: a bound device logs its user in by its signature alone ([Sign-in approvals](ciba.md#the-mobile-app-as-identity)). |
| `ENROLLMENT` | `False` | `POST /devices/enroll/code` and `POST /devices/enroll`: a user with no bound device binds the first one with no login first ([Enrolling a first device](#enrolling-a-first-device)). Needs `domainkit.contrib.messaging`. |

## Adding a key needs the user again

Adding a passkey or binding a device needs the user to authenticate
again: their password (`{"password"}`, counted against
`AUTH.LOGIN_RATE` with their other password checks), or a
[step-up](step-up.md) within the last 10 minutes. Without it, whoever
held a stolen access token could add a key of their own and keep the
account.

- No password and no recent step-up: `401 reauthentication_required`.
- A wrong password: `400 wrong_password`.
- Staff acting as the user ([impersonation](routes.md#impersonation))
  add no keys, password or not: `401 reauthentication_required`.

Every route of this domain for a signed-in user needs credentials without scopes.

## Passkeys

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /passkeys/register/options` | a user, authenticated again (`{"password"}`); in the [phone modes](ciba.md#managing-logins-from-the-phone), signed by one of their devices | The options for `navigator.credentials.create()`. |
| `POST /passkeys/register` | a user; in the phone modes, signed | `{"credential": <its toJSON()>, "name"}`: `201` with the passkey. `name` is 1 to 100 characters, `"Passkey"` by default. |
| `POST /passkeys/login/options` | anyone | The options for `navigator.credentials.get()`. |
| `POST /passkeys/login` | anyone | `{"credential"}`: `200` with tokens. |
| `POST /passkeys/step-up/options` | a user | The options, offering the user's passkeys. |
| `POST /passkeys/step-up` | a user | `{"credential"}`: a step-up, `amr` `["hwk", "mfa"]`. |
| `POST /passkeys/authorizations/{id}/options` | the session that asked for the authorization | The options, with a challenge for that authorization ([Confirming one act](#confirming-one-act)). |
| `POST /passkeys/authorizations/{id}` | the session that asked | `{"credential"}`: `200 {"authorization", "expires_at"}`, proving `hwk` and `mfa`. |
| `GET /passkeys` | a user | The user's passkeys: `id`, `name`, `backed_up` (synced), `created_at`, `last_used_at`. |
| `DELETE /passkeys/{id}` | a user; in the phone modes, signed | `204`; `404 passkey_not_found` for another's. |

The login options look like this:

```json
{"challenge": "nsC_dbQq3XYmoO_8UA0NWxNzqkqqvA4Xe2JcxUBbFjo", "rpId": "example.com", "timeout": 300000, "allowCredentials": [], "userVerification": "required"}
```

A page logs in with a passkey in three calls:

```javascript
const options = await post("/auth/passkeys/login/options");
const credential = await navigator.credentials.get({
  publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
});
const tokens = await post("/auth/passkeys/login", {credential: credential.toJSON()});
```

What the server checks:

- **A challenge for one ceremony.** Every ceremony starts with a
  challenge that the server makes and keeps: 256 random bits, for one
  purpose and (except for a login) one user, used once within
  `CHALLENGE_TTL`. A refused answer spends it too.
- **The client data** must be of the right ceremony, for that challenge,
  from one of `ORIGINS`, and not made in another origin's frame.
- **The authenticator data** must name `RP_ID`, with the user present and
  verified (`userVerification: "required"`).
- **The signature** must hold for the passkey's key: ES256, EdDSA, or
  RS256 of 2,048 bits or more.
- **The signature counter** must move past the last one, as a copied
  key's would not. Passkeys that count nothing (synced ones) are taken.
- **The attestation is not checked** (`attestation: "none"`). A passkey is
  trusted as the user's because they authenticated again to add it, not
  because of who made it.

Every refusal answers `401 passkey_refused`; the reason is logged.

A passkey verified with its user is two factors on its own (possession,
and a biometric or PIN; NIST SP 800-63B). So a login with one asks no
second factor, and a step-up with one proves whatever `StepUp` asks
(`mfa`).

For code of your own, `domainkit.contrib.devices.passkeys` has the
ceremonies the routes use: `creation_options(user)`,
`register(user, client_data_json=..., attestation_object=..., name=...)`,
`request_options(purpose, user)`, `authenticate(assertion, purpose, user=None, *, challenge=None)`
with an `Assertion(credential_id, client_data_json, authenticator_data, signature, user_handle=None)`
(with `challenge`, only an answer to that one),
and `reauthenticate(request, user, password, action)`.

## Bound devices

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /devices` | a user, authenticated again; in the [phone modes](ciba.md#managing-logins-from-the-phone), signed by one of their devices once they have one | `{"name", "public_key", "password"}`: `201` with the device's `id`. `403 phone_required` unsigned in the phone modes. |
| `GET /devices` | a user | The user's devices: `id`, `name`, `created_at`, `last_used_at`. |
| `DELETE /devices/{id}` | a user; in the [phone modes](ciba.md#managing-logins-from-the-phone), signed by one of their devices | `204`: its signatures hold no more, and its logins end. `404 device_not_found` for another's; `403 phone_required` unsigned in the phone modes. |
| `POST /devices/step-up` | a user, signed by their device | A step-up, `amr` `["hwk"]`. |
| `POST /devices/authorizations/{id}` | the login that asked for the authorization, signed by one of the user's devices | `{"digest"}`: `200 {"authorization", "expires_at"}`, proving `hwk` and `phone` ([Confirming one act](#confirming-one-act)). |
| `POST /devices/login` | anyone, signed by a bound device (`DEVICE_LOGIN`) | Tokens for the device's user, as `/login` gives them; `404 device_login_off` without the setting. |

The key is P-256 or Ed25519, as SubjectPublicKeyInfo or, for P-256, the
raw point that the Secure Enclave exports, in base64url. Anything else is
`422 unsupported_device_key`. `device_key(encoded)`, in
`domainkit.contrib.devices.binding`, checks one the same way.

A request that the device signs carries three headers:

```text
X-Device-Id: 7b1c...            the device's id
X-Device-Time: 1767225600123    milliseconds since the epoch
X-Device-Signature: MEUCIQ...   base64url
```

The signature (ES256 in DER, as both platforms make it, or Ed25519) is
of these lines, joined by `\n`:

```text
domainkit-device-v1
POST
/payments?draft=1
1767225600123
<the body's SHA-256, base64url, unpadded>
```

`signed_message(method, target, time, body)` builds these bytes. A test,
or a client written in Python, signs with it:

```python
import time

from cryptography.hazmat.primitives.asymmetric import ed25519

from domainkit.contrib.devices.binding import signed_message

key = ed25519.Ed25519PrivateKey.generate()  # the device's key, kept on the device
moment = int(time.time() * 1000)
signature = key.sign(signed_message("POST", "/payments", moment, b""))
```

`DeviceSigned` lets a request through only when:

- a device of the request's user signed it;
- its time is within `SIGNATURE_WINDOW` of the server's;
- the same request was not sent before. Each request is kept while its
  time is within the window, by the SHA-256 of what it signed, so it
  works once, whichever process reads it.

Otherwise the answer is `401 device_signature_refused`. Requests that an
app sends together pass in whatever order they arrive (since 2.3,
[ADR 0135](../adr/0135-keep-each-signed-request-not-the-last-time.md));
two that are the same in every part, the time included, are one request
sent twice.

By default the requests are kept in a table of the devices' database.
With `SIGNATURE_STORE = "cache"` they are kept in the default cache, and
a signed request writes to no database:

- the cache must be shared by every process (Redis). `setup()` refuses
  `dummy://`, and `memory://` in staging and production;
- while the cache is down, signed requests are refused.

A device's `last_used_at` is written at most once a minute. `DeviceSigned`
is a rule like any other: combine it with `&`, `|`, and `~`, and give it
to a `ResourceRouter`'s `rule=`.

A device's step-up proves `hwk`: a key in the device's hardware, a
possession factor as the authenticator app is. So it stands for the code
(`otp`) a `StepUp` asks, but not for the password:

```json
{"auth_time": "2026-11-04T18:37:36.410418Z", "amr": ["hwk"]}
```

A device's login, by `/devices/login` or by enrolling, records the method
`device` and the device's id; a passkey's records `passkey`. So do a login
whose request the device signed, and the login a device was bound with.
The user's login list (`GET /auth/tokens`) shows them, so the phone can
tell its own login from the web's ([Where each login came
from](logging-in.md#where-each-login-came-from)), and a password change
can keep it ([Credential changes and the phone](logging-in.md#credential-changes-and-the-phone)).

### Binding another device

In the [phone modes](ciba.md#managing-logins-from-the-phone), a user who
bound a device binds another only from it (since 2.3,
[ADR 0134](../adr/0134-bind-another-device-from-a-bound-one.md)): the
password alone would let a stolen web session bind a phone of its thief's.

1. The new phone makes its key, and shows the public key, as a QR code
   for example.
2. The old phone reads it, and sends `POST /devices` with the new
   `public_key` and a `name`, signed by its own key, with the password or
   after a step-up (`POST /devices/step-up`).
3. The new phone logs in with its key: `POST /devices/login`
   (`DEVICE_LOGIN`).

The old phone's login stays its own. Unsigned, the request is
`403 phone_required`. A user who lost every phone goes to support
(`unbinddevices`), then enrolls again.

With `domainkit.contrib.ciba`, the new phone can show a QR code instead,
and the old one scans it and approves, with no key moved between them
([A new phone asks](ciba.md#a-new-phone-asks-to-be-bound)).

### The user is told

When a device is bound, by any of these ways, its user gets a mail: the
device's name, and what to do if they did not bind it (since 2.4,
[ADR 0137](../adr/0137-a-new-phone-asks-and-the-bound-one-approves.md)).
NIST SP 800-63B-4 asks for it: a subscriber is told of an authenticator
bound to their account, by another channel than the one that bound it
([authenticator events](https://pages.nist.gov/800-63-4/sp800-63b/events)).
It needs `domainkit.contrib.messaging` and `domainkit.contrib.mail`, and
an address on the account. The template is `devices/device_bound`;
`BOUND_NOTICE = False` turns it off.

## Enrolling a first device

With `ENROLLMENT`, a phone app binds the first device of a user who has
none, with no login first. This is how a user starts once
`CIBA.APPROVE_LOGINS` lets no password log in
([Sign-in approvals](ciba.md#the-mobile-app-as-identity)).

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /devices/enroll/code` | anyone | `{"identifier"}`: `202`, always. The worker sends a code to the account, at an address or a number its user proved ([ADR 0141](../adr/0141-codes-that-open-an-account-go-only-where-it-was-proved.md)). |
| `POST /devices/enroll` | anyone, signed by the new key | `{"identifier", "code", "password", "name", "public_key"}`: `201` with the tokens of a login and the `device`. |

1. The app asks for a code. `identifier` is what the user logs in with
   (`AUTH.IDENTIFIERS`): a mobile number gets a text message; anything
   else, a mail to the account's address. The answer is `202` whoever it
   names. Nothing is sent to an unknown, inactive, or passwordless
   account, nor to one with a bound device already.
2. The app makes its key, and sends the code, the password, and the
   public key, signed by the new key. The headers are those of any signed
   request, without `X-Device-Id`: there is no device yet.

The code and the password are two factors; the signature proves the app
holds the key it binds. Each is checked before the code is spent, in
this order:

- The signature and the password. Either wrong: `401 enrollment_refused`.
- A user who turned a second factor on sends `otp` too: a code of the
  authenticator app, or a recovery code. Without it, the answer is
  `401 second_factor_required`, and the code is not spent: send again
  with `otp`.
- The code, then `otp`. Either wrong: `401 enrollment_refused`, and the
  code is spent: ask for another.
- A password staff set (`AUTH.PASSWORD_CHANGE_ON_FIRST_USE`) needs
  `new_password`, as `/login` does: `403 password_change_required`
  without it, and `422 invalid_password` for one a rule refuses. Both are
  answered before the code is checked, so it is not spent: send again
  with a new password.

A refusal never says which proof failed. Refusals count against
`AUTH.LOGIN_RATE`, for the client's address and for the account. Asking
for a code counts against it for the address, and against
`AUTH.EMAIL_RATE` for the identifier, as every request that sends a code
does: callers from many addresses cannot text one number without end,
and the ways of writing one number share its count.

A user with a bound device enrolls no other: `409 device_already_bound`.
A new phone is approved from the old one, then binds with `POST /devices`
as usual. A user who lost every phone goes to support, who checks who is
asking and runs `unbinddevices`; there is no self-service replacement.

The login the enrollment issues passes the login gates as any login does,
signed by the device it bound: `CIBA.APPROVE_LOGINS` lets it through.

## Confirming one act

An act may ask for an authorization of its own, for its arguments, spent
once ([Authorizing one act](actions.md), since 2.4,
[ADR 0144](../adr/0144-authorize-one-act-not-a-window-of-time.md)). This
domain confirms one two ways, besides a code, and registers both with
auth when it is installed:

| Way | Endpoint | Proves | A user has it when |
| --- | --- | --- | --- |
| The mobile app's own act | `POST /devices/authorizations/{id}` | `hwk`, `phone` | they have a bound device |
| A passkey | `POST /passkeys/authorizations/{id}/options`, then `POST /passkeys/authorizations/{id}` | `hwk`, `mfa` | they have a passkey, and `RP_ID` is set |

Each answers `{"authorization": "<id>.<code>", "expires_at"}`, for the
act's `Action-Authorization` header. Asking for an authorization needs a
way the user has that proves what the act asks (`409 no_second_factor`
otherwise): with this domain, a bound device or a passkey stands for the
authenticator app. An act may ask `["phone"]` alone: the app's own act
proves it, so `check` passes it (`auth.E108`).

### The mobile app's own act

The app makes an act itself, from its own login, and shows it on the
phone's screen, which the web session does not control. It asks for the
authorization, works out the act's digest from what it shows, and sends
the digest, signed by its device:

```http
POST   /organizations/7/memberships/42/authorization                 201 {"id": "9f0c...", "shown": {...}}
POST   /auth/devices/authorizations/9f0c... {"digest": "5d1e..."}    200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
       X-Device-Id, X-Device-Time, X-Device-Signature
DELETE /organizations/7/memberships/42
       Action-Authorization: 9f0c....mYd3...                         204
```

The digest is the SHA-256 of `{"action": <the purpose>, "arguments": <the
command's fields>}` as JSON, keys sorted and no spaces, in UTF-8, less the
fields the action ignores: `fingerprint(purpose, arguments)`.

```python
from domainkit.ddd import fingerprint

digest = fingerprint(
    "organizations.end_membership",
    {"organization_id": ORGANIZATION_ID, "membership_id": MEMBERSHIP_ID},
)
```

What the server checks:

- **A bound device of the user signed the request**, as `DeviceSigned`
  checks it: else `401 device_signature_refused`.
- **The authorization is the app's own login's.** The login that sends
  the request must be the one that asked for it, which alone spends it.
  Another session's, even the user's own browser's, is `404
  authorization_not_found`: the app confirms only the acts it makes and
  shows itself. A page's act is confirmed on the phone
  ([Sign-in approvals](ciba.md#asking-the-phone-from-a-signed-in-session)),
  which shows who asked, and matches a number.
- **The digest is the act's**: else `409 authorization_changed`, and the
  authorization stays unconfirmed. The app shows another act than the
  server holds.

The authorization keeps the device's id, its key, what it signed, its
signature, and when, so the approval can be checked again once the device
is unbound; with `domainkit.contrib.evidence` installed, the evidence
ledger keeps them too, past the row (`auth.action_authorized`). What the
device signed holds the body's SHA-256: an app that sends the body as
compact JSON, `{"digest":"<the digest>"}`, lets anyone check the kept proof
from the row alone.

### A passkey

1. `POST /passkeys/authorizations/{id}/options` answers the options for
   `navigator.credentials.get()`, as the step-up's do, with a challenge
   made for this authorization and linked to its row. Asking again makes
   a new challenge; only the last one confirms it.
2. The page shows the act (`GET /auth/authorizations/{id}`), and the
   browser asks the passkey.
3. `POST /passkeys/authorizations/{id}` takes `{"credential"}`, and
   answers the authorization.

What the server checks:

- **Only the session that asked** uses either route. Another, even
  another login of the same user, gets `404 authorization_not_found`, and
  the challenge stays for the session.
- **The challenge**, as a step-up's is checked (purpose `authorize`, the
  user, once), must be the one made for this authorization. An answer to
  another authorization's challenge, or to a step-up's or a login's, is
  `401 passkey_refused`, and the challenge it answered is spent.
- **What the act asks.** A passkey proves `hwk` and `mfa`: it stands for
  a code and for the password, never for `phone`. An act that asks the
  phone is `409 authorization_needs_other_factors`, before any challenge
  is made.

As with a code, what the user read was on the page.

`domainkit.contrib.devices.authorizations` has what the routes call, for
code of your own: `confirm_with_device(request, user, authorization_id, digest)`,
`passkey_options(request, user, authorization_id)`,
`confirm_with_passkey(request, user, authorization_id, assertion)`, and
the two ways, `DEVICE_CONFIRMATION` and `PASSKEY_CONFIRMATION`.
`device_proof(request, user)`, in `domainkit.contrib.devices.binding`,
returns what the user's device signed for the request (`DeviceProof`:
`device`, `signed`, `signature`, `signed_at`), or `None`.

## Push tokens

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /devices/push-tokens` | a user; in the phone modes, signed by one of their devices once they have one | `{"token", "platform"}` (`fcm`, `apns`, `huawei`): `201` with `id`, `platform`, `created_at`, `last_seen_at`. |
| `GET /devices/push-tokens` | a user | The user's tokens, never the token itself. |
| `DELETE /devices/push-tokens/{id}` | a user; signed as registering one is | `204`, when the app logs out. |

In the phone modes, a web session could otherwise register a token of
its own and read the user's sign-in pushes, or remove the phone's so that
none arrive (`403 phone_required`, since 2.3).

An app registers the token its push service gave it. A token moves to
whoever registers it last (a phone that changed hands). Past
`MAX_PUSH_TOKENS`, a user's oldest tokens end. A use case notifies the
user on every app of theirs, through
[`domainkit.contrib.notifications`](../mail.md#text-messages-and-push-notifications):

```python
from domainkit.contrib.devices.push import notify_user

sent = await notify_user(
    order.customer_id, "Shipped", "On its way", data={"order": str(order.id)}, uow=uow
)
```

`notify_user(user_id, title, body, *, data=None, uow=None)` queues one
notification per token, for the backend of its platform
(`NOTIFICATIONS.PUSH_BACKENDS`), and returns how many. With `uow`, they
go out when it commits. A token that its service no longer knows is
forgotten when the worker learns so (`PushTokenExpired`).

## What is audited

With `domainkit.contrib.audit` installed: `devices.passkey_added`,
`devices.passkey_removed`, `devices.device_bound`, and
`devices.device_unbound` (the events `PasskeyAdded`, `PasskeyRemoved`,
`DeviceBound`, `DeviceUnbound`), step-ups as `auth.stepped_up`, and an
act's authorization confirmed as `auth.action_authorized`, with the
device that confirmed it.
Refused enrollments are audited as `devices.enroll`.

## The Python names

`domainkit.contrib.devices` exports `DeviceSigned`, `DevicesUnitOfWork`
(an `AuthUnitOfWork` with `passkeys`, `challenges`, `devices`, and
`push_tokens`), the stored records `Passkey` and `BoundDevice`, and the
errors below. `domainkit.contrib.devices.stores` also has `Challenge`
and `PushToken`, and `domainkit.contrib.devices.tables` the tables
`passkeys`, `challenges`, `devices`, and `push_tokens`.

```python
from domainkit.contrib.devices import DevicesUnitOfWork

async with DevicesUnitOfWork() as uow:
    for token in await uow.push_tokens.of_user(user.id):
        print(token.platform, token.last_seen_at)
```

## Errors and refusals

| Exception | Status and code | When |
| --- | --- | --- |
| `ReauthenticationRequired` | `401 reauthentication_required` | Adding a key without the password or a recent step-up, or under impersonation. |
| `PasskeyRefused` | `401 passkey_refused` | Any passkey answer that does not hold; the reason is logged. |
| `DeviceSignatureRefused` | `401 device_signature_refused` | A request not signed, signed by another's device, out of the window, or signed again. |
| `EnrollmentRefused` | `401 enrollment_refused` | An enrollment whose signature, password, code, or `otp` is wrong. |
| `SecondFactorRequired` | `401 second_factor_required` | An enrollment of a user with a second factor, without `otp`. |
| `EnrollmentOff` | `404 enrollment_off` | `ENROLLMENT` is off. |
| `DeviceAlreadyBound` | `409 device_already_bound` | An enrollment of a user who has a bound device. |
| `PasskeysOff` | `404 passkeys_off` | `RP_ID` is not set. |
| `PasskeyNotFound`, `DeviceNotFound` | `404 passkey_not_found`, `404 device_not_found` | Not one of the user's. |
| `TooManyKeys` | `409 too_many_keys` | Past `MAX_PASSKEYS` or `MAX_DEVICES`. |
| `AuthorizationNotFound` | `404 authorization_not_found` | Confirming an authorization another session asked for, or another user's. |
| `AuthorizationChanged` | `409 authorization_changed` | The app's digest is not the act's. |
| `AuthorizationNeedsOtherFactors` | `409 authorization_needs_other_factors` | A passkey, where the act asks the phone. |
| `UnsupportedKey` | `422 unsupported_device_key` | Not a P-256 or Ed25519 public key. |

## Common mistakes

- **`RP_ID` without `ORIGINS`.** The settings refuse it: passkeys need
  the pages they come from.
- **An origin outside `RP_ID`.** Every origin must be within the
  relying party's domain.
- **Signing the path without its query.** The device signs the path with
  its query, as sent.
- **Sending the same signed request twice.** A retry is a new request:
  sign it again, with the time of the retry.
- **Enrolling with an identifier that does not log in.** The code goes
  only to an account that `identifier` logs in as (`AUTH.IDENTIFIERS`):
  with logins by username only, send the username.
- **Scoped tokens on a `DeviceSigned` route.** `DeviceSigned` does not
  check scopes; add a rule that does, or the request is refused.

## See also

- [Stepping up](step-up.md): what `hwk` and `mfa` prove.
- [Authorizing one act](actions.md): what the app's own act and a
  passkey confirm.
- [Logging in](logging-in.md): passwords and the second factor.
- [Phone numbers](phones.md): proving a user receives text messages.
- [Mail and notifications](../mail.md#text-messages-and-push-notifications):
  the push backends.

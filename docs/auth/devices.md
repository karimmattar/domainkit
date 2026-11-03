# Passkeys and bound devices

`domainkit.contrib.devices` keeps keys a user's devices hold:

- **passkeys** (WebAuthn), which log a user in with no password and no
  second factor, and step them up;
- **bound devices**: a key a mobile application makes in the phone's
  Secure Enclave or Android Keystore, which signs requests and steps its
  user up.

The server stores only public keys. Passkeys are checked with
`cryptography` alone: no new dependency.

## Installing it

```python
INSTALLED_DOMAINS = [..., "domainkit.contrib.auth", "domainkit.contrib.devices"]
DEVICES = {
    "RP_ID": "example.com",
    "ORIGINS": ("https://app.example.com",),
}
```

```python
from domainkit.contrib.devices.api import router as devices_router

router.include_router(devices_router, prefix="/auth")
```

Migrate. The device tables must live on the same database as auth's: the
application refuses to start when routing sends them elsewhere. Deleting
a user (`UserDeleted`) removes their passkeys and devices.

| Key | Default | Meaning |
| --- | --- | --- |
| `RP_ID` | `None` | The relying party: your site's domain, which every origin is within. None: no passkeys (`404 passkeys_off`). |
| `RP_NAME` | `None` | The name a browser shows when a passkey is made; None: `RP_ID`. |
| `ORIGINS` | `()` | The pages passkeys are used on (`https://app.example.com`), an app's too (`android:apk-key-hash:...`). Needed with `RP_ID`. |
| `CHALLENGE_TTL` | 5 minutes (at most 10) | How long a challenge works; the browser's timeout too. |
| `MAX_PASSKEYS`, `MAX_DEVICES` | 10, 10 | Keys a user holds at once (`409 too_many_keys` past them). |
| `MAX_PUSH_TOKENS` | 20 | Push tokens a user holds; a new one past it ends the oldest. |
| `SIGNATURE_WINDOW` | 2 minutes (at most 10) | How far a signed request's time may be from the server's. |

## Adding a key needs the user again

Adding a passkey or binding a device needs the user to authenticate
again: their password (`{"password"}`, counted against
`AUTH.LOGIN_RATE` with their other password checks), or a
[step-up](step-up.md) within the last ten minutes. Without it, whoever
held a stolen access token could add a key of their own and keep the
account. Staff acting as the user ([impersonation](routes.md#impersonation))
add no keys. Otherwise: `401 reauthentication_required`.

## Passkeys

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /passkeys/register/options` | a user, authenticated again | The options for `navigator.credentials.create()`. |
| `POST /passkeys/register` | a user | `{"credential": <its toJSON()>, "name"}`: `201` with the passkey. |
| `POST /passkeys/login/options` | anyone | The options for `navigator.credentials.get()`. |
| `POST /passkeys/login` | anyone | `{"credential"}`: `200` with tokens. |
| `POST /passkeys/step-up/options` | a user | The options, offering the user's passkeys. |
| `POST /passkeys/step-up` | a user | `{"credential"}`: a step-up, `amr` `["hwk", "mfa"]`. |
| `GET /passkeys` | a user | The user's passkeys: name, synced or not, made, last used. |
| `DELETE /passkeys/{id}` | a user | `204`. |

```javascript
const options = await post("/auth/passkeys/login/options");
const credential = await navigator.credentials.get({
  publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
});
const tokens = await post("/auth/passkeys/login", {credential: credential.toJSON()});
```

Every ceremony starts with a challenge the server makes and keeps: 256
random bits for one purpose and (but for a login) one user, used once
within `CHALLENGE_TTL`; a refused answer spends it too. The answer is
believed only when its client data is of the right ceremony, for that
challenge, from one of `ORIGINS`, not made in another origin's frame;
its authenticator data names `RP_ID`, with the user present and
verified (`userVerification: "required"`); and its signature holds for
the passkey's key (ES256, EdDSA, or RS256 of 2048 bits or more). A
signature counter that does not move past the last is refused, as a
copied key's would be; passkeys that count nothing (synced ones) are
taken. The attestation statement is not checked (`attestation:
"none"`): a passkey is trusted as the user's because they authenticated
again to add it, not because of who made it. Every refusal answers
`401 passkey_refused`; the reason is logged.

A passkey verified with its user is two factors on its own (possession,
and a biometric or PIN; NIST SP 800-63B): a login with one asks no
second factor, and a step-up with one proves whatever `StepUp` asks
(`mfa`).

## Bound devices

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /devices` | a user, authenticated again | `{"name", "public_key"}`: `201` with the device's `id`. |
| `GET /devices` | a user | The user's devices: name, bound, last used. |
| `DELETE /devices/{id}` | a user | `204`: its signatures hold no more. |
| `POST /devices/step-up` | a user, signed by their device | A step-up, `amr` `["hwk"]`. |

The key is P-256 or Ed25519, as SubjectPublicKeyInfo or, for P-256, the
raw point the Secure Enclave exports, base64url (else
`422 unsupported_device_key`). A request the device signs carries:

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

`DeviceSigned` lets a request through only when a device of the
request's user signed it, within `SIGNATURE_WINDOW`, later than any
request it signed before (each device's last time is kept, so a
signature works once whichever process reads it; a device sending
requests at once gives each its own millisecond). Otherwise
`401 device_signature_refused`:

```python
from domainkit.contrib.devices import DeviceSigned


@router.post("/payments", dependencies=[Depends(Require(DeviceSigned()))])
async def pay(command: PayIn) -> PaymentOut: ...
```

A device's step-up proves `hwk`: a key in the device's hardware, a
possession factor as the authenticator app is, so it stands for the code
(`otp`) a `StepUp` asks, but not for the password.

## Push tokens

| Endpoint | Who | Answer |
| --- | --- | --- |
| `POST /devices/push-tokens` | a user | `{"token", "platform"}` (`fcm`, `apns`, `huawei`): `201`. |
| `GET /devices/push-tokens` | a user | The user's tokens: platform, registered, last seen (never the token). |
| `DELETE /devices/push-tokens/{id}` | a user | `204`, when the app logs out. |

An app registers the token its push service gave it; a token moves to
whoever registers it last (a phone that changed hands), and past
`MAX_PUSH_TOKENS` (20) a user's oldest tokens end. A use case notifies
the user on every app of theirs, through
[`domainkit.contrib.notifications`](../mail.md#text-messages-and-push-notifications):

```python
from domainkit.contrib.devices.push import notify_user

await notify_user(order.customer_id, "Shipped", "On its way", uow=uow)
```

One notification is queued per token, for its platform's backend. A
token its service no longer knows is forgotten when the worker learns so
(`PushTokenExpired`).

## What is audited

`devices.passkey_added`, `devices.passkey_removed`, `devices.device_bound`,
and `devices.device_unbound`, with `domainkit.contrib.audit` installed;
step-ups as `auth.stepped_up`.

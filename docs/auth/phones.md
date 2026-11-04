# Phone numbers

`domainkit.contrib.phones` proves that a user receives text messages at
a number. It sends a code, checks the code that the user types back, and
keeps the number as verified. The text message leaves through the outbox
with the commit, by the `SMS_BACKEND` of `domainkit.contrib.notifications`.

On this page you learn how to:

- install the domain and choose how codes are sent;
- send a code and verify a number over HTTP;
- do the same from a use case or a script;
- know what is limited, and what is stored.

## A first example

Install the domain after auth, messaging, and notifications, run
`makemigrations` and `migrate`, and mount its routes:

```python
from datetime import timedelta

INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.messaging",
    "domainkit.contrib.notifications",
    "domainkit.contrib.phones",
]
NOTIFICATIONS = {
    "SMS_BACKEND": "domainkit.contrib.notifications.sms.TwilioSmsBackend",
    # ... and its options
}
PHONES = {"CODE_TTL": timedelta(minutes=10)}  # every key is optional
```

```python
from domainkit.contrib.phones.api import router as phones_router

router.include_router(phones_router, prefix="/auth")
```

A signed-in user asks for a code:

```http
POST /auth/phones/codes
{"phone": "+33 6 12 34 56 78"}
```

```json
{"phone": "+33612345678", "expires_at": "2026-11-04T18:49:10.205638Z", "resend_at": "2026-11-04T18:40:10.205638Z"}
```

The outbox holds the text message, which the worker sends:

```text
{'message_id': '9bf7b24f-afb8-4e65-804a-85389f3d71ae', 'to': '+33612345678', 'body': 'Your verification code is 039285.', 'sender': None}
```

The user types the code back:

```http
POST /auth/phones
{"phone": "+33612345678", "code": "039285"}
```

```json
{"id": "c8f2b498-b6be-4ed6-b370-2fad585f6ce0", "phone": "+33612345678", "verified_at": "2026-11-04T18:39:10.310062Z"}
```

The phone tables live on auth's database (the application refuses to
start otherwise): a verification records its event on the user in the
same transaction. Deleting a user (`UserDeleted`) removes their numbers
and codes, and `privacy export` and `privacy erase` cover them too.

## Settings

`PHONES = {...}`; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `CODE_LENGTH` | 6 (6 to 10) | Digits in a code. |
| `CODE_TTL` | 10 minutes (at most 1 hour) | How long a code works. |
| `MAX_ATTEMPTS` | 5 (1 to 10) | Wrong tries before the code is spent and a new one must be sent. |
| `RESEND_AFTER` | 1 minute (at most 1 hour) | How long a user waits before another code for the same number. |
| `MAX_SENDS` | 5 (1 to 100) | Codes sent in `SEND_WINDOW` to one number, and asked by one user: each is a text message that someone pays for. |
| `SEND_WINDOW` | 1 day (at most 30 days) | The window of `MAX_SENDS`. |
| `MAX_PHONES` | 5 (1 to 100) | Verified numbers a user holds (`409 too_many_phones` past it). |
| `MESSAGE` | `"Your verification code is {code}."` | The text sent, at most 300 characters. It holds `{code}` once, and no other `{...}`. |

## The routes

| Route | Body | Answer |
| --- | --- | --- |
| `POST /auth/phones/codes` | `{"phone"}` | `202`: the number as kept, `expires_at`, `resend_at`. |
| `POST /auth/phones` | `{"phone", "code"}` | `201`: the verified number, its `id`, and `verified_at`. |
| `GET /auth/phones` | | The user's verified numbers, oldest first. |
| `DELETE /auth/phones/{id}` | | `204`; `404 phone_not_found` for another's. |

Every route needs a signed-in user, with credentials without scopes. A
number is given in international form: `+`, the country code, and at
most 15 digits. Spaces, dots, dashes, and brackets are dropped. Anything
else is `422 invalid_phone` ("Give the number in international form,
such as +33612345678."). `normalize_phone(text)` does the same in code.

## What holds

- **The code is never stored.** The table keeps an HMAC-SHA256 of it,
  under a key derived from `SECRET_KEY` for this purpose alone. So a copy
  of the database does not give the codes away, even by trying all of
  them. Keys in `SECRET_KEY_FALLBACKS` still verify a code sent before a
  rotation.
- **Only the last code works**, for `CODE_TTL`, and for `MAX_ATTEMPTS`
  tries. Each wrong try is counted, and committed, before the answer
  `422 wrong_code` ("That is not the code we sent."). After the last try,
  or when the code is too old, the answer is `422 code_expired`: ask for
  a new one. A right code spends every code of the number.
- **Sends are limited twice.** A second code for the same number within
  `RESEND_AFTER` is `429 rate_limited`, with `Retry-After`. So is a send
  past `MAX_SENDS` in `SEND_WINDOW`, counted for the number (whoever
  asks) and for the user (whatever number). So neither many users nor
  many numbers multiply the messages.
- **A verified number is not sent again.** Asking for a code for a number
  the user verified already is `409 phone_already_verified`.
- **Events name the number by its id.** `PhoneVerified` and
  `PhoneRemoved` carry `user_id` and `phone_id`. The number itself stays
  in its table, out of the outbox and the audit trail.

## Without HTTP

The routes call functions that a script or a use case may call too, in a
`PhonesUnitOfWork` that it commits:

```python
from domainkit.contrib.phones import (
    PhonesUnitOfWork,
    WrongCode,
    confirm_code,
    send_code,
)

async with PhonesUnitOfWork() as uow:
    pending = await send_code(uow, user.id, "+33 6 12 34 56 78")
    await uow.commit()  # the text message leaves with the commit

async with PhonesUnitOfWork() as uow:
    verified = await confirm_code(uow, user.id, "+33612345678", typed)
    await uow.commit()  # a wrong try counts even when it is refused
if verified is None:
    raise WrongCode()
```

| Function | What it does |
| --- | --- |
| `send_code(uow, user_id, phone)` | Makes a code, queues the text message, and returns a `PendingCode` (`phone`, `expires_at`, `resend_at`). Raises `InvalidPhone`, `PhoneAlreadyVerified`, `CodeTooSoon`, or `TooManyCodes`. |
| `confirm_code(uow, user_id, phone, code)` | Returns the `VerifiedPhone` (`id`, `user_id`, `phone`, `verified_at`), or `None` for a wrong code; commit anyway, so the try counts. Raises `InvalidPhone`, `CodeExpired`, `PhoneAlreadyVerified`, or `TooManyPhones`. |
| `remove_phone(uow, user_id, phone_id)` | Forgets a verified number, and records `PhoneRemoved`. Raises `PhoneNotFound`. |
| `normalize_phone(text)` | The number as kept, or `InvalidPhone`. |

`PhonesUnitOfWork` is an `AuthUnitOfWork` with `phones` and `codes`.
`codes` keeps one `PhoneCode` per code sent (its digest, the number, the
tries, and when it expires) in the table `phone_codes`
(`domainkit.contrib.phones.tables`); verified numbers are in `phones`.
`phones_settings()`, in `domainkit.contrib.phones.services`, returns
the validated `PHONES`. To
keep the verified number on the user (a `phone` field of a custom user
model), subscribe to `PhoneVerified` and copy the number there in the
same transaction. Or read `uow.phones.of_user(user.id)` where you need
it. `PhonesSettings` is the settings model.

## Errors and refusals

| Exception | Status and code | When |
| --- | --- | --- |
| `InvalidPhone` | `422 invalid_phone` | Not a number in international form. |
| `WrongCode` | `422 wrong_code` | Not the code sent; a few tries are left. |
| `CodeExpired` | `422 code_expired` | No code works: none was sent, it is too old, or it was tried `MAX_ATTEMPTS` times. |
| `CodeTooSoon` | `429 rate_limited` | A code went to this number within `RESEND_AFTER`; see `Retry-After`. |
| `TooManyCodes` | `429 rate_limited` | `MAX_SENDS` codes in `SEND_WINDOW`, for the number or the user; see `Retry-After`. |
| `PhoneAlreadyVerified` | `409 phone_already_verified` | The user verified this number already. |
| `TooManyPhones` | `409 too_many_phones` | The user holds `MAX_PHONES` numbers. |
| `PhoneNotFound` | `404 phone_not_found` | Not one of the user's numbers. |

## Common mistakes

- **Rolling back after a wrong code.** The try would not count, and a
  guesser would get endless tries. Commit, then raise `WrongCode`.
- **A `MESSAGE` without `{code}`, or with other braces.** The settings
  refuse it.
- **Storing the number in an event.** Events carry `phone_id` only; read
  the number from `uow.phones` where you need it.
- **Forgetting `domainkit.contrib.messaging`.** The text message leaves
  through the outbox, which needs the worker.

## See also

- [Mail and notifications](../mail.md#text-messages-and-push-notifications):
  the SMS backends.
- [Passkeys and bound devices](devices.md): other keys a user holds.
- [Users, groups, backends, and passwords](users.md#a-custom-user-model):
  a `phone` field on the user.

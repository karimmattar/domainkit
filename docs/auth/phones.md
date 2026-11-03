# Phone numbers

`domainkit.contrib.phones` proves a user receives text messages at a
number: it sends a code, checks the code the user types back, and keeps
the number as verified. The text message leaves through the outbox with
the commit, by the `SMS_BACKEND` of `domainkit.contrib.notifications`.

## Installing it

```python
INSTALLED_DOMAINS = [
    ...,
    "domainkit.contrib.auth",
    "domainkit.contrib.messaging",
    "domainkit.contrib.notifications",
    "domainkit.contrib.phones",
]
NOTIFICATIONS = {"SMS_BACKEND": "domainkit.contrib.notifications.sms.TwilioSmsBackend", ...}
PHONES = {"CODE_TTL": timedelta(minutes=10)}  # every key is optional
```

```python
from domainkit.contrib.phones.api import router as phones_router

router.include_router(phones_router, prefix="/auth")
```

Migrate. The phone tables live on auth's database (the application
refuses to start otherwise): a verification records its event on the
user in the same transaction. Deleting a user (`UserDeleted`) removes
their numbers and codes.

| Key | Default | Meaning |
| --- | --- | --- |
| `CODE_LENGTH` | 6 (6 to 10) | Digits in a code. |
| `CODE_TTL` | 10 minutes (at most 1 hour) | How long a code works. |
| `MAX_ATTEMPTS` | 5 (1 to 10) | Wrong tries before the code is spent and a new one must be sent. |
| `RESEND_AFTER` | 1 minute | How long a user waits before another code for the same number. |
| `MAX_SENDS`, `SEND_WINDOW` | 5, 1 day | Codes sent in the window to one number, and asked by one user: each is a text message someone pays for. |
| `MAX_PHONES` | 5 | Verified numbers a user holds (`409 too_many_phones` past it). |
| `MESSAGE` | `Your verification code is {code}.` | The text sent; it holds `{code}` once. |

## The routes

| Route | Body | Answer |
| --- | --- | --- |
| `POST /auth/phones/codes` | `{"phone": "+33 6 12 34 56 78"}` | `202`: the number as kept (`+33612345678`), `expires_at`, `resend_at` |
| `POST /auth/phones` | `{"phone", "code"}` | `201`: the verified number, its `id` and `verified_at` |
| `GET /auth/phones` | | The user's verified numbers, oldest first |
| `DELETE /auth/phones/{id}` | | `204` |

Every route needs a signed-in user. A number is given in international
form (`+`, the country code, at most 15 digits); spaces, dots, dashes, and
brackets are dropped, and anything else is `422 invalid_phone`.

## What holds

- **The code is never stored.** The table keeps an HMAC-SHA256 of it,
  keyed from `SECRET_KEY` for this purpose alone, so a copy of the
  database does not give the codes away, even by trying all of them.
  Keys in `SECRET_KEY_FALLBACKS` still verify a code sent before a
  rotation.
- **Only the last code works**, for `CODE_TTL`, and for `MAX_ATTEMPTS`
  tries: each wrong one is counted (and committed) before the answer,
  `422 wrong_code`. Then, or when it is old, `422 code_expired`: ask for a
  new one. A right code spends every code of the number.
- **Sends are limited** twice: a second code for the same number within
  `RESEND_AFTER` is `429 rate_limited` with `Retry-After`; so is a send
  past `MAX_SENDS` in `SEND_WINDOW`, counted for the number (whoever asks)
  and for the user (whatever number), so neither many users nor many
  numbers multiply the messages.
- **Events name the number by its id.** `PhoneVerified` and
  `PhoneRemoved` carry `user_id` and `phone_id`; the number itself stays
  in its table, out of the outbox and the audit trail.

## Without HTTP

The routes call three functions a script or a use case may call too, in
a `PhonesUnitOfWork` it commits:

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

To keep the verified number on the user (a `phone` field of a custom
user model), subscribe to `PhoneVerified` and copy it there in the same
transaction, or read `uow.phones.of_user(user.id)` where it is needed.

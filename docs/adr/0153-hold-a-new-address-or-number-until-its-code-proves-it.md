# 0153. Hold a new address or number until its code proves it

- Status: Accepted
- Date: 2026-11-16

## Context

A project that runs an identity service on the framework reported (user
report A2): "An identifier nobody proved still receives codes." It
proposed two things: "Send account codes (reset, enrollment) to proved
identifiers only; hold a new identifier apart ("pending") until its code
proves it, instead of storing it on the account unproved."

[ADR 0141](0141-codes-that-open-an-account-go-only-where-it-was-proved.md)
did the first: an address or a number nobody proved opens nothing. The
second was left open.

### What exists

- **No route of the framework changes a user's own address or number.**
  Staff change them through the admin API (`PATCH /users/{id}`), and a
  project's own routes call `AbstractUser.update`, as the project's `/me`
  routes do.
- **`update` replaces the value at once** and clears `email_verified` or
  `mobile_number_verified`. It records `EmailChanged` or
  `MobileNumberChanged` with the value replaced
  ([ADR 0147](0147-name-who-caused-each-event-and-tell-the-old-address.md)).
- **So a change trades a proved value for an unproved one.** Since 0141,
  the user loses password resets and enrollment codes until they prove
  the new one, and the value they proved is gone. A typo in the address
  locks them out of recovery. A stolen session replaces the owner's
  address with one the thief chose: the owner's address no longer logs
  in, and the thief proves theirs at leisure.
- **Verification codes** (`/email-verification`, `/mobile-verification`)
  prove the value on the account. Codes are kept by purpose and target
  (an HMAC of the address or number), for `AUTH.CODE_TTL` (10 minutes by
  default), and are made in the worker
  ([ADR 0126](0126-identify-users-by-the-fields-a-project-chooses-and-verify-them-by-code.md)).
- **Pending signups** already hold an address apart, in a table of their
  own, until its code comes back, and last as long as their code. The
  signup whose code proves an address takes it from an account that never
  proved it
  ([ADR 0133](0133-a-signup-takes-an-address-its-holder-never-proved.md)).

### What others do

- **OWASP's Authentication Cheat Sheet**, changing a registered email
  address: ask for the password or the second factor, "store the proposed
  new email address as a pending change", send a time-limited confirmation
  to the new address, and a notice (with MFA) or a confirmation (without)
  to the current one [1].
- **OWASP ASVS 5.0**: V6.3.7 asks to notify users after a change of their
  username or email address; V6.6.1 offers codes by phone "only when the
  phone number has previously been validated" [2].
- **NIST SP 800-63B-4**: setting or changing the pre-registered telephone
  number is binding a new authenticator (3.1.3.3), which needs
  authentication and a notice through an independent channel (4.1.2.1).
  A recovery address other than one validated at proofing "SHALL be
  established only after the subscriber provides the correct confirmation
  code" sent to it (4.2) [3].
- **django-allauth**: with `ACCOUNT_CHANGE_EMAIL`, a user has one address
  and changes it "by adding a temporary second email address that, when
  verified, replaces the current email address" [4]. Only then is
  `email_changed` sent, and a notice mailed to the old address [5]. A new
  phone number waits in the session, with its code, and replaces the old
  one when the code comes back; the change asks for a fresh login [6].
- **Keycloak**: with email verification on, the Update Email action
  "will send an email with a link to the new email address without
  changing the account email. Only after following the link and
  confirming the email, the email will be updated" [7].
- **Firebase Authentication**: `verifyBeforeUpdateEmail` "sends a
  verification email to a new email address"; "the user's email will be
  updated to the new one after being verified". `updateEmail`, which
  changes it at once, is deprecated, and refused where email enumeration
  protection is on [8] [9].
- **Auth0**: the Management API sets a new address at once; it is
  unverified unless the request says otherwise, and `verify_email` mails
  a verification [10]. It is an administrator's API, as domainkit's admin
  API is.

### Options

| Option | What it costs |
| --- | --- |
| Leave it to each project | Every project writes the same table, codes, swap, and release; the report's project already did, with 0133's methods. |
| Columns on the user's table (`pending_email`, `pending_mobile_number`, and when) | Every project, a custom user table included, migrates its hottest table, read on every request; erasure and export would see them, but so would every query of a user. |
| **A table of its own, one row per user and field, as pending signups have** | One new table, created by `makemigrations`; nothing read on a request; the store deletes the rows with the user's credentials. Export does not list a value held for minutes. |
| New routes (`POST /email-change`, `/email-change/code`) | Two more pairs of routes to learn and mount, for codes that are verification codes all the same. |
| **The verification routes take the new value** | One pair of routes per kind, as before: the code proves an address, whichever it is. A project's rule on the route (such as a step-up) applies to plain verification too. |
| `update` holds the value when the user changes themselves | A domain method that sometimes does not change what it was told to, and cannot send a code. |

## Decision

1. **The verification routes take the new value.** `POST
   /email-verification` takes `{"email"}`, and `POST /mobile-verification`
   `{"mobile_number"}`. The value is checked as `update` checks it, then
   held in `domainkit_pending_identifiers` (`uow.pending_identifiers`):
   one row per user and field, the value, and when it expires. The worker
   sends the verification code to it. Without a body, the routes do what
   they did, or send the code again to the value held. The user's own
   value, given, drops what is held.
2. **The account keeps what it has until the code comes back.** The old
   address or number still logs in, and receives reset and enrollment
   codes if it was proved (0141). While a value is held, `/email-verification/code` (or
   `/mobile-verification/code`) checks the code sent to it. The code
   makes the swap: `AbstractUser.change_email(address)` (or
   `change_mobile_number`) records `UserUpdated`, `EmailChanged` (or
   `MobileNumberChanged`) naming the value replaced, and `EmailVerified`
   (or `MobileNumberVerified`), in that order. The events are recorded
   then, not at the request, so a handler tells the old address once
   there is a change to tell. Codes sent to the old value stop working.
3. **A held value reserves nothing.** As with pending signups, whoever
   proves it first gets it. When the code comes back, another account
   that proved it keeps it: `409 mobile_number_taken` for a number, and
   `409 email_taken` for an address where addresses identify users
   (`AUTH.IDENTIFIERS`). Only the reader of the address learns it is
   taken. An account that holds it unproved gives it up (`release`), as
   to a signup (ADR 0133).
4. **It lasts as long as its last code.** A value is held for
   `AUTH.CODE_TTL`, from its last code: each code sent to it keeps it that
   much longer. Then it is ignored, and the hourly task deletes it. No
   new setting.
5. **Holding a value is managing logins.** In the phone modes, a request
   that holds one runs the `on_manage_logins` checks, so a web session
   cannot start a change, and the phone can
   ([ADR 0136](0136-declare-the-login-check-on-the-route.md)). A request
   that holds one is counted against `AUTH.EMAIL_RATE` per recipient too,
   as every other request that sends a code is.
6. **Staff still set a value at once.** `PATCH /users/{id}` and `update`
   are unchanged: the value replaces the old one, unproved, and
   `EmailChanged` is recorded with the staff member as its actor. Staff
   check who is asking on their side; an unproved value opens nothing
   (0141), and the user proves it with a code. A staff change also drops
   a value the user held for that field, and the codes sent to the value
   it replaced stop working.

The password or a second factor is not asked for in the request: an
account that logs in by a phone or another provider has no password,
and the route takes only credentials not limited to scopes. A project
that wants a recent login adds `StepUp(max_age=...)` to the route with
`select_routes` ([ADR 0145](0145-mount-part-of-a-router-as-a-copy.md)).
No event is recorded at the request: nothing changed yet.

## Consequences

- **A user's own change no longer costs them a proved address.** A typo
  is a code that never arrives, and the old address still works. A
  stolen session cannot replace the owner's address with one it has not
  proved, and in the phone modes cannot start a change at all.
- **The old address is told after the swap**, from `EmailChanged`, as
  ADR 0147 has it. OWASP's notice to the current address at the request
  is not sent, as no event is recorded until something changes: a project
  that wants it adds a check of its own to the route (`select_routes`).
- **Projects make a migration**: `makemigrations`, then `migrate`, before
  the new code serves the verification routes (`docs/upgrading.md`).
- **A project's own route that calls `update` for the user still
  replaces at once**, as staff do. Such a route, as the report's
  project's `/me` routes, moves the address and the number to the
  verification routes, and keeps `update` for the other fields.
- **A held value is personal data for minutes.** It is in no fixture,
  goes with the user's credentials (deletion, erasure, a signup that
  ends the account), and is not in `privacy export`.
- **While a value is held, the code of the current one is refused.** A
  user who wants to prove the value they have gives it again, which
  drops the held one.

## References

1. OWASP, *Authentication Cheat Sheet*, changing a user's registered
   email address:
   <https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html>
2. OWASP ASVS 5.0, V6 Authentication, requirements 6.3.7 and 6.6.1:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x15-V6-Authentication.md>
3. NIST SP 800-63B-4, *Authentication and Authenticator Management*,
   sections 3.1.3.3, 4.1.2.1, and 4.2:
   <https://pages.nist.gov/800-63-4/sp800-63b.html>
4. django-allauth, *Configuration*, `ACCOUNT_CHANGE_EMAIL`:
   <https://docs.allauth.org/en/latest/account/configuration.html>
5. django-allauth, `verify_email` and `emit_email_changed`:
   <https://github.com/pennersr/django-allauth/blob/main/allauth/account/internal/flows/email_verification.py>,
   <https://github.com/pennersr/django-allauth/blob/main/allauth/account/internal/flows/manage_email.py>
6. django-allauth, `ChangePhoneVerificationProcess`:
   <https://github.com/pennersr/django-allauth/blob/main/allauth/account/internal/flows/phone_verification.py>
7. Keycloak, *Server Administration Guide*, Update Email:
   <https://www.keycloak.org/docs/latest/server_admin/index.html>
8. Firebase, *Auth* reference, `verifyBeforeUpdateEmail` and
   `updateEmail`: <https://firebase.google.com/docs/reference/js/auth.md#verifybeforeupdateemail>
9. Google Cloud Identity Platform, *Email enumeration protection*:
   <https://docs.cloud.google.com/identity-platform/docs/admin/email-enumeration-protection>
10. Auth0, Management API, *Update a user*:
    <https://auth0.com/docs/api/management/v2/users/patch-users-by-id>

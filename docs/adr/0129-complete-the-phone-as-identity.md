# 0129. Complete the phone as identity

- Status: Accepted
- Date: 2026-11-08

## Context

[ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md) made the
user's bound phone approve sign-ins. With `CIBA.APPROVE_LOGINS`, a user
with a bound device logs in only from it, or once it approves. Three gaps
remained.

1. **A user with no bound device still logs in with a password,
   anywhere.**
   - The phone's first login, before it holds a key, cannot be told apart
     from any other client, so the mode had to let unbound users through.
   - Until the phone is bound, a web page can log them in with a password.
2. **A web session cannot ask the phone for anything but a sign-in.**
   - `StepUp` on a web route asks for the password or an authenticator
     code: a browser cannot sign with the phone's key.
   - A web action that should need the phone's approval has no way to get
     it.
3. **A transaction (`auth.sca`, PSD2's dynamic linking) is confirmed with
   a code, never on the phone.** The phone is where the user can see
   what they authorize.

Two constraints shape the answer:

- **Boundaries:** contrib apps reach each other only through public
  names. Auth must not import devices, and devices must not import ciba.
- **The project owner's decision:** a user who lost every bound phone is
  recovered by support only (`unbinddevices`), never by self-service.

## Decision

### Enrollment: the first device of a user who has none

The devices app binds the first device of a user who has none, without a
login first.

- `POST /devices/enroll/code` with `{"identifier"}` queues a code. The
  code goes to the identifier, an email address or mobile number of the
  account.
  - It uses auth's codes (HMAC-kept, single use, `AUTH.CODE_TTL`,
    `AUTH.CODE_ATTEMPTS`) for a purpose of its own, `device_enrollment`.
  - The answer is always `202`, and the code is sent by the worker. The
    answer and its timing say nothing about the account.
  - No code is sent to an unknown, inactive or passwordless account, or
    to one that already has a bound device.
- `POST /devices/enroll` with
  `{"identifier", "code", "password", "name", "public_key"}` binds the
  key and logs the user in.
  - The request is signed by the new key, with the same headers and
    message as `DeviceSigned` and no device id yet. That proves the
    caller holds the key.
  - It needs the code and the password: two factors, as a password reset
    with a code needs.
  - It takes nothing a login would ask for. A user who turned a second
    factor on sends a code of it too (`otp`), or the email address and
    the password alone would move the account to an attacker's phone. A
    password staff set is changed first (`new_password`). The signature
    and the password are checked, and the second factor asked for, before
    the code is spent.
  - It is refused while the user has a bound device. A new phone is
    approved from the old one through `/app-sign-in`, then binds as
    usual. A user who lost every phone goes to support, who checks who is
    asking and runs `unbinddevices`. There is no self-service
    replacement.
- The login it issues passes every login gate as any login does. The
  request counts as signed by the device just bound, so the CIBA gate lets
  it through and the project's own gates still judge it.

### Strict mode: `CIBA.APPROVE_LOGINS = "always"`

`True` keeps ADR 0128's rule: users with no bound device log in as
before. `"always"` refuses them too. Every login must then be one of
these:

- signed by a bound device;
- approved from one;
- the enrollment itself.

The web never logs in with a password. A user signs up (`/signup`, a
code), enrolls the phone (`/devices/enroll`), and from then on the phone
is the only way in. `ciba.E005` refuses `"always"` when the enrollment
routes are not served, since nobody could then log in at all.

### Web step-up approved from the phone

A signed-in web session asks the user's phone to approve an action:

- `POST /app-sign-in/step-up` with `{"message"}` (at most 64 printable
  characters, such as "Transfer 250.00 EUR to DE89…") pushes it to the
  phone, with the two digits of `NUMBER_MATCHING`.
- `POST /app-sign-in/step-up/token` with `{"request"}` polls it. Once
  approved, the session that started it is stepped up with `amr`
  `["hwk"]`, as a device step-up records it. Existing `StepUp(...)`
  rules pass, unchanged.
- The request is bound to the user and to the session's credentials. Only
  that session redeems it, and a step-up request never redeems as a login
  (nor a login request as a step-up): each request has a `kind`.

### Transactions confirmed from the phone

- `POST /app-sign-in/transactions/{challenge_id}` asks the phone to
  confirm one of the user's `auth.sca` transactions.
  - The phone is shown the amount, currency, payee and purpose read from
    the stored transaction, never text from the page.
  - Approving confirms the transaction with `amr` `["hwk"]`.
- `POST /app-sign-in/transactions/token` with `{"request"}` answers the
  authorization (`<id>.<code>`), as `/sca/{id}/confirm` does. Spending
  it is unchanged: it is bound to that amount and payee.
- Auth offers this through `auth.integration` (`sca_transaction`,
  `confirm_transaction_with`), so the CIBA app imports no internals.
- A transaction whose `AUTH.SCA.METHODS` a device key cannot prove (one
  that asks for the password too) is refused with
  `409 transaction_needs_other_factors`.
- A transaction confirmed, used, or expired already is refused with
  `409 transaction_closed`. A user whose phone cannot be asked now (none
  bound, `MAX_PENDING` waiting, a recent report) gets
  `409 phone_unavailable`: the session is signed in, so there is nobody
  to hide the answer from, unlike a sign-in's decoys.

### The table

`domainkit_ciba_requests` gains three columns. Projects make the
migration with `makemigrations`.

- `kind`: `sign_in`, `step_up` or `transaction`.
- `credential_digest`: the session a step-up belongs to.
- `sca_challenge_id`: the transaction a request confirms.

## Consequences

- **The whole cycle runs from the phone, with no web password at all
  (`"always"`):** sign up, verify, enroll, log in with biometrics, approve
  web sign-ins, approve web actions, and confirm transactions.
- **Two codes for a new user:** a signup code, then an enrollment code.
  Joining them would need auth to know devices, which the boundaries
  forbid.
- **Recovering a lost phone needs people.** That is slower than
  self-service, by the owner's choice: no code and password alone can
  move an account to a new phone.
- **Every new route follows ADR 0128's rules:** handles that work once,
  guarded updates, no personal data in pushes, and decoys where an
  identifier is named.

# 0141. Codes that open an account go only where it was proved

- Status: Accepted
- Date: 2026-11-15
- Amends: [0133](0133-a-signup-takes-an-address-its-holder-never-proved.md)

## Context

A password-reset code and a device-enrollment code open an account: whoever
types one sets the password, or binds a phone. Both went to the account's
address or number whether its user had proved it or not
(`email_verified`, `mobile_number_verified`), and since 2.2.1 a reset code
that reached one marked it proved
([ADR 0133](0133-a-signup-takes-an-address-its-holder-never-proved.md)).

A project that runs an identity service on domainkit reported what that
allows. Mallory signs up with her own address, then puts the victim's
number on her account, or changes her profile to it. Nobody proves it. Then:

- the service texts the victim's number whenever anyone asks for a reset
  or an enrollment code with it, at the service's cost;
- a victim who types the code they received sets the password of
  Mallory's account, and proves their own number on it.

Signups were already safe: an account is made only once its code came back
from the address or the number. The holes were in the codes that go to an
existing account.

## Decision

Reset and enrollment codes go only to an address or a number its user
proved. `auth.recovery.reaches_proved(user, texted=...)` decides, for the
reset task and for `send_code` (enrollment, and any app's code built on it).
The request answers as before, 202 whoever it names: the worker sends
nothing, and logs the user's id, never the identifier.

`AUTH.CODES_TO_UNPROVED = True` sends as 2.3 did, for projects whose
accounts were made before their identifiers could be proved. With it, a
reset code that reached an address still proves it (ADR 0133). A user model
without the `*_verified` fields counts them proved, as before.

Verification codes are not concerned: proving an address is their purpose.

## Consequences

- **An address or a number put on an account by its holder, unproved,
  opens nothing.** The holder proves it first, by its verification code,
  or loses it to the signup of whoever does (ADR 0133).
- **A user whose address was never proved cannot reset their password by
  it.** Staff-made accounts, and those made before 2.0, are the usual
  case. They log in and prove it; one who cannot gets a password from
  staff (`changepassword`), or the project sets `CODES_TO_UNPROVED`.
- **ADR 0133's advice to an account that never proved its address, to keep
  it by resetting its password, no longer holds by default.** The mail that
  says so (`auth/signup_code_held`) now says to log in and prove it.

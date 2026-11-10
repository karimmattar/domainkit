# 0133. A signup takes an address its holder never proved

- Status: Accepted
- Date: 2026-11-10

## Context

A signup (`POST /signup`, then `/signup/code`) is refused when an account
holds its address or its number. That account may never have proved it:

- staff made it, or the project's own code did;
- its user changed to the address and never confirmed it (a change
  resets `email_verified`).

Then nobody who reads mail at the address can sign up with it: the one
person who could prove it is refused, for an account that never did. A
project that uses the framework reported it. It is one form of the
account pre-hijacking that Sudhodanan and Paverd describe (2022): an
attacker who knows an address claims it first, and its owner cannot.
Microsoft's report traces those attacks to services that let an address
be used before its owner proved it. Bitbucket moves an address to the
account that verifies it, and removes it from the one that held it.

## Decision

### Whoever proves the address gets it

An account that holds the signup's address, or its number, but never
proved it (`email_verified`, `mobile_number_verified` false) does not stop
the signup. The code goes out, and the signup that uses it takes the
address or the number:

- The holder gives it up (`release_email`, `release_mobile_number`),
  recorded as `EmailReleased` or `MobileNumberReleased` in the audit trail.
- The holder's codes sent there stop working.
- The new account has it, verified, as every signup's account does.

A username, and an address or a number an account proved, still stop a
signup. So does a holder that proves it between the mail and the code.

### A holder left with no login is ended

A holder left with nothing that logs it in (`AUTH.IDENTIFIERS`) is ended
as an erased account is (`AbstractUser.erase_personal_data`):

- deactivated, its password made unusable;
- its tokens and second factor deleted.

Its rows and data stay, for staff to see or remove.

### The owner of an old account chooses

The reader of the mail may be the holder's own user, who never confirmed
the address. The code's mail says so (`auth/signup_code_held`): to keep
that account, log in or reset the password instead of using the code. A
reset whose code was sent to the address or the number proves it, so the
account holds it for good from then on.

## Consequences

- **The address's owner can always sign up with it**, and an account made
  with someone else's address, unproved, loses it the day the owner does.
- **A holder can lose its address, or its login,** when someone else
  proves it: only an account that never proved it. The audit trail
  records it.
- **A password reset by code marks its address or number proved.**
- **A project's own `auth/signup_code` override is not used** when an
  account holds the address unproved: override `auth/signup_code_held`
  too.

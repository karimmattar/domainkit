# 0171. Prove a number given at signup by its own code

- Status: Accepted
- Date: 2026-11-16
- Amends: [0133](0133-a-signup-takes-an-address-its-holder-never-proved.md)

## Context

A signup (`POST /signup`) may give an address and, where numbers
identify users (`AUTH.IDENTIFIERS`), a mobile number too. The code goes
to the address, and proves the address alone. Until 2.5 the signup used
it for the number as well:

- **It took the number from an account that held it unproved**, as
  [ADR 0133](0133-a-signup-takes-an-address-its-holder-never-proved.md)
  lets a signup take what its code proved. The code proved no number. An
  account whose only login was that number was ended: deactivated, its
  password unusable, its tokens deleted. Anyone could do it with an
  address of their own and a victim's number, for instance to a
  superuser that `createsuperuser --mobile_number` made, which proves
  nothing ([ADR 0165](0165-let-an-operator-prove-a-superusers-address-or-promote-a-proved-account.md)).
- **The new account got the number, unproved**: a value its holder never
  showed they read, on an account, as in the "trojan identifier" attack
  below.
- **The mail told whether an account used the number.** A number an
  account had proved made the worker mail "an account already uses
  this" (`auth/signup_exists`) instead of the code, to an address the
  asker reads: an oracle for any number. A number held unproved made it
  mail `auth/signup_code_held`, which said that an account held the
  address.

A review of user report A12 found it; it was in 2.4.0.

### What others do

- **Sudhodanan and Paverd** studied account pre-hijacking on 75 services
  (USENIX Security 2022) [1]. Their "trojan identifier" attack adds an
  identifier the attacker does not own to an account; Instagram let an
  account be made with one identifier and another, unverified, attached.
  The root cause of every attack they found is "failure to verify
  ownership of the claimed identifier", and all are mitigated if the
  service verifies it "before allowing any further actions associated
  with the account"; "a similar approach could be used to verify
  ownership of other types of identifiers, such as using text messages
  ... of phone numbers" (6.2.1). As defence in depth, a service tells
  the user which identifiers are linked (6.2.2).
- **OWASP's Authentication Cheat Sheet**: registration answers the same
  way whether the user exists ("A link to activate your account has been
  emailed to the address provided") [2]. **The Forgot Password Cheat
  Sheet**: "Return a consistent message for both existent and
  non-existent accounts", in a consistent time [3].
- **NIST SP 800-63B-4**: a recovery address or number other than one
  validated at proofing "SHALL be established only after the subscriber
  provides the correct confirmation code" sent to it (4.2); setting a
  pre-registered telephone number binds an authenticator (3.1.3.3) [4].
- **django-allauth** keeps a phone number with its verified state, and
  verifies phone numbers by code at signup and login by default
  (`ACCOUNT_PHONE_VERIFICATION_ENABLED`); with enumeration prevention it
  texts an unknown number that no account is on file, rather than answer
  differently [5].
- **Domainkit already holds a user's new number** until its code comes
  back, and moves it then
  ([ADR 0153](0153-hold-a-new-address-or-number-until-its-code-proves-it.md)).

### Options

| Option | What it costs |
| --- | --- |
| Refuse a number with an address at signup | A client that sends both breaks: a change a minor release cannot make. |
| Two codes at signup, the address's and the number's, both needed to make the account | A new signup flow, and a second code in the pending signup; a number its holder cannot read stops the account. |
| Put the number on the account unproved, but take it from nobody, and decide nothing by it | Still a trojan identifier on the new account, and two accounts would hold one number (it is unique). |
| **Make the account with the address; hold the number for a code of its own, as ADR 0153 holds a new number** | No new route, table, or setting: the held number, its text, and `/mobile-verification/code` exist. The user proves the number after the first login, as after a change. |

## Decision

1. **A signup's code proves the address, or a number given alone; nothing
   else.** Only that decides whether the signup can be made, which mail
   the address gets, and what the signup takes from an account that
   holds it unproved (ADR 0133). A number given with an address counts
   for none of these.
2. **The account is made with the address alone.** Then the number given
   with it is held for the new user (`uow.pending_identifiers`) and the
   worker texts it a verification code, as `POST /mobile-verification`
   with a number does. The user, logged in, sends the code to `POST
   /mobile-verification/code`: the number is then on the account,
   proved, and moves from an account that held it unproved; or the code
   answers `409 mobile_number_taken` where another account proved it
   first.
   Only the number's reader learns that it is taken.
3. **The text counts against `AUTH.EMAIL_RATE` per number**, as `POST
   /mobile-verification` counts it, so signups with addresses of one's
   own cannot text a number without bound. Past the rate, or where it
   cannot be counted, the account is made without the number; its user
   asks for a code later.
4. **A signup by a number alone is unchanged**: its code goes to the
   number, and proves it.

`409 signup_taken` and `auth/signup_exists` name the username where the
signup chose one, and the address or a number given alone: never a
number given with the address.

## Consequences

- **Nobody loses a number to a signup that did not prove it**, and no
  account is ended by one. A number reaches an account only once its
  reader typed its code.
- **The signup's answer and mail are the same whether the number has an
  account**: the oracle is gone. The number's reader is told, after
  proving it.
- **An app that signs up with both asks for the number's code after the
  first login.** The account has no number until then, so it does not
  log in by it. `docs/upgrading.md` says so.
- **A held number lasts as long as its code** (`AUTH.CODE_TTL`); a user
  who misses it asks again with `POST /mobile-verification`.
- **ADR 0133 stands for what a code proves**: an address held unproved
  still moves to the signup that proves it, and a number to a signup by
  that number alone.

## References

1. A. Sudhodanan and A. Paverd, *Pre-hijacked accounts: An Empirical
   Study of Security Failures in User Account Creation on the Web*,
   USENIX Security 2022, sections 4, 5.3.2 and 6.2:
   <https://www.usenix.org/conference/usenixsecurity22/presentation/sudhodanan>
2. OWASP, *Authentication Cheat Sheet*, authentication responses:
   <https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html>
3. OWASP, *Forgot Password Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html>
4. NIST SP 800-63B-4, *Authentication and Authenticator Management*,
   sections 3.1.3.3 and 4.2: <https://pages.nist.gov/800-63-4/sp800-63b.html>
5. django-allauth, *Phone*, and the account adapter's phone methods:
   <https://docs.allauth.org/en/latest/account/phone.html>,
   <https://docs.allauth.org/en/latest/account/adapter.html>

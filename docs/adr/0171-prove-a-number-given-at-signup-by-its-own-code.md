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

1. **A signup's code proves one value, which only it decides on.** It
   goes to the address; or to the number, where the number is given
   alone, or with an address where neither the address nor a username
   given identifies users (`AUTH.IDENTIFIERS`): an account made with the
   address alone could not log in there. That value alone decides
   whether the signup can be made, which mail or text it gets, and what
   it takes from an account that holds it unproved (ADR 0133).
2. **The account is made with that value; the other is held.** The other
   value given (the number, or the address) is held for the new user
   (`uow.pending_identifiers`), and the worker sends it a verification
   code, as `POST /mobile-verification` (or `/email-verification`) does
   with a value in the body. The user, logged in, sends the code to its
   verification route: the value is then on the account, proved, and
   moves from an account that held it unproved; or the code answers
   `409 mobile_number_taken` (`email_taken`) where another account
   proved it first. Only the value's reader learns that it is taken.
   Holding it runs the `on_manage_logins` checks, as that route does.
3. **Each text or mail is counted per recipient.** A held value counts
   against `AUTH.EMAIL_RATE` per value among signups (the count a signup
   by that value spends), apart from the count of the value's owner's
   own requests, so signups do not spend it. Asking again for a code
   with no body (`POST /mobile-verification`) counts the value the code
   goes to, which a held value had escaped. So a number gets at most
   `EMAIL_RATE` texts from signups, and as many from requests to verify
   it, in each period. A per-recipient count is one anyone can spend:
   accounts that ask for one number spend that number's count of
   verification requests, its owner's included.
4. **A verification code is its user's own.** Codes were one per purpose
   and value, so Mallory's signup with the owner's number replaced the
   owner's code. Email and mobile verification codes are now kept per
   user too (`user_target`): two users' codes for one value never
   replace each other.
5. **Past the rate, where it cannot be counted, or where a check
   refuses, the account is made without the other value**, and nothing
   says so: `/signup/code` answers `204`. Its user asks for a code later,
   with the value in the body. A held value lasts as long as its last
   code (`AUTH.CODE_TTL`); once it lapses, `POST /mobile-verification`
   with no body sends nothing (the account has no number), and answers
   `202` as for a number proved already.
6. **A signup by a number alone is unchanged**: its code goes to the
   number, and proves it.
7. **`mobile_number` comes on its own.** Where it identifies users and
   `REQUIRED_FIELDS` names it, a signup gives it as `mobile_number`,
   never under `fields`, where it was stored unproved, nothing was
   texted, and the answer of `/signup/code` told whether an account had
   it; `fields` naming it is `422 unknown_user_field`, and a signup
   without it `422 invalid_user_field`.

`409 signup_taken` and `auth/signup_exists` name the username where the
signup chose one, and the value the code proves: never the held one.

## Consequences

- **Nobody loses an address or a number to a signup that did not prove
  it**, and no account is ended by one. A value reaches an account only
  once its reader typed its code.
- **The signup's answer, mail, and text are the same whether the held
  value has an account**: the oracle is gone. Its reader is told, after
  proving it.
- **An app that signs up with both asks for the held value's code after
  the first login.** The account does not log in by it until then.
  `docs/upgrading.md` says so. Where addresses identify no one, the
  signup's code is texted, which 2.4 mailed.
- **Verification codes issued before the upgrade stop working**: they
  were kept under the value alone. They last `AUTH.CODE_TTL`.
- **ADR 0133 stands for what a code proves**: a value held unproved
  still moves to the signup that proves it.
- **A review of the first version** found that a signup with an address
  and a number could never finish where addresses identify no one (it
  was made with the address alone), that re-asking and signups shared
  or escaped the counts, and that `REQUIRED_FIELDS` let the number round
  this decision. Decisions 1, 3, 4, 5 and 7 answer them.

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

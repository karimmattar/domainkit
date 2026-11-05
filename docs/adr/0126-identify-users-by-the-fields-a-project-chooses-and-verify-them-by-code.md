# 0126. Identify users by the fields a project chooses, and verify them by code

- Status: Accepted
- Date: 2026-11-04

## Context

Until 1.7 a user is found by one field, `USERNAME_FIELD` (`username` on
the built-in `User`), which every user must have. Signup, email
verification, and password resets send a signed link by email.

Many services have no usernames. People log in with their email address
or their mobile number, and they confirm an address by typing a short
code the service sent, not by opening a link: a code works in the app
that asked for it, on a phone whose mail client opens links elsewhere,
and by text message, where a link is a phishing pattern.

The options weighed:

- **One fixed identifier per model** (Django's `USERNAME_FIELD` set to
  `email`). Simple, but a project that accepts email *or* mobile number
  needs a custom backend, and every flow (signup, reset) must be written
  again for it.
- **A list of identifier fields, in a setting.** Every flow reads the
  same list: login, signup, password reset, `createsuperuser`, the admin
  API. A project chooses `("email",)`, `("email", "mobile_number")`, or
  its own field.
- **Codes instead of links, at once.** Links are what 1.x projects and
  their front ends use today. The versioning policy lets a minor release
  add, not break, so links must keep working in 1.x.

## Decision

1. **`AUTH.IDENTIFIERS`** names the fields that identify a user, in the
   order a login tries them, such as `("email", "mobile_number")`. Its
   default is `(USERNAME_FIELD,)`, so an existing project behaves as
   before. A value is matched to a field by its form: one with `@` is an
   email address, one that normalizes to an E.164 number is a mobile
   number, anything else is tried against the other fields. Each field
   must exist on the user model; startup refuses a setting that names
   one that does not (`auth.E011`).
2. The built-in **`User` gets `mobile_number`** (E.164, unique when set),
   and its **`username` becomes optional** (unique when set, in any
   case). A user needs at least one identifier of `AUTH.IDENTIFIERS`.
   An identifier is unique among users in any case: for `username` and
   `mobile_number` the table says so; for `email`, which existing tables
   may hold twice, the repository refuses a second user with the same
   address, under a lock on the address on PostgreSQL. An existing
   project runs `makemigrations` and `migrate` once.
3. **`AUTH.VERIFICATION`**, `"link"` or `"code"`, chooses how signup,
   email verification, and password resets prove that someone reads an
   address or a number. Its default is `"link"`, as in 1.7; `init` writes
   `"code"` for new projects. Links are deprecated, and go in 2.0.
4. **A code** is 6 digits from `secrets`, kept only as an HMAC keyed from
   `SECRET_KEY`, for one purpose and one identifier (`domainkit_auth_codes`).
   It works for `AUTH.CODE_TTL` (10 minutes), is spent after
   `AUTH.CODE_ATTEMPTS` (5) wrong tries, and is used once; a new code
   replaces the one before. Codes go by email, or by text message
   through `domainkit.contrib.notifications` for a mobile number.
5. Every flow keeps what links gave: a request answers the same, and
   takes the same work, whether or not an account uses the identifier;
   whoever proves the address chooses the password; every attempt counts
   against the same rates, and refusals go to the audit trail.

## Consequences

- New projects log in by email or mobile number, with codes, and no
  username. Existing projects change nothing until they set
  `AUTH.IDENTIFIERS` and `AUTH.VERIFICATION`.
- A 6-digit code can be guessed, where a signed link cannot: the attempt
  limit, the short lifetime, and the rates per client and per identifier
  are what keep a guess to 1 in 200,000 per code.
- `get_username()` returns the user's first identifier that is set, so
  events, the audit trail, and the admin API name a user without a
  username.
- Email uniqueness is the application's, not the table's, until 2.0 adds
  the index: rows written outside the repository can still repeat an
  address.

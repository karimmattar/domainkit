# 0068. Count failed attempts, and hold back only what can be guessed

- Status: Accepted; amended 2026-10-25 (at the end)
- Date: 2026-10-25

## Context

Logins, second-factor codes, and links from mail are throttled per account
and per client address (`AUTH.LOGIN_RATE`, `AUTH.MFA.RATE`), so that a
password or a one-in-a-million code cannot be guessed at speed. Until
0.9.1 every attempt counted, successes too, and refused bearer tokens held
back every credential of their address. The 0.9.0 audit showed what that
costs where many people share an address (an office, a VPN, a mobile
carrier, a proxy without `TRUSTED_PROXIES`):

- `MFA.RATE` (10 an hour) counted every code checked at `/mfa/login` per
  address: the whole address had ten second-factor logins an hour, and
  anyone there could spend them with wrong codes (AUTH-02).
- `LOGIN_RATE` counted logins that worked: a service account that logs in
  often throttled itself.
- Four bad bearer tokens held the address, and its browser sessions were
  refused with them (AUTH-03).

Options considered:

- **Higher rates.** Raises the attacker's cost and the honest users'
  ceiling alike; the guess budget grows with it.
- **Peek, then count failures.** A backend reads the count without
  changing it, and a failure is counted after the check. Requests sent at
  once all peek before any counts, so a burst gets as many guesses as it
  has requests: the bound no longer holds.
- **Count, then give back.** Each attempt is counted before the check, as
  before, and a check that holds gives its count back. The bound on
  failures is exact under concurrency, since every attempt is counted
  atomically before it is made; only the attempts that fail stay counted.
- **Per-account lockout in the database.** Durable, but a schema change
  and an unlock workflow: work for a minor release.

What cannot be guessed needs no count before the check: a signed link or
challenge (HMAC-SHA256 under `SECRET_KEY`), a session cookie of 256
random bits that leaves a request anonymous when it does not hold, or a
client certificate, which takes its private key.

## Decision

- Password and code checks count before the check and give the count back
  when it holds (`throttling.attempt`, `Attempt.succeeded`). A count that
  one key of an attempt refuses is given back to the keys before it: the
  attempt was not made.
- Backends may give a request back with `refund(key, limit, period)`:
  `MemoryBackend` lowers what the caller owes by one request,
  `RedisBackend` moves its arrival back one interval in a Lua script on the
  server's clock (deleting a caller that owes nothing). A backend without
  `refund` keeps counting every attempt, as before.
- At `/mfa/login`, a code counts per user (`MFA.RATE`) and per address
  (`MFA.ADDRESS_RATE`, 60 an hour), only once the challenge holds.
- Signed tokens that do not hold (second-factor challenges, reset,
  verification, and signup links) are counted per address after the
  check, against `LOGIN_RATE` (`throttling.count_failure`): the count
  bounds the work and the audit rows a flood costs, and never refuses a
  valid token.
- The refused-credential hold refuses requests with an `Authorization`
  header unread, as before, and other credentials only from classes that
  are `guessable` (the default). `CookieAuthentication` and
  `ClientCertificateAuthentication` are not.
- `Decision.counted` says whether a backend counted a request. With
  `AUTH.THROTTLE_FAIL_CLOSED`, a password or code check, or a request for
  mail, it could not count is refused with `503 throttle_unavailable`; by
  default it is let through, as before.

## Consequences

- An address shared by many honest users is held back only by failures,
  and a guesser at that address needs the password of each account whose
  codes it spends. Anyone can still spend an account's own count with
  wrong passwords; that is the price of a per-account bound.
- A successful login costs one more round trip to the backend (the refund).
- Forged links and challenges are checked even past their count (one
  signature and one indexed lookup each); `RATE_LIMIT` bounds them.
- A project's own backend that wants the new behaviour implements
  `refund`; one that does not keeps the old.
- A per-account lockout with audited unlock stays for 0.10; it will rest on
  these counts of failures.

## Amendment (2026-10-25): what the counts do not stop

The first wording of this change said that a user who logs in often, or
an office behind one address, "is never refused for logging in". That
holds for their own logins only. A security review of 0.9.1 (SEC-r1-2)
showed, as was already true in 0.9.0:

- `LOGIN_RATE` wrong passwords a period for a username, sent from any
  addresses, fill the account's count, and its user's right password is
  then refused with 429 before it is checked. The count frees a place
  every `period / limit` seconds, so one wrong password that often keeps
  the user out for as long as it is sent.
- `LOGIN_RATE` wrong passwords from one address, for any usernames, fill
  the address's count, and every login from that address is refused the
  same way: an office or a carrier behind one address can be kept out by
  one person there.

Both follow from counting before the check, which is what keeps the bound
on guessing one account from many addresses exact; giving successes back
does not change them. 0.9.1 changes no behaviour here: its docstrings,
`docs/auth.md`, and CHANGELOG now say what holds and what does not. The
per-account lockout planned for 0.10 is to be stored in the database, with
an audited unlock, and to let the account's user in by a way a stranger's
failures cannot use up, without checking more guesses per account.

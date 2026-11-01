# 0122. Step up in a rule, and bind authorizations with a stored row and a code

- Status: Accepted
- Date: 2026-10-31

## Context

The fintech audit left three items of authentication open after 0.10's
second factor: step-up (FIN-30: an act that needs the user to
authenticate again, recently), strong customer authentication with
dynamic linking (FIN-29: PSD2's RTS, Article 5, binds the authentication
of a payment to its amount and payee, and any change must make it fail),
and PCI DSS v4.0's passwords (FIN-42: 12 characters, letters and digits,
a history of four, a change at first use). Four questions decided the
design.

**Where a step-up is a rule.** Other apps take access rules (the approvals
app's approve route takes any rule), and routes, `ResourceRouter`
actions, and WebSockets check them through `Require`. A step-up that is a
dependency of its own would fit none of them. A rule answers only
allowed or not, and a refused known user got a `403` the client cannot
act on; a step-up's refusal must say "authenticate again", as RFC 9470
writes it (`401`, `error="insufficient_user_authentication"`,
`max_age`).

**Where a step-up is kept.** Opaque login tokens and browser sessions are
rows of `domainkit_tokens`, read through the principal cache (ADR 0058);
JSON Web Tokens are read from their claims alone. Columns on the token
table would put the step-up in every credential read: every request of
every project would read them, so a project that had not migrated would
fail on its first request, for a feature it may never use, and the cache
would hold a step-up that another process just recorded for up to its
`TTL`, refusing the user who just stepped up.

**What counts.** OpenID Connect's `auth_time` is the last authentication;
a login with a second factor would be one. A login is not the act,
though: the point of a step-up is a factor proven for the act, and a
login's methods are not recorded anywhere a later request reads.

**How an authorization is bound.** Two designs were asked for: a stored
challenge row, or a signed token over a canonical encoding of the amount,
currency, payee, and a nonce. A proof of concept (`binding.py`, in the
work package's notes) built three, and attacked each five ways: a replay,
another amount, another payee, the stored amount edited after the user
confirmed it (an insider, or a bug, with write access to the auth
tables), and 20 concurrent uses of one authorization.

| Design | Replay | Amount | Payee | Row edited | Concurrent uses that won |
| --- | --- | --- | --- | --- | --- |
| A: signed token over the canonical encoding, nothing stored | **works** | fails | fails | (nothing stored) | **20 of 20** |
| B: stored row, values compared with the row | fails | fails | fails | **works** | 1 of 20 |
| C: stored row, once, and an HMAC over the canonical encoding of the confirmed values | fails | fails | fails | fails | 1 of 20 |

A signed token alone cannot be spent once: "once" is state, and the
store of spent nonces it then needs is design B's row by another name.
A row alone binds only what the row says when it is spent, so an edit
after the user confirmed is authorized as if the user had seen it.

## Decision

- **`StepUp(max_age=...)` is an access rule** of `domainkit.contrib.auth`
  (it reads this app's credentials, as `IsAdminUser` reads its users;
  ADR 0080), with `methods` defaulting to `AUTH.STEP_UP.METHODS`
  (`["otp"]`, or `["pwd", "otp"]`; `otp` is required). The core's
  `BasePermission` gains `refusal(request, user)`: a rule that refuses may
  name the error it answers with, in place of the `403`, unrecorded as a
  denial; `AllOf` passes on that of its first refusing rule, `AnyOf` and
  `Not` none. `StepUp` answers `401 step_up_required` with RFC 9470's
  header. An open WebSocket or event stream keeps what its start proved.
- **Step-ups of opaque credentials live in a table of their own**,
  `domainkit_step_ups`, one row per login token or session (by digest),
  read by `StepUp` alone, in one query, only on the routes that ask, and
  never cached: a step-up holds at once in every process. A JSON Web
  Token login steps up into a new access token of the same login, with
  `auth_time` and `amr`; a refresh issues tokens without them, so the
  step-up lasts one access token.
- **Only `POST /step-up` steps up**, with an authenticator's code (never
  a recovery code, never spent trying one) and the password where asked,
  counted with the user's other code and password checks
  (`AUTH.MFA.RATE`, `AUTH.LOGIN_RATE`), recorded (`SteppedUp`) and
  audited when refused. A login is not a step-up.
- **Authorizations are design C** (`domainkit.contrib.auth.sca`): a row
  of `domainkit_sca_challenges` holds the user, the amount (`Money`, two
  columns), the payee, the purpose, and the expiry (`AUTH.SCA.TTL`, 5
  minutes); confirming it with a factor returns `<id>.<code>`, the code
  an HMAC-SHA256, under a key derived from `SECRET_KEY`, of a JSON array
  of the encoding's name, the id (a random nonce), the user, the
  currency's code, the amount in fixed point with its places, the payee
  as given, and the purpose. `consume_authorization` recomputes the code
  from the values the use case is about to act on, checks it against the
  row's too, and marks the row used in one `UPDATE ... WHERE consumed_at
  IS NULL`. Values other than those confirmed void it. It is spent in a
  transaction of its own, before the transaction acts: at most once.
- **PCI DSS v4.0 is a preset**, `AUTH.PASSWORD_PRESET = "pci-dss-4"`,
  that fills in what `AUTH` leaves unset (the validators with 12
  characters and `LettersAndDigitsValidator`, `PASSWORD_HISTORY` 4,
  `PASSWORD_CHANGE_ON_FIRST_USE`, `LOGIN_RATE` `"10/hour"`, and
  `IDLE_TIMEOUT` 13 minutes, which ends a login by 14.3 minutes, within
  8.2.8's 15), and refuses to start, with `auth.E010`, where an explicit
  setting contradicts it. The history keeps hashes in
  `domainkit_password_history`, checked where a password is stored. 8.3.4
  stays answered by device proofs, without a stored lockout (ADR 0087).

## Consequences

- Nothing changes by default: no setting's default moved, and the new
  tables are read and written only by the new routes and settings, by the
  hourly purge and `cleartokens`, and when a user's credentials are
  deleted (deleting or erasing a user). Those last two need the tables
  migrated, as every new table does.
- A rule of a project's own can now tell a client what to do on refusal;
  one that returns an error from `refusal` should be one a client can
  act on, since it is not recorded as a denial.
- A step-up costs one query on the routes that ask for it; the principal
  cache keeps no step-up, so none goes stale.
- A JSON Web Token's step-up does not survive a refresh. A project whose
  access tokens live long should keep `max_age` shorter than they do.
- The authorization is bound to the payee's exact text: a project that
  normalizes payees (an IBAN's spaces, its case) does so before asking,
  and again before spending.
- An authorization spent on a transaction that then fails is gone: the
  user confirms again. The opposite (spend after acting) would let a
  crash between the two act twice.

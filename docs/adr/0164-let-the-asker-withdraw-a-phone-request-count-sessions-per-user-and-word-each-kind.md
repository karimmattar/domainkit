# 0164. Let the asker withdraw a phone request, count sessions per user, and word each kind

- Status: Accepted
- Date: 2026-11-16
- Amends: [0144](0144-authorize-one-act-not-a-window-of-time.md), [0146](0146-show-gates-how-a-login-was-made-and-let-a-login-keep-shorter-limits.md)

## Context

A project that runs an identity service on the framework confirms its
staff's acts on their phones ([ADR 0144](0144-authorize-one-act-not-a-window-of-time.md))
and signs its dashboard in with ciba's push and QR sign-ins. Its third
report, against 2.4.0, lists five gaps in ciba:

1. **A15. A session's asks are counted per address.** `count_start`
   counts every start against `START_RATE` by the client's address, as an
   anonymous QR start is counted. `POST /app-sign-in/authorizations/{id}`,
   a signed-in session asking the phone to confirm one act, calls it too.
   Every person behind one address, an office's staff, shares 10 acts a
   minute. The project's tests raise `START_RATE`.
2. **A16. A page cannot withdraw what it asked.** No route lets the
   initiator cancel; denying is the phone's. What a session asks counts
   against `MAX_PENDING` until it expires (2 minutes): two acts closed on
   the page make the third `409 phone_unavailable`. Clearing them from
   the phone with "Not me" reports them, and a report stops the user's
   pushes for `REPORT_COOLDOWN`.
3. **A17. An `on_approve` refusal leaves the request waiting.** The
   phone is answered `403`, and the page polls on until the request
   expires. The project's hook calls `deny()` itself before it raises.
4. **A11. A login gate cannot tell which sign-in a login redeems.**
   `login_origin(request)` gives the method, `amr`, the device and the
   time ([ADR 0146](0146-show-gates-how-a-login-was-made-and-let-a-login-keep-shorter-limits.md)),
   but not the request, nor its binding message, which an `on_start`
   hook set per page ("Sign in to the dashboard"). The project's gate
   reads the redeem's `Origin` header instead.
5. **I8. One push text for every kind.** `PUSH_TITLE` and `PUSH_BODY`
   ("Approve sign-in?") are sent for a sign-in, a step-up, a transaction
   and an act alike ([ADR 0149](0149-write-codes-and-pushes-in-their-readers-language.md)
   translates them). The project set both to "Approve?".

### What others do, and what the standards say

- **Rate limits.** Counting by address punishes everyone behind a NAT, a
  carrier's or an office's; once a request carries an identity, the
  common advice is to count it per user, and keep counts by address for
  what is anonymous [1][2]. OWASP lists unbounded resource use as an API
  risk (API4:2023), and asks to limit "how often a client can interact
  with the API", without fixing what a client is [3].
  Okta's client-based limits exist for the same reason: one office, one
  address, many users [4].
- **Withdrawing a request.** OpenID CIBA Core has no cancellation: an
  initiator that no longer wants the answer stops polling, and keeps
  `expires_in` "to clean up authentication requests" [5]. Providers add one: HID's token endpoint deletes a
  pending push request (`ciba_delete`) [6]; cidaas cancels an
  authentication request [7]; Okta's Authentication API cancels a
  transaction and revokes its state token [8]. None reports the user.
- **A refusal is an answer.** In CIBA a denial ends the request, and the
  initiator's next poll gets `access_denied` [5]. ciba already ends a
  request on a wrong number, and on an act whose digest changed.
- **Push context.** Microsoft Authenticator shows the application's name
  and the sign-in's location in its notifications, against push fatigue
  [9]; the user should know what a prompt is for before they open it.
  ciba keeps personal data out of a push (it goes through Apple's or
  Google's servers), so the kind of request is what a push can say.

### Options

A15, what a session's ask is counted by:

| Option | What it costs |
| --- | --- |
| Per address, as today | An office shares one count. |
| **Per user, at `START_RATE`** | One user's sessions share it, from any address. No new setting; a project that set `START_RATE` keeps its value. |
| Per user, at a new `ASK_RATE` (10 a minute) | A project that raised or removed `START_RATE` for its acts is limited again, at 10. |
| Per user and per address | The office's count again. |

A16, what withdrawing does:

| Option | What it costs |
| --- | --- |
| Deny it, as the phone does | The page reads `access_denied`, "the phone denied it"; the audit says the user denied what they never saw. |
| A new `cancelled_at` column | A migration, and a fifth state every reader learns. |
| **End it now (`expires_at` = now)** | No migration. The phone's list, `MAX_PENDING`, the poll, the claim and the purge already read the expiry. The page reads `expired_token`. |

Who may withdraw: whoever polls. The poll handle is the initiator's
alone (ADR 0128), and a session's request also names the credentials
that asked: the same check as the poll.

A17, which refusals deny:

| Option | What it costs |
| --- | --- |
| Every exception | A hook that fails (its database is down: a 5xx) ends a request the phone could approve again. |
| **A refusal: a `DomainError`, or an HTTP error under 500** | A hook's own failure leaves the request waiting, as today. |

A11, how the login names its request:

| Option | What it costs |
| --- | --- |
| New arguments of `pass_login_gates`, `issue_login`, `start_browser_session` | Two arguments on three functions, passed twice by each route. |
| **Noted in the request's scope (`note_login_sign_in`), as the device is (`note_login_device`)** | One call where the request is redeemed; the gates and `on_session_limits` read it. |
| Kept with the login, in the token table | A migration; nothing asked for it after the login is made. |

I8, where a kind's text comes from:

| Option | What it costs |
| --- | --- |
| Each kind's own default, `PUSH_TITLE` and `PUSH_BODY` the sign-in's | A project that set them for every kind ("Approve?") gets the defaults for the others. |
| **`PUSH_TEXTS[kind]`, then `PUSH_TITLE`/`PUSH_BODY` when set (they now default to None), then the kind's own** | A project that set them keeps its words for every kind; one that did not gets each kind's. |

## Decision

- **A session's asks are counted per user.** `count_start(request, *,
  user=None)`: with `user`, the key is the user's (`ciba-ask:user:<id>`),
  at `START_RATE`, and a refusal says "Too many requests were sent to
  your phone". The act, transaction and step-up routes pass the user. A
  page's push and QR starts, and a new phone's request to be bound, stay
  counted per address.
- **The initiator withdraws a request.** `cancel(handle, *, client_id,
  kind="sign_in", credential_digest=None)` checks the handle as `poll`
  does, and ends the request now, unless it was redeemed: the phone no
  longer lists it nor can answer it, it no longer counts against
  `MAX_PENDING`, nobody redeems it, and nothing is reported or audited.
  The routes: `POST /app-sign-in/cancel` (a page's sign-in, public),
  `/app-sign-in/authorizations/cancel` and
  `/app-sign-in/transactions/cancel` (the session that asked). They
  answer `204`, the same for a decoy and for a request answered or ended
  already; an unknown or redeemed handle, or another's,
  `400 sign_in_unknown`. The deprecated step-up gets none: it goes in
  3.0. The phone's own "Deny" without `report` already reports nothing.
- **A refusal denies.** An `on_approve` hook that raises a `DomainError`,
  or an HTTP error under 500, denies the request, unreported
  (`SignInDenied`), before its refusal is answered: the poll and the
  wait answer `access_denied` at once. A request the hook denied itself
  stays as it is. Another error refuses nothing, and denies nothing.
- **A login names the sign-in it redeems.** `LoginOrigin` gains
  `sign_in_request_id` and `binding_message`, None by default.
  `note_login_sign_in(request, request_id, binding_message)`
  (`auth.integration`) notes them in the request's scope;
  `login_origin(request)` and the origin `on_session_limits` gets read
  them. ciba notes the request when `/app-sign-in/token` or `/session`
  redeems it. The login does not keep them: `Credentials.origin` has
  neither.
- **Each kind has its words.** A sign-in: "Approve sign-in?"; a
  step-up: "Confirm it's you?"; a transaction: "Approve a transaction?";
  an act: "Approve an action?", each with a body that says what the app
  shows. domainkit's catalogs translate them into Arabic.
  `PUSH_TEXTS = {kind: {"TITLE", "BODY"}}` names a kind's;
  `PUSH_TITLE` and `PUSH_BODY` default to None, and, set, stand for
  every kind `PUSH_TEXTS` names none for. Each field falls back on its
  own.

## Consequences

- An office's staff each confirm `START_RATE` acts a minute. One user is
  limited across addresses: their sessions on two networks share one
  count, where each address had one before.
- A page closes its dialog and asks the phone again at once. The push
  already delivered stays in the phone's notifications; the app finds
  the request gone, and approving it is `404 sign_in_not_found`. A
  withdrawn act's authorization stays: the session asks the phone again,
  or confirms it another way.
- A withdrawn request reads as expired: its row keeps when it was
  withdrawn, not when it would have expired, and is forgotten a day
  later, as any.
- A hook's refusal ends the request: the user who wants to try again
  starts a new one. A hook that denied it itself keeps working.
- A gate can tell the dashboard's sign-ins by their binding message, as
  `on_approve` could. Whoever starts a sign-in chooses the page, so a
  gate that lets more through for a message trusts the starter.
- A project that set `PUSH_TITLE` or `PUSH_BODY` sees no change; one that
  did not sees each kind's words. Code that read `PUSH_TITLE` gets None
  where it was not set.

## References

1. Zuplo, [Don't rate limit by IP address](https://zuplo.com/blog/dont-rate-limit-by-ip).
2. Kevin Cox, [Per-User Rate Limiting](https://kevincox.ca/2021/04/14/per-user-rate-limiting/).
3. OWASP, [API4:2023 Unrestricted Resource Consumption](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption).
4. Okta, [Client-based rate limiting](https://developer.okta.com/docs/reference/rl-clientbased/).
5. OpenID Foundation, [Client-Initiated Backchannel Authentication Core 1.0](https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html), sections 7.3, 7.4 and 11.
6. HID Global, [Bcauthorize Endpoint](https://docs.hidglobal.com/dev/auth-service/api/bcauthorize-endpoint.htm).
7. cidaas, [Cancel auth request](https://docs.cidaas.com/openapi/authentication/cancel-auth-request).
8. Okta, [AuthenticationClient.CancelTransactionAsync](https://developer.okta.com/okta-auth-dotnet/latest/api/Okta.Auth.Sdk.AuthenticationClient.html).
9. Microsoft, [Authentication methods enabled by default](https://learn.microsoft.com/en-us/entra/identity/authentication/concept-authentication-default-enablement): additional context in Authenticator notifications.

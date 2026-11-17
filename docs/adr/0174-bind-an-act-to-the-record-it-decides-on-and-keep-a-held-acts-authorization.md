# 0174. Bind an act to the record it decides on, and keep a held act's authorization

- Status: Accepted
- Date: 2026-11-17
- Amends: [0144](0144-authorize-one-act-not-a-window-of-time.md), [0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md), [0164](0164-let-the-asker-withdraw-a-phone-request-count-sessions-per-user-and-word-each-kind.md)

## Context

A project that confirms its back office's acts on the phone (ADR 0144)
reported four gaps against 2.6 (user reports A24 and A17).

- **An act binds its command, not the record it decides on** (A24). The
  digest is `fingerprint(purpose, arguments)`, the command read back
  through pydantic. A decision's command names the record by its id
  (`document_id`, `template_id`). `show(command, uow)` reads the record
  and the phone shows it, but nothing of it is bound. The project's
  audit proved both cases: staff confirmed "approve" for a legal
  document's file A, the company uploaded B, and the same authorization
  approved B; a member confirmed "approve" for a template, its owner
  withdrew it, rewrote it and sent it again, and the old authorization
  published the new text. Meanwhile its clients restate what they were
  shown (a file's SHA-256 and number, a submission's id) in the command,
  and the use case refuses a record that is not as stated.
- **A held act keeps its authorization five minutes** (A17). An admin
  change held for a second person's approval (`approval_pending`, a
  guard `by_maker`) is made, once approved, by its maker's same call,
  "with the first authorization while it lives" (ADR 0163). It lives
  `AUTH.AUTHORIZATIONS.TTL`, 5 minutes by default, 30 at most. Approvals
  take hours, so the maker confirms every held change twice.
- **The framework's acts have no methods of their own** (A17).
  `auth.admin_change` and approvals' four acts are declared without
  `methods` or `ttl`, so they take `AUTHORIZATIONS.METHODS` and `TTL`,
  the same as every act of the project. Its default, `otp`, is proved
  by a passkey too. A project whose back office must confirm on the phone
  sets `METHODS = ("phone",)` for every act.
- **A withdrawn ask can only be reported from its push** (A17). Since
  2.5 a session withdraws what it asked the phone (ADR 0164); the phone
  may still report it until `REQUEST_TTL` after it was asked, but the
  list (`GET /app-sign-in/requests`) leaves it out at once. An app opened
  without the push cannot report it.

### What others do

- **OWASP's Transaction Authorization Cheat Sheet** [1] asks that
  transaction data be protected against modification between its entry
  and its authorization, and that any change reset the authorization
  (2.6); that a final gate check, before execution, that the transaction
  was authorized (2.8); and that authorization happen "in a limited time
  window" between the challenge and its completion (2.9). The record a
  decision is about is part of what the user saw.
- **PSD2's dynamic linking** [2] makes the authentication code specific
  to the amount and the payee, and "any change to the amount or the
  payee" invalidates it (Article 5(1)(d)): what was approved is the data,
  not a reference to it.
- **HTTP's conditional requests** [3]: `If-Match` makes a state-changing
  request conditional on the representation the client saw, its entity
  tag, against the "lost update"; domainkit answers it with an
  aggregate's version (`UnitOfWork.expect`). An optimistic version check
  is the same idea inside one transaction.
- **Microsoft Entra's protected actions** [4] attach an authentication
  context, which says which methods are enough, to single permissions:
  one action can require a phishing-resistant method while others take
  any MFA. **Entra PIM** [5] asks for MFA when a role's activation is
  requested, and the approval then activates it without asking again.
- **Microsoft Entra's "Report suspicious activity"** [6] lets the user
  report an unknown prompt from the Authenticator app; the report sets
  the user's risk, and is what turns a stranger's prompt into a signal.

### Options: the record

| Option | What it costs |
| --- | --- |
| The client restates the record in the command (the project's way) | Every act grows fields the client copies back; one forgotten is unbound. The phone shows the screen, not the fields. |
| Bind `shown`: the act's screen built again at the act must match | `show` cuts and counts what it shows (six lines of 64 characters); two records that show alike pass. It is built in the request's language. |
| A version column read by the framework | Needs a convention every record follows; records without one (a file in storage, a row of another service) cannot. |
| **`Action(binds=...)`: a function of the command and the unit of work, read at the twin and at the act; its value's SHA-256 kept on the row, and covered by the digest** | One column (`bound`). A project names what to bind; an aggregate's `version` serves most. |

Where the act reads it: before the service reads (when it opens its unit
of work), in the act's own transaction, so the decision and the check
see one state. Reading it at the commit would read the record after the
act changed it. Reading it before the service, in a unit of work of its
own, leaves a window between the check and the decision.

How the twin's value is kept: the digest, which the HMAC code covers and
the phone signs, is of the command and what was bound; the row keeps the
bound value's SHA-256 separately, so the act tells "another command"
(`403 action_not_authorized`, as before) from "the record changed" (`409
action_record_changed`), which the client answers by asking again.

### Options: the held act

| Option | What it costs |
| --- | --- |
| A longer `TTL` for `auth.admin_change` (above 30 minutes) | Every admin change could be confirmed hours after it was asked, against OWASP 2.9, held or not. |
| Two lifetimes, to confirm and to use | Every confirmed authorization lives the longer one, held or not. |
| **Kept when held: approvals, which holds the act, says so; the row keeps `held_until`, at most `HELD_TTL` past its own expiry, and at most the request's expiry** | One column. The code covers the expiry, not the hold; the hold counts only within `HELD_TTL` of the expiry, so a row changed to hold longer gains nothing past the setting. |

A setting, off by default: keeping an authorization longer changes what
a 2.6 project relies on, so it is a project's choice, as Entra PIM's is
an administrator's.

### Options: each act's methods

| Option | What it costs |
| --- | --- |
| `METHODS` in `AUTH.ADMIN_ACTION` and beside each of approvals' `*_ACTION` (the project's proposal) | A key per act, in two apps, and again for each act the framework adds; `ADMIN_ACTION` changes from a boolean to a mapping. |
| **`AUTH.AUTHORIZATIONS.ACTIONS`: per purpose, `METHODS` and `TTL`, in place of the action's and of the defaults** | One place for every act, the framework's and the project's, as Entra's authentication contexts are per action. A mistyped purpose would hold for nothing: `check` refuses it (`auth.E112`). |

### Options: the withdrawn ask

| Option | What it costs |
| --- | --- |
| List it always, marked | An app of 2.6 shows it as waiting, and offers to approve what then answers `404`. |
| **List it with `?withdrawn=true`, marked `"withdrawn": true`** | An app that knows the mark asks for it; one that does not lists as before. |

## Decision

### `Action(binds=...)`

- `binds(command, uow)`, a coroutine or a plain function, reads the
  record the act decides on and answers a value pydantic writes as JSON.
  It is lent the unit of work as `show` is, which refuses a commit.
- **Asked**: `request_action_authorization` reads it first, then `show`,
  in one unit of work the act's factory makes (a `ResourceRouter`'s twin
  gives its route's). The row keeps the SHA-256 of the value as
  canonical JSON (`bound`); the digest is the SHA-256 of `{"action",
  "arguments", "binds"}`: the phone signs it, the events and the evidence
  carry it, the code covers it.
- **At the act**: `requires_authorization` checks the command against the
  digest with the stored `bound`, as before (`403`, voided), then reads
  `binds` when the service opens its unit of work (`async with uow`), or
  commits without opening it. Another value: the unit of work is left,
  the authorization voided, the trail told `record_changed`, and the act
  refused `409 action_record_changed` (`ActionRecordChanged`).
  `spend_authorization(uow, ...)` reads it in `uow`, open, first.
- `ImproperlyConfigured`: `binds` not callable, a value JSON cannot
  hold, a `binds` that commits, asking without the act's unit of work,
  a service that takes none or never opens it.

### Held for approval

- `AUTH.AUTHORIZATIONS.HELD_TTL`, a `timedelta` of at most 7 days, None
  by default.
- `hold_authorizations(until)` (`auth.actions`): the request's
  authorizations that an act took (`requires_authorization`,
  `check_authorization`) and did not spend are kept until `until`, at
  most `HELD_TTL` past their own expiry, written in a transaction of
  their own as the response starts. Approvals calls it where a guard
  `by_maker` refuses with `ApprovalPending`, with the request's expiry.
- The row's `held_until` keeps it. An authorization works before its
  expiry, or before `held_until` and within `HELD_TTL` of its expiry.
  The purge keeps it while it is held.

### Each act its own methods and lifetime

- `AUTH.AUTHORIZATIONS.ACTIONS`: per purpose, `TTL` (30 minutes at
  most) and `METHODS` (holding `otp` or `phone`), each optional, in place
  of the `Action`'s `ttl` and `methods` and of `TTL` and `METHODS`.
- `auth.E112`: a purpose there that no registered action has.

### The phone's list

- `GET /app-sign-in/requests?withdrawn=true` lists, among those waiting,
  the pushes asked within `REQUEST_TTL` that ended unanswered, each
  `"withdrawn": true`: what a session withdrew. Approving it and a plain
  deny stay `404`; `deny` with `report` takes it, as since 2.5.

## Consequences

- **An act that binds refuses a record changed after the screen.** The
  project drops its restated fields and its `409 document_changed`; the
  client asks again on `409 action_record_changed`. An authorization
  asked before a deploy that adds `binds` has no bound value, and is
  refused once.
- **A record the act does not write** is read for update in `binds`, so
  that no writer changes it between the check and the commit; one the
  act writes is held to its version as it saves. The docs say so.
- **Two nullable columns** on `domainkit_authorizations`, `bound` and
  `held_until`: `makemigrations`, then `migrate` (the upgrading notes).
  Rows of 2.6 bind no record and are held by nothing.
- **A held change is made without a second confirmation** where a
  project sets `HELD_TTL`; its maker's login must still be the one that
  asked, and the change the one confirmed.
- **Each act asks what its entry names**; a project that asks the phone
  of the back office alone sets two or five entries, not the default.
- **New names**: `Action(binds=...)`, `Action.read`, `Action.bound_in`,
  `ActionRecordChanged`, `hold_authorizations`; settings `HELD_TTL` and
  `ACTIONS`; the check `auth.E112`; the problem code
  `action_record_changed`; the list's `withdrawn` parameter and field.
  Nothing is renamed or removed: a minor release (2.7).

## References

1. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
2. Commission Delegated Regulation (EU) 2018/389, Article 5, *Dynamic
   linking*: <https://eur-lex.europa.eu/eli/reg_del/2018/389>
3. RFC 9110, *HTTP Semantics*, 13.1.1 `If-Match`:
   <https://www.rfc-editor.org/rfc/rfc9110#section-13.1.1>
4. Microsoft Entra ID, *What are protected actions*:
   <https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/protected-actions-overview>
5. Microsoft Entra ID, *Configure Microsoft Entra role settings in
   Privileged Identity Management*:
   <https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-how-to-change-default-settings>
6. Microsoft Entra ID, *Configure Microsoft Entra multifactor
   authentication settings*, Report suspicious activity:
   <https://learn.microsoft.com/en-us/entra/identity/authentication/howto-mfa-mfasettings#report-suspicious-activity>

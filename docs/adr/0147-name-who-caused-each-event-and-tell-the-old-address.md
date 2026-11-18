# 0147. Name who caused each event, and tell the old address

- Status: Accepted; amended 2026-11-18 (at the end)
- Date: 2026-11-16

## Context

A project that runs an identity service on the framework reported (user
report A11, low): "Events carry no actor, and `UserUpdated` carries
field names only: a handler cannot tell a change staff made from the
user's own, nor notify the old address."

### What exists

- **The request's actor.** `RequestContext.actor` is a string: the
  user's id once `AuthenticationMiddleware` has run; in an impersonation,
  the staff member's id; `operator:<name>` in a command an operator runs
  ([ADR 0070](0070-name-the-operator-of-data-changing-commands.md));
  `system` in work no request queued. The outbox carries it to the
  worker in each message's headers, and the worker runs the message as
  it. Rate limits, idempotency keys, logs, the audit trail's `actor`
  column, and evidence records read it.
- **Events** carry `event_id` and `occurred_at`, and the fields their
  class declares. An in-process handler runs inside the request after the
  commit, and a durable one runs in the worker as the request's actor,
  so `current_request().actor` answers there. But nothing on the event
  says it, the impersonated user is gone (the context holds only the
  staff member's id), and an event handled anywhere else (a test, a
  replay, another bus) has no request to ask.
- **`UserUpdated`** names the fields that changed, never their values,
  which may be personal data. `AbstractUser.update` marks a new address
  or number unverified. After the commit the old value is gone, so no
  handler can write to it.

### What keeps events, and what protects personal data there

| Store | What it keeps of an event | What protects personal data |
| --- | --- | --- |
| Outbox (`messaging`) | Every field (`dump_event`), once per durable handler, until the message is done; the request's actor in the headers | Done messages are blanked; `privacy erase` blanks dead ones about a subject, found by the `Personal` markers |
| Audit trail (`audit`) | Every field as `details`, for good | A `Personal` field is a pseudonym under the subject's key, which `audit forget` destroys; an unmarked field stays in clear |
| Webhook deliveries | Every field by default, or `payload=`'s choice | Deliveries are tagged with the subjects; erasure blanks the bodies |
| Live events | Sent to clients, not kept | `Personal` fields are left out |
| Evidence ledger | Never an event; its own records capture the request's actor | Facts are the project's choice |

`Encrypted` seals fields of aggregates in their rows
([ADR 0120](0120-seal-encrypted-fields-in-the-orm-bound-to-their-row-and-shred-a-person-by-destroying-their-key.md)).
A value copied into an event is in clear there: in the stores above,
what protects it is its `Personal` marker
([ADR 0041](0041-privacy-through-personal-markers.md)).

### What others do

- **Event stores keep who and why next to each event, apart from its
  data.** KurrentDB (EventStoreDB) reserves the metadata keys
  `$correlationId` and `$causationId` [1]. Marten stores a correlation
  id, a causation id, and the user's name (`LastModifiedBy`) with each
  event; the first two come from the active trace, and the user's name is
  set on the session [2].
- **CloudEvents' Auth Context extension** describes the principal that
  caused an event: `authtype` (`app_user`, `user`, `service_account`,
  `api_key`, `system`, `unauthenticated`, `unknown`), `authid`, and
  `authclaims`. It discourages personal data in `authid` [3].
- **OAuth 2.0 Token Exchange** (RFC 8693) names the acting party in the
  `act` claim when one party acts for the subject. It calls that
  delegation: the actor keeps an identity of its own, as staff do in
  domainkit's impersonation [4].
- **OWASP's Authentication Cheat Sheet** sends the current address a
  notice of an email change (with MFA) or a confirmation (without) [5].
  The Forgot Password Cheat Sheet warns against relying on recovery
  details changed recently [6]. **ASVS 5.0 V6.3.7** (level 3): users are
  notified after updates to authentication details, such as a change of
  username or email address [7].
- **django-allauth** sends `email_changed(request, user,
  from_email_address, to_email_address)`, a signal of its own, apart from
  the user's other changes [8].

### Options for the actor

| Option | What it costs |
| --- | --- |
| Leave it to `current_request()` | Implicit; no impersonated user; nothing for events handled outside a request or a job. |
| Each call site passes the actor | Every `record(...)` of every domain; domain code would need the request; a forgotten one is silent. |
| The unit of work stamps it at commit | Events are frozen; the unit of work is in the domain layer, which reads no request; events published without one miss it. |
| Metadata outside the dataclass fields (an attribute, or an envelope) | `fields()` stays as it is, but `dataclasses.replace` drops it, the outbox must carry it in headers, a domain cannot pass one, and an `InitVar` would break every subclass's `__post_init__`. |
| **A field of `DomainEvent`, `actor` (an `Actor`, or None), defaulted from the context where the event is made** | Typed and explicit; it travels in the outbox payload and survives `replace`. `fields()` and `dump_event` list one more key, so the stores that send events out leave it out. |

### Options for the old address

| Option | What it costs |
| --- | --- |
| Old values on `UserUpdated` | The old value of every field, names and a project's own fields included, in every durable `UserUpdated` handler's message and every exposed webhook; the audit trail pseudonymizes whole fields only, so a mapping would be one opaque value. |
| Old and new values on an event of its own (allauth) | The new value is on the user already: twice the personal data, kept in the outbox and the trail. |
| **An event per kind of address, with the old one only, marked `Personal`, and whether the user had proved it** | Only handlers of that event hold the old value; the existing protections apply by the marker; `UserUpdated` and its handlers are unchanged. |
| domainkit sends the notice (as `DEVICES.BOUND_NOTICE` does) | Templates, translations, a setting, and which channel to use: a project's choice. It can come later, as a handler of these events. |

## Decision

1. **`DomainEvent.actor: Actor | None`**, defaulted when the event is
   made, as `occurred_at` is. `Actor` (`domainkit.ddd`) is a frozen
   dataclass: `id`, the request's actor as logs and the audit trail write
   it, and `impersonating`, the id of the user staff act as.
   `is_self(subject)` holds when the subject acted in their own name;
   `is_system` when no one asked for the work. `None` means an anonymous
   caller, or code outside requests, jobs, and commands. A domain may pass
   `actor=` itself. It is not CloudEvents' `authtype`: the form of `id`
   already tells an operator and the system apart, and the core knows no
   staff. A handler tells staff from the user by comparing the actor with
   the subject.
2. **The framework fills it in where it knows.** `RequestContext` gains
   `impersonating`. `AuthenticationMiddleware` sets it for an
   impersonation token; the outbox carries it to the worker in an
   `impersonating` header, only when set; the worker binds it; a login
   (`acting_as`) and an approved command run as its maker clear it.
   Commands run as `operator:<name>`, periodic work as `system`.
3. **The domain layer reads no request.** `domainkit.ddd` imports
   nothing above it. `domainkit.context` gives its `current_actor` to
   `domainkit.ddd.events` when it is imported, which every request, job,
   and command does first. Nothing is added to a request: the actor is
   read once per event made.
4. **What left before is unchanged.** A webhook's default `data`, a live
   event's payload, and the audit trail's `details` leave
   `DomainEvent.actor` out: a receiver or a browser is not told which
   staff member acted, and the audit row has its own `actor` column. A
   project's event that declares an `actor` of its own keeps that field
   in all three: the check is that the class's field is
   `DomainEvent.actor`'s, the same dataclass field.
5. **`EmailChanged` and `MobileNumberChanged`**
   (`domainkit.contrib.auth.integration`), recorded by
   `AbstractUser.update` after `UserUpdated`: `user_id`, `previous`
   (marked `Personal`, `None` when there was none), and
   `previous_verified`. They are recorded only for a change that is kept:
   not for a change refused with `IdentifierRequired`, nor for privacy's
   erasure, which must not copy the data again, nor for a signup that
   takes an address its holder never proved (`EmailReleased`,
   [ADR 0133](0133-a-signup-takes-an-address-its-holder-never-proved.md)).
   `UserUpdated` stays names only.

Correlation and causation ids are left out: the request id already goes
with each message, log line, and audit row.

## Consequences

- A handler can tell the user's own change from staff's, an operator's,
  or the system's, from the event alone, in the worker too, and can write
  to the old address or number.
- `dataclasses.fields(event)`, `asdict`, and `dump_event` list `actor`.
  Code that lists an event's own fields, such as a test helper that drops
  `event_id` and `occurred_at`, drops `actor` too (`docs/upgrading.md`).
  A project's event with an `actor` field of its own keeps working, and
  the framework leaves that field alone, but mypy reports the override.
- The outbox payload holds the actor, which its headers held already.
  An operator's name is a login name: the audit trail's `actor` column
  and the outbox headers held it before.
- The old address is in the outbox until the handler's message has run,
  and a pseudonym in the audit trail. Live fan-out between processes
  carries every field, the old address included, as it carries the other
  `Personal` fields, and keeps none.
- Not done here: the directory login (`identity.ldap`) sets a changed
  email without `update`, so it records neither `UserUpdated` nor
  `EmailChanged`; `PhoneRemoved` (`phones`) names the number by its id,
  and the number is deleted. Each would follow this decision if changed.

## References

1. KurrentDB, *Event streams*, reserved metadata keys:
   <https://docs.kurrent.io/server/v25.0/features/streams>
2. Marten, *Event Metadata*: <https://martendb.io/events/metadata>
3. CloudEvents, *Auth Context* extension:
   <https://github.com/cloudevents/spec/blob/main/cloudevents/extensions/authcontext.md>
4. RFC 8693, *OAuth 2.0 Token Exchange*, section 4.1, the `act` claim:
   <https://www.rfc-editor.org/rfc/rfc8693#section-4.1>
5. OWASP, *Authentication Cheat Sheet*, changing a user's registered
   email address:
   <https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html>
6. OWASP, *Forgot Password Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html>
7. OWASP ASVS 5.0, V6 Authentication, requirement 6.3.7:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x15-V6-Authentication.md>
8. django-allauth, *Signals*, `email_changed`:
   <https://docs.allauth.org/en/latest/account/signals.html>

## Amendment (2026-11-18): what staff acting as a user are answered is kept for that user

The 2.8 audit found per-user state kept by the request's `actor`
alone, the staff member under an impersonation (A28-SEC-03). The actor
stays as this record decided: the trail, events and approvals name the
staff member, with `impersonating` beside it.

- `caller_key(actor, impersonating)`, beside `current_request`, names
  whom a request's answers are kept for: the actor, or `<staff id>:as:<user id>`
  under an impersonation. Idempotency keys and pages cached with
  `vary_on_user=True` are kept by it, apart from the staff member's own,
  the user's, and those of each other user the staff member acts as.
- A response kept for an `Idempotency-Key` under an impersonation names
  the user among its subjects: erasing the user drops it, and the user's
  export lists it, withheld.
- Rate limits, throttles, live connections, and outbound lookups count
  what a caller uses, and keep counting the actor: one person, one
  budget, whomever they act as.

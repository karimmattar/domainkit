# 0029. Send mail, text messages, and push notifications through the outbox

- Status: Accepted; amended 2026-10-25 (at the end)
- Date: 2026-10-19

## Context

Password resets, email verification, and most domains need to send
messages to people: email above all, sometimes a text message or a push
notification. Django's `send_mail` talks to the SMTP server inside the
request. That couples the request to the server's speed and availability,
and it gets transactions wrong both ways: a message can go out for a change
that then rolls back, and a change can commit while its message is lost.

domainkit already has a transactional outbox (ADR 0005) with registered
tasks, per-task `max_attempts`, queues, and a background publisher. The
options were: (a) send inside the request, as Django does; (b) send after
the commit, in the request's process (`on_commit`); (c) store a task in the
outbox in the transaction of the change, and let the worker send it.

## Decision

Messages to people are tasks in the outbox (option c).

- `domainkit.contrib.mail` and `domainkit.contrib.notifications` register
  one task each per channel (`domainkit.mail.send`,
  `domainkit.notifications.send_sms`, `domainkit.notifications.send_push`)
  when they are ready, with `MAX_ATTEMPTS` and `QUEUE` from their settings.
  They require `domainkit.contrib.messaging`.
- `send(message, uow)` defers the task in the unit of work, so the message
  is stored if and only if the change commits; without a unit of work it is
  stored at once, in a transaction of its own.
- Every message gets its id (`Message-ID`, `message_id`) when it is queued,
  so the retries that at-least-once delivery brings send the same message,
  which receivers and providers can drop.
- Backends are ports. Mail ships SMTP (stdlib `smtplib` on a worker
  thread, TLS required outside `DEBUG`), console, and memory backends; text
  messages and push notifications ship console and memory only, and
  projects write the adapter for their provider. Staging and production
  refuse the development backends.
- Work that already runs in the worker, such as the task that makes a
  password reset link, sends with `deliver`, skipping the outbox, so
  secrets the message holds are never stored there.
- Signing up queues only the id of a pending signup. The username and
  the address wait in `domainkit_pending_signups`, a table of their own
  that is never in fixtures and is purged once the link expires, and the
  account is made only when the link the worker mails is used, with the
  password chosen then. (A first version made the account in the worker
  at once, from a payload that held the hash; logging in with the
  username chosen then told anyone whether the address already had an
  account. A second took the password when asking: an owner who used the
  link made an account whose password the asker had chosen.)
- Messages are validated when they are built: no header injection, bounded
  recipients and sizes; their `repr` shows only their id, and delivery
  errors name codes, never addresses.

## Consequences

- A request never waits for a mail server, and a server that is down delays
  messages instead of failing requests or losing messages.
- A message is never sent for a change that rolled back, and never lost for
  one that committed; it may be sent twice when a worker dies after sending
  and before recording it.
- Projects that send mail must run a worker, and install messaging.
- What a queued message holds stays in the outbox table until
  `RETENTION_SECONDS` after it is sent (dead letters until retried): queue
  ids and let a task build sensitive messages in the worker, as auth does.
- An account asked for at `/signup` exists only once its address is
  confirmed, so whether an address has an account is told to whoever
  reads its mail and to no one else; a pending signup reserves no name,
  and the first link used for a name gets it. Whoever reads the mail
  chooses the password, so asking with another's address gains nothing;
  the page behind the link must ask for the password, an extra step for
  people who sign up.
- Templates are files rendered by a small substitution helper (values only,
  escaped in HTML), not a template engine: there is no new dependency, and
  no logic in templates to review.

## Amendment (2026-10-25)

What a queued message holds no longer stays in the outbox after it is
sent (audit 0.9.0 DATA-01). The worker blanks every message's arguments
(`payload = {}`) in the statement that marks it done, and `cancel` in the
one that cancels it; the row keeps what operators need (target, ids,
times, attempts, error) for `RETENTION_SECONDS`. Tasks registered with
`sensitive=True`, which the mail and notifications tasks are, have their
arguments blanked when they die too, after the error is recorded, with a
`payload_blanked` header: `messages --retry` refuses them, and
`--retry all` leaves them and says how many. A dead message of another
task keeps its arguments, so it can still be retried. A queued message
is still readable in the table until it is sent: queuing ids and building
links and codes in the worker (`deliver`) stays the way for secrets.


## Amendment (2026-10-25): chains

A chain's next step is handed the arguments of the step before, or what
it returned (`next_arguments`), so a sensitive step's code or address
reached the next step's row and stayed there when that step died
(SEC-r2-5). The worker now marks each step written after a sensitive one
with a `sensitive` header, carried down the rest of the chain, and blanks
such a step's arguments when it dies, as it blanks the sensitive task's
own. A chain with no sensitive step keeps a dead step's arguments, to be
retried, as before.

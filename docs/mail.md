# Mail, text messages, and push notifications

`domainkit.contrib.mail` is Django's `django.core.mail`, and
`domainkit.contrib.notifications` does the same for text messages (SMS) and
push notifications. Neither sends anything from a request: they queue a
task in the outbox of `domainkit.contrib.messaging`, in the transaction of
the change that causes the message, and the worker (`runworker`) sends it.
A message goes out if, and only if, the change commits, and a failure to
send is retried without failing the request. The design is in
[ADR 0029](adr/0029-messages-to-people-leave-through-the-outbox.md).

## Mail

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.mail",
    # your domains
]
MAIL = {
    "DEFAULT_FROM_EMAIL": "Billing <billing@example.com>",
    "SMTP": {
        "HOST": "smtp.example.com",
        "USER": env.str("SMTP_USER"),
        "PASSWORD": env.str("SMTP_PASSWORD"),
    },
}
```

```python
from domainkit.contrib.mail import EmailMessage, send_mail

async with uow:
    invoice = ...
    await send_mail(
        "Your invoice",
        f"Invoice {invoice.number} is attached to your account.",
        [customer.email],
        html_message="<p>...</p>",  # optional
        uow=uow,  # sent only if this commits
    )
    await uow.commit()

message = EmailMessage(
    subject="Welcome",
    body="Hello.",
    to=("Alice <alice@example.com>",),
    cc=(...),
    bcc=(...),
    reply_to=(...),
    headers={"List-Unsubscribe": "<https://example.com/u/1>"},
)
await message.send(uow)  # or send() alone: its own transaction
```

Without `uow`, the task is stored at once in a transaction of its own.
`send` and `send_mail` return the queued message, with its sender
(`DEFAULT_FROM_EMAIL` unless it named one) and a `Message-ID`. Delivery is
at least once: a worker that dies after sending sends it again, with the
same `Message-ID`, so mail clients can drop the copy. In background work
that already runs in the worker, `await deliver([message])` sends at once,
without storing the message in the outbox.

**What the outbox keeps.** A queued message is stored whole, in clear, in
the outbox row's `payload` (`domainkit_messages`): its recipients, subject,
and bodies, with any reset link or code in them. It stays there only until
it is sent: the worker blanks the payload in the statement that marks the
message done, and a cancel does the same. The row itself (the task's
name, its id, times, attempts, and error) is kept `MESSAGING.RETENTION_SECONDS`
(7 days), then deleted. A message that fails `MAX_ATTEMPTS` times is
dead-lettered with its error and its payload blanked too, so it cannot be
retried: send it again from where it came from. Until it is sent, anyone
who can read the table, a replica, or a backup can read it; for a link or
a code, queue the user's id with a task of your own and build the message
in the worker with `deliver`, as the auth flows do.

A message refuses what could add a header or a recipient (header
injection): line breaks and control characters in the subject, addresses,
and extra headers, and extra headers that would replace `From`, `To`,
`Subject`, or the others the message sets itself. An address is
`user@example.com` or `Name <user@example.com>`, with an ASCII address (an
internationalized domain in its `xn--` form); a list in one string is
refused. A message goes to 1 to 100 addresses (`to`, `cc`, and `bcc`
together), with a subject of 998 characters at most, bodies of a million,
and 20 extra headers. Its `repr` shows only its `Message-ID`.

### Backends

| `MAIL.BACKEND` | Sends to |
| --- | --- |
| `domainkit.contrib.mail.backends.SmtpBackend` (default) | The SMTP server in `MAIL.SMTP`, with Python's `smtplib` on a worker thread. |
| `domainkit.contrib.mail.backends.ConsoleBackend` | Standard output, for development. |
| `domainkit.contrib.mail.backends.MemoryBackend` | `domainkit.contrib.mail.outbox`, a list, for tests. |

The SMTP connection is encrypted, `STARTTLS` on port 587 by default or
TLS from the first byte on 465, and the server's certificate is checked
against the system's CAs or `TLS_CA`. A server that offers no `STARTTLS`
gets nothing. Plain text (`SECURITY = "none"`) is refused unless `DEBUG`
is on, for a local mail catcher. Each exchange with the server is bounded
by `TIMEOUT`. A failure raises `MailDeliveryFailed`, which names the error
and the server's code but never an address, and the worker tries again
until `MAX_ATTEMPTS`. Recipients the server refuses while it takes the
message for others are logged as a count.

Staging and production refuse the console and memory backends: the console
would print reset links into the logs. To send through a provider's API,
subclass `BaseEmailBackend` and implement `async send_messages(messages)`.

In tests:

```python
# config/settings/test.py
MAIL = {"BACKEND": "domainkit.contrib.mail.backends.MemoryBackend"}
```

```python
from domainkit.contrib import mail

await Worker().run_once()
(message,) = mail.outbox
assert message.subject == "Welcome"
mail.outbox.clear()
```

### Templates

A template is up to three files under `templates/mail/` in a domain
package, named after it:

```text
billing/templates/mail/billing/invoice.subject.txt    one line
billing/templates/mail/billing/invoice.txt            the plain-text body
billing/templates/mail/billing/invoice.html           the HTML body (optional)
```

```text
Dear {{ customer.name }},
Invoice {{ invoice.number }} for {{ invoice.total }} is due.
```

```python
from domainkit.contrib.mail import render_mail

message = render_mail(
    "billing/invoice",
    {"customer": customer, "invoice": invoice},
    to=[customer.email],
)
await message.send(uow)
```

`{{ name }}` and `{{ a.b }}` take keys of mappings and attributes of
objects, never names that start with `_` nor methods. In HTML, values are
escaped (`<` becomes `&lt;`); in text they are left as they are, and the
subject is joined onto one line. A name the context lacks raises
`TemplateError`, so no message goes out with a hole in it. There are no
loops or conditions: compute such text in Python and pass it in. Template
names are lower-case words separated by `/`.

`MAIL.TEMPLATE_DIRS` are searched first, then the installed domains in
`INSTALLED_DOMAINS` order; the first that has the text body wins. List a
directory there to replace a template a contrib app ships, such as
`auth/password_reset` ([auth.md](auth/logging-in.md#password-reset)).

### Settings

| `MAIL` key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `...SmtpBackend` | Dotted path to a `BaseEmailBackend`. Checked at startup. |
| `DEFAULT_FROM_EMAIL` | `"webmaster@localhost"` | The sender of messages that name none. |
| `SMTP` | see below | The server `SmtpBackend` uses. |
| `MAX_ATTEMPTS` | `5` | Tries of each message before it is dead-lettered (`messages --dead`). |
| `QUEUE` | `None` | The worker queue that sends mail (`runworker --queue`); `default` when `None`. |
| `TEMPLATE_DIRS` | `()` | Directories searched for templates before the domains. |

| `MAIL.SMTP` | Default | Meaning |
| --- | --- | --- |
| `HOST` | `"localhost"` | The server's name or address: not empty, and at most 253 characters. |
| `PORT` | `None` | 1 to 65,535; `None` is 587 for `starttls`, 465 for `tls`, 25 for `none`. |
| `SECURITY` | `"starttls"` | `starttls`, `tls`, or `none` (only with `DEBUG`). |
| `USER`, `PASSWORD` | `None` | Set together; the password is hidden in reprs. |
| `TIMEOUT` | `10.0` | Seconds to connect, and for each exchange. |
| `TLS_CA` | `None` | CA bundle that signed the server's certificate. |

## Text messages and push notifications

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.notifications",
    ...,
]
NOTIFICATIONS = {
    "SMS_BACKEND": "accounts.infrastructure.sms.GatewaySmsBackend",
    "PUSH_BACKEND": "accounts.infrastructure.push.FcmPushBackend",
}
```

```python
from domainkit.contrib.notifications import send_push, send_sms

await send_sms("+33612345678", "Your code is 123456", uow=uow)
await send_push(
    device.token,
    "Shipped",
    "Your order is on its way",
    data={"order": str(order.id)},
    uow=uow,
)
```

`SmsBackend` and `PushBackend` are ports: no provider ships with domainkit.
An adapter implements `async send_messages(messages)`, returns how many it
sent, and raises `NotificationFailed` (with the provider's code, never a
number or a token) for the worker to try again. Every message carries a
`message_id`, the same on every try, to pass on to providers that drop
duplicates.

| Message | Fields and limits |
| --- | --- |
| `SmsMessage` | `to`: E.164 (`+` and up to 15 digits); `body`: 1 to 1600 characters; `sender`: an alphanumeric id of 11 characters at most, or a number. |
| `PushMessage` | `token`: one line, up to 4096 characters; `title`: one line, 255; `body`: 4096; `data`: up to 50 string pairs of 1024 characters each. |

Their reprs show only the `message_id`. Queued messages are kept in the
outbox as mail is, until they are sent, cancelled, or dead-lettered, then
blanked (see [what the outbox keeps](#mail)). `deliver_sms` and
`deliver_push` send at once, from the worker. The console backends (the defaults) print
what they would send; the memory backends keep it in `sms_outbox` and
`push_outbox`, for tests. Staging and production refuse both.

| `NOTIFICATIONS` key | Default | Meaning |
| --- | --- | --- |
| `SMS_BACKEND` | `...ConsoleSmsBackend` | Dotted path to an `SmsBackend`. |
| `PUSH_BACKEND` | `...ConsolePushBackend` | Dotted path to a `PushBackend`. |
| `MAX_ATTEMPTS` | `5` | Tries of each message before it is dead-lettered. |
| `QUEUE` | `None` | The worker queue that sends them; `default` when `None`. |

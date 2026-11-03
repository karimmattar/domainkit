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
| `domainkit.contrib.mail.providers.SesBackend` | Amazon SES's v2 API: the MIME message, signed with SigV4. |
| `domainkit.contrib.mail.providers.SendGridBackend` | SendGrid's v3 Mail Send API. |
| `domainkit.contrib.mail.providers.MailgunBackend` | Mailgun's Messages API. |
| `domainkit.contrib.mail.providers.PostmarkBackend` | Postmark's Email API. |

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
would print reset links into the logs.

The provider backends call their HTTP API through
`domainkit.http.client` (the `httpclient` extra), with no SDK, and read
their settings from `MAIL.OPTIONS`; wrong ones stop the application at
startup. A refusal raises `MailDeliveryFailed` with the provider's error
code, never an address.

```python
MAIL = {
    "BACKEND": "domainkit.contrib.mail.providers.PostmarkBackend",
    "OPTIONS": {"SERVER_TOKEN": env.str("POSTMARK_TOKEN")},
    "DEFAULT_FROM_EMAIL": "Acme <hello@acme.example>",
}
```

| Backend | `MAIL.OPTIONS` |
| --- | --- |
| `SesBackend` | `REGION`; `ACCESS_KEY_ID` and `SECRET_ACCESS_KEY`, or neither for the environment's credentials (variables, a web identity, a container or instance role); `CONFIGURATION_SET`; `URL`. |
| `SendGridBackend` | `API_KEY`; `URL` (`https://api.eu.sendgrid.com` for EU residency). |
| `MailgunBackend` | `API_KEY`, `DOMAIN`; `URL` (`https://api.eu.mailgun.net` for the EU region). |
| `PostmarkBackend` | `SERVER_TOKEN`; `MESSAGE_STREAM` (`outbound`). |

Every backend also takes `TIMEOUT` (10 seconds, at most 60). SES is sent
the message as SMTP is (`to_mime`); the others its parts: sender,
recipients (Bcc never in a header), subject, text, HTML, `Reply-To`,
headers, and the `Message-ID`. To use another provider, subclass
`BaseEmailBackend` and implement `async send_messages(messages)`.

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
| `BACKEND` | `...SmtpBackend` | Dotted path to a `BaseEmailBackend`, or the name an installed package offers it by ([extension points](reference/extension-points.md#backends-from-an-installed-package)). Checked at startup. |
| `DEFAULT_FROM_EMAIL` | `"webmaster@localhost"` | The sender of messages that name none. |
| `SMTP` | see below | The server `SmtpBackend` uses. |
| `OPTIONS` | `{}` | A provider backend's settings, which it reads (above). |
| `MAX_ATTEMPTS` | `5` | Tries of each message before it is dead-lettered (`messages list dead`). |
| `QUEUE` | `None` | The worker queue that sends mail (`runworker --queue`); `default` when `None`. |
| `TEMPLATE_DIRS` | `()` | Directories searched for templates before the domains. |
| `ADMINS` | `()` | Who hears of unexpected errors (Django's `ADMINS`): addresses `mail_admins` and `report_to_admins` write to. |
| `ADMINS_EVERY` | 5 minutes | At most one error report per kind and place in this long (at most 1 day), from each process. |

| `MAIL.SMTP` | Default | Meaning |
| --- | --- | --- |
| `HOST` | `"localhost"` | The server's name or address: not empty, and at most 253 characters. |
| `PORT` | `None` | 1 to 65,535; `None` is 587 for `starttls`, 465 for `tls`, 25 for `none`. |
| `SECURITY` | `"starttls"` | `starttls`, `tls`, or `none` (only with `DEBUG`). |
| `USER`, `PASSWORD` | `None` | Set together; the password is hidden in reprs. |
| `TIMEOUT` | `10.0` | Seconds to connect, and for each exchange. |
| `TLS_CA` | `None` | CA bundle that signed the server's certificate. |

### Telling the admins

`MAIL["ADMINS"]` names who hears of trouble (Django's `ADMINS`).
`await mail_admins(subject, body, uow=uow)` writes to them through the
outbox, as any mail; with no admin named, it sends nothing.

To mail them each unexpected error (Django's error emails), add the
reporter to `ERROR_REPORTERS`, beside a tracker or instead of one:

```python
MAIL = {..., "ADMINS": ("ops@example.com",)}
ERROR_REPORTERS = ("domainkit.contrib.mail.admins.report_to_admins",)
```

A report names the error's type, where it was raised, the frames of its
traceback, and the request's context (its id, path, and method), so the
logs can be found. It never holds the error's message nor any value of
the frames, which may hold what a user sent. Each process mails at most
one report per kind and place in `ADMINS_EVERY`, so a failing dependency
does not flood the inbox; the report goes straight through the backend,
from the reporting thread.

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

`SmsBackend` and `PushBackend` are ports, with adapters for these
providers, which call their HTTP APIs through `domainkit.http.client`
with no SDK:

| Backend | Provider | Options (`SMS_OPTIONS`, or `PUSH_OPTIONS[name]`) |
| --- | --- | --- |
| `...sms.TwilioSmsBackend` | Twilio | `ACCOUNT_SID`, `AUTH_TOKEN` (or an API key's secret with `API_KEY_SID`), `FROM` or `MESSAGING_SERVICE_SID` |
| `...sms.VonageSmsBackend` | Vonage | `API_KEY`, `API_SECRET`, `FROM` |
| `...sms.SnsSmsBackend` | Amazon SNS | `REGION`, keys or the environment's, `SENDER_ID`, `SMS_TYPE` (`Transactional`) |
| `...sms.UnifonicSmsBackend` | Unifonic | `APP_SID`, `SENDER_ID` |
| `...sms.HuaweiSmsBackend` | Huawei Cloud's Message & SMS | `URL` (the region's), `APP_KEY`, `APP_SECRET`, `SENDER`, `TEMPLATE_ID` (a template whose one variable is the text), `SIGNATURE` |
| `...push.FcmPushBackend` (`fcm`) | Firebase Cloud Messaging, HTTP v1 | `PROJECT_ID`, `CREDENTIALS_FILE` (a service account's key; none: the metadata server's account) |
| `...push.ApnsPushBackend` (`apns`) | Apple's APNs, over HTTP/2 (the `push` extra) | `TEAM_ID`, `KEY_ID`, `PRIVATE_KEY` (the `.p8`), `TOPIC` (the bundle id), `SANDBOX` |
| `...push.HuaweiPushBackend` (`huawei`) | Huawei Push Kit | `APP_ID`, `APP_SECRET` |

```python
NOTIFICATIONS = {
    "SMS_BACKEND": "domainkit.contrib.notifications.sms.UnifonicSmsBackend",
    "SMS_OPTIONS": {"APP_SID": env.str("UNIFONIC_APP_SID"), "SENDER_ID": "Acme"},
    "PUSH_BACKEND": "domainkit.contrib.notifications.push.FcmPushBackend",
    "PUSH_BACKENDS": {
        "huawei": "domainkit.contrib.notifications.push.HuaweiPushBackend"
    },
    "PUSH_OPTIONS": {
        "fcm": {"PROJECT_ID": "acme-shop"},
        "huawei": {"APP_ID": "10123456", "APP_SECRET": env.str("HUAWEI_PUSH_SECRET")},
    },
}
```

A notification names its `platform` (`fcm`, `apns`, `huawei`), which
picks its backend in `PUSH_BACKENDS`; others go to `PUSH_BACKEND`.
Provider tokens are kept: Google's and Huawei's until five minutes
before they expire, Apple's provider token 50 minutes. A token the
service no longer knows (FCM's `UNREGISTERED`, APNs' `410`, Push Kit's
`80300007`) raises `PushTokenGone`: the worker drops the notification,
without trying again, and publishes `PushTokenExpired` with the token's
digest, which `domainkit.contrib.devices` uses to forget it ([push
tokens](auth/devices.md#push-tokens)).

Another provider's adapter implements `async send_messages(messages)`,
returns how many it sent, and raises `NotificationFailed` (with the
provider's code, never a number or a token) for the worker to try again.
Every message carries a `message_id`, the same on every try, to pass on
to providers that drop duplicates.

| Message | Fields and limits |
| --- | --- |
| `SmsMessage` | `to`: E.164 (`+` and up to 15 digits); `body`: 1 to 1600 characters; `sender`: an alphanumeric id of 11 characters at most, or a number. |
| `PushMessage` | `token`: one line, up to 4096 characters; `title`: one line, 255; `body`: 4096; `data`: up to 50 string pairs of 1024 characters each; `platform`: its push service, or `None`. |

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
| `PUSH_BACKENDS` | `{}` | A backend per platform, for notifications that name theirs. |
| `SMS_OPTIONS` | `{}` | The SMS provider's settings, which its backend reads. |
| `PUSH_OPTIONS` | `{}` | Each push provider's settings, by its name (`fcm`, `apns`, `huawei`). |
| `MAX_ATTEMPTS` | `5` | Tries of each message before it is dead-lettered. |
| `QUEUE` | `None` | The worker queue that sends them; `default` when `None`. |

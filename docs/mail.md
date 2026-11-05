# Mail, text messages, and push notifications

A service tells people things: an invoice is ready, a code to log in, an
order has shipped. `domainkit.contrib.mail` sends email, and
`domainkit.contrib.notifications` sends text messages (SMS) and push
notifications. Neither sends anything from a request. They queue a task
in the outbox of `domainkit.contrib.messaging`, in the transaction of the
change that causes the message, and the worker (`runworker`) sends it. So
a message goes out if, and only if, the change commits, and a failure to
send is retried without failing the request.

On this page you learn how to:

- send an email from a use case, with `send_mail` or `EmailMessage`;
- choose a backend: SMTP, a provider's API, the console, or memory for
  tests;
- write mail templates and render them with `render_mail`;
- mail the admins about errors, with `mail_admins` and
  `report_to_admins`;
- send text messages and push notifications, with `send_sms` and
  `send_push`;
- test what your code sends.

`domainkit.contrib.mail` works as Django's `django.core.mail` does. The
design is in
[ADR 0029](adr/0029-messages-to-people-leave-through-the-outbox.md).

## A first example

Install mail with messaging, and name the SMTP server:

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

In a use case, queue the message with the
[unit of work](learning.md#words):

```python
from domainkit.contrib.mail import send_mail

async with uow:
    invoice = await uow.invoices.get(invoice_id)
    invoice.issue()
    await send_mail(
        "Your invoice",
        f"Invoice {invoice.number} is ready in your account.",
        [customer.email],
        html_message=f"<p>Invoice {invoice.number} is ready.</p>",  # optional
        uow=uow,  # sent only if this commits
    )
    await uow.commit()
```

The message waits in the outbox until the commit. Then the worker sends
it. The memory backend kept this message; its first lines, made into MIME
(`to_mime()`), are:

```text
Subject: Your invoice
From: Billing <billing@example.com>
To: alice@example.com
Date: Wed, 04 Nov 2026 18:29:46 +0000
Message-ID: <179381698697.13193.7466985203696426531@example.com>
MIME-Version: 1.0
Content-Type: multipart/alternative;
 boundary="===============7234577402879629031=="
```

## Mail

`send_mail(subject, message, recipient_list, *, from_email=None,
html_message=None, uow=None)` queues one message. For more control, make
an `EmailMessage` and call its `send`:

```python
from domainkit.contrib.mail import EmailMessage

message = EmailMessage(
    subject="Welcome",
    body="Hello.",
    to=("Alice <alice@example.com>",),
    cc=("sales@example.com",),
    bcc=("archive@example.com",),
    reply_to=("help@example.com",),
    headers={"List-Unsubscribe": "<https://example.com/u/1>"},
)
await message.send(uow)  # or send() alone: its own transaction
```

- **With or without a unit of work.** With `uow`, the task is stored with
  the commit, and dropped if the unit of work rolls back. Without `uow`,
  the task is stored at once, in a transaction of its own.
  `send(message, uow)` is the same as `message.send(uow)`.
- **What they return.** `send` and `send_mail` return the queued message,
  with its sender (`DEFAULT_FROM_EMAIL` unless it named one) and a
  `Message-ID`.
- **At least once.** A worker that stops after sending sends the message
  again, with the same `Message-ID`, so mail clients can drop the copy.
- **Sending at once.** In background work that already runs in the
  worker, `await deliver([message])` sends at once, without storing the
  message in the outbox. It returns how many it sent.

An `EmailMessage` has these fields: `subject`, `body` (the plain text),
`html` (an HTML alternative), `to`, `cc`, `bcc` (recipients that no
other recipient sees), `reply_to`, `from_email`, `headers`, and
`message_id` (set when it is queued). `recipients()` returns every
address it goes to, once each. `to_mime()` returns it as the standard
library's `EmailMessage`, ready to send, with `bcc` left out of the
headers; call it on a message that `send` or `deliver` prepared.

### What a message refuses

A message refuses what could add a header or a recipient (header
injection). Each refusal is a `pydantic.ValidationError` when you make
the message:

```text
Value error, line breaks and control characters are not allowed
Value error, not an email address: user@example.com or Name <...>
Value error, a message goes to 1 to 100 addresses (to, cc, bcc)
Value error, headers are named with letters, digits, and '-', and may not replace From, To, Subject, or the message's own headers
```

- Line breaks and control characters are refused in the subject, the
  addresses, and the extra headers.
- Extra headers may not replace `From`, `To`, `Subject`, or the other
  headers that the message sets itself.
- An address is `user@example.com` or `Name <user@example.com>`, with an
  ASCII address (an internationalized domain in its `xn--` form). A list
  in one string (`"a@b.c, d@e.f"`) is refused.
- A message goes to 1 to 100 addresses (`to`, `cc`, and `bcc` together),
  with a subject of 998 characters at most, bodies of a million, and 20
  extra headers.
- Its `repr` shows only its `Message-ID`:
  `EmailMessage(message_id='<179381687771.11936.4406251986910933513@example.com>')`.
  The rest may be personal, or a secret.

### What the outbox keeps

A queued message is stored whole, in clear, in the outbox row's
`payload` (`domainkit_messages`): its recipients, subject, and bodies,
with any code in them.

- It stays there only until it is sent. The worker blanks the payload in
  the statement that marks the message done, and a cancel does the same.
- The row itself (the task's name, its id, times, attempts, and error) is
  kept `MESSAGING.RETENTION_SECONDS` (7 days), then deleted.
- A message that fails `MAX_ATTEMPTS` times is dead-lettered with its
  error, and its payload is blanked too, so it cannot be retried. Send it
  again from where it came from.
- Until it is sent, anyone who can read the table, a replica, or a backup
  can read it. For a link or a code, queue the user's id with a task of
  your own, and build the message in the worker with `deliver`, as the
  auth flows do.

### Backends

`MAIL.BACKEND` names the class that sends. `get_backend()` returns a new
instance of it:

| `MAIL.BACKEND` | Sends to |
| --- | --- |
| `domainkit.contrib.mail.backends.SmtpBackend` (default) | The SMTP server in `MAIL.SMTP`, with Python's `smtplib` on a worker thread. |
| `domainkit.contrib.mail.backends.ConsoleBackend` | Standard output, for development. |
| `domainkit.contrib.mail.backends.MemoryBackend` | `domainkit.contrib.mail.outbox`, a list, for tests. |
| `domainkit.contrib.mail.providers.SesBackend` | Amazon SES's v2 API: the MIME message, signed with SigV4. |
| `domainkit.contrib.mail.providers.SendGridBackend` | SendGrid's v3 Mail Send API. |
| `domainkit.contrib.mail.providers.MailgunBackend` | Mailgun's Messages API. |
| `domainkit.contrib.mail.providers.PostmarkBackend` | Postmark's Email API. |

Staging and production refuse the console and memory backends: the
console would print reset codes into the logs.

**SMTP.** The connection is encrypted: `STARTTLS` on port 587 by default,
or TLS from the first byte on port 465. The server's certificate is
checked against the system's certificate authorities, or `TLS_CA`. A
server that offers no `STARTTLS` gets nothing. Plain text
(`SECURITY = "none"`) is refused unless `DEBUG` is on, for a local mail
catcher. Each exchange with the server is limited by `TIMEOUT`.

**Failures.** A failure raises `MailDeliveryFailed`, which names the
error and the server's code, but never an address. The worker tries
again until `MAX_ATTEMPTS`. When the server refuses some recipients but
takes the message for others, the refused ones are logged as a count.

**Providers.** The provider backends call their HTTP API through
`domainkit.http.client` (the `httpclient` extra), with no SDK. They read
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
| `PostmarkBackend` | `SERVER_TOKEN`; `MESSAGE_STREAM` (`outbound`); `URL`. |

Every provider backend also takes `TIMEOUT` (10 seconds, at most 60).
SES is sent the message as SMTP is (`to_mime`). The others are sent its
parts: sender, recipients (Bcc never in a header), subject, text, HTML,
`Reply-To`, headers, and the `Message-ID`.

**Your own backend.** Subclass `BaseEmailBackend` and implement
`async send_messages(messages)`, which returns how many it sent:

```python
from collections.abc import Sequence

from domainkit.contrib.mail import BaseEmailBackend, EmailMessage, MailDeliveryFailed


class GatewayBackend(BaseEmailBackend):
    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        for message in messages:
            if not await gateway.post(message.to_mime().as_bytes()):
                raise MailDeliveryFailed("The gateway refused the message.")
        return len(messages)
```

### Templates

A template is up to three files under `templates/mail/` in a domain
package, named after the template:

```text
billing/templates/mail/billing/invoice.subject.txt    one line
billing/templates/mail/billing/invoice.txt            the plain-text body
billing/templates/mail/billing/invoice.html           the HTML body (optional)
```

`invoice.subject.txt`:

```text
Invoice {{ invoice.number }} is due
```

`invoice.txt`:

```text
Dear {{ customer.name }},
Invoice {{ invoice.number }} for {{ invoice.total }} is due.
```

`invoice.html`:

```text
<p>Dear {{ customer.name }},</p>
```

`render_mail` makes an `EmailMessage` from it:

```python
from domainkit.contrib.mail import render_mail

message = render_mail(
    "billing/invoice",
    {"customer": customer, "invoice": {"number": "F-7", "total": "19.90 EUR"}},
    to=[customer.email],
)
print(message.subject)
print(message.body)
print(message.html)
await message.send(uow)
```

For a customer named `Alice <Ann>`, it printed:

```text
Invoice F-7 is due
Dear Alice <Ann>,
Invoice F-7 for 19.90 EUR is due.

<p>Dear Alice &lt;Ann&gt;,</p>
```

- `{{ name }}` and `{{ a.b }}` take keys of mappings and attributes of
  objects. Names that start with `_`, and methods, are never read.
- In HTML, values are escaped (`<` becomes `&lt;`). In text they are left
  as they are, and the subject is joined onto one line.
- A name that the context lacks raises `TemplateError`, so no message
  goes out with a hole in it:
  `TemplateError: The template needs 'invoice.number', which is missing.`
- A template that no directory holds raises `TemplateDoesNotExist` (a
  kind of `TemplateError`):
  `No mail template 'billing/missing' (looked for billing/missing.txt).`
- There are no loops or conditions. Compute such text in Python and pass
  it in.
- Template names are lower-case words separated by `/`.
- `render_mail` also takes `from_email=` and `reply_to=`.

`MAIL.TEMPLATE_DIRS` are searched first, then the installed domains in
`INSTALLED_DOMAINS` order. The first that has the text body wins. List a
directory there to replace a template that a contrib app ships, such as
`auth/password_reset` ([auth.md](auth/logging-in.md#password-reset)).

### Settings

These are the keys of `MAIL`:

| `MAIL` key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `...SmtpBackend` | Dotted path to a `BaseEmailBackend`, or the name an installed package offers it by ([extension points](reference/extension-points.md#backends-from-an-installed-package)). Checked at startup. |
| `DEFAULT_FROM_EMAIL` | `"webmaster@localhost"` | The sender of messages that name none. It must be one address. |
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

The models behind these tables are `MailSettings` and `SmtpSettings`.
`USER` without `PASSWORD`, or the other way round, is refused when the
settings load: "USER and PASSWORD go together".

### Telling the admins

`MAIL["ADMINS"]` names who hears of trouble (Django's `ADMINS`).
`mail_admins` in `domainkit.contrib.mail.admins` writes to them through
the outbox, as any mail. With no admin named, it sends nothing and
returns `None`:

```python
from domainkit.contrib.mail.admins import mail_admins

await mail_admins("Nightly import failed", "3 of 120 files were refused.", uow=uow)
```

To mail them each unexpected error (Django's error emails), add the
reporter to `ERROR_REPORTERS`, beside an error tracker or instead of
one:

```python
MAIL = {"ADMINS": ("ops@example.com",)}  # and the other keys
ERROR_REPORTERS = ("domainkit.contrib.mail.admins.report_to_admins",)
```

- A report names the error's type, where it was raised, the frames of its
  traceback, and the request's context (its id, path, and method), so the
  logs can be found.
- It never holds the error's message, nor any value of the frames, which
  may hold what a user sent.
- Each process mails at most one report per kind and place in
  `ADMINS_EVERY`, so a failing dependency does not flood the inbox.
- The report goes straight through the backend, from the reporting
  thread, not through the outbox.

A report looks like this:

```text
Subject: Error: ZeroDivisionError in total

builtins.ZeroDivisionError was raised at /srv/shop/src/billing/domain.py:42 in total.

Context:
  method: GET
  path: /invoices/7
  request_id: 5fc4ce02c2df56870ec65d4ab50d9a04

Traceback, innermost last (no values):
  /srv/shop/src/billing/routes.py:18 in read_invoice
  /srv/shop/src/billing/domain.py:42 in total

The error's message is left out: it may hold what a user sent.
Find it in the logs by the request id.
```

## Text messages and push notifications

Install notifications with messaging, and name a backend for each
channel:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.notifications",
    # your domains
]
NOTIFICATIONS = {
    "SMS_BACKEND": "domainkit.contrib.notifications.sms.TwilioSmsBackend",
    "PUSH_BACKEND": "domainkit.contrib.notifications.push.FcmPushBackend",
}
```

Queue them with the unit of work, as mail:

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

- `send_sms(to, body, *, sender=None, uow=None)` queues an `SmsMessage`.
- `send_push(token, title, body, *, data=None, uow=None)` queues a
  `PushMessage`.
- To name a push notification's platform, make the message and call its
  `send`:
  `await PushMessage(token=t, title="Shipped", body="...", platform="huawei").send(uow)`.
  `SmsMessage(...).send(uow)` works the same way.
- `deliver_sms(messages)` and `deliver_push(messages)` send at once, from
  the worker, and return how many were sent.
- `get_sms_backend()` and `get_push_backend(platform=None)` return a new
  instance of the backend that would send.

### Providers

`SmsBackend` and `PushBackend` are ports. These adapters call the
providers' HTTP APIs through `domainkit.http.client`, with no SDK:

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

`...` stands for `domainkit.contrib.notifications`. Every adapter also
takes `TIMEOUT` (10 seconds, at most 60) and `URL`, which replaces the
provider's address.

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

- **Platforms.** A notification may name its `platform` (`fcm`, `apns`,
  `huawei`), which picks its backend in `PUSH_BACKENDS`. Others go to
  `PUSH_BACKEND`.
- **Provider tokens are kept**: Google's and Huawei's until 5 minutes
  before they expire, and Apple's provider token for 50 minutes.
- **Tokens that are gone.** A device token that the service no longer
  knows (FCM's `UNREGISTERED`, APNs' `410`, Push Kit's `80300007`) raises
  `PushTokenGone`. The worker drops the notification, without trying
  again, and publishes `PushTokenExpired` with the token's SHA-256 digest
  (`token_digest(token)`, in `domainkit.contrib.notifications.events`).
  `domainkit.contrib.devices` uses it to forget the token
  ([push tokens](auth/devices.md#push-tokens)).

**Your own adapter.** Subclass `SmsBackend` or `PushBackend`, implement
`async send_messages(messages)`, return how many it sent, and raise
`NotificationFailed` (with the provider's code, never a number or a
token) for the worker to try again:

```python
from collections.abc import Sequence

from domainkit.contrib.notifications import NotificationFailed, SmsBackend, SmsMessage


class GatewaySmsBackend(SmsBackend):
    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        for message in messages:
            code = await gateway.send(message.to, message.body, ref=message.message_id)
            if code != "OK":
                raise NotificationFailed(f"The gateway answered {code}.")
        return len(messages)
```

Every message carries a `message_id`, the same on every try, to pass on
to providers that drop duplicates.

### Messages and their limits

| Message | Fields and limits |
| --- | --- |
| `SmsMessage` | `to`: E.164, `+` and 7 to 15 digits, not starting with 0; `body`: 1 to 1600 characters; `sender`: an alphanumeric id of 11 characters at most, or a number. |
| `PushMessage` | `token`: one line, up to 4096 characters; `title`: one line, 255; `body`: 4096; `data`: up to 50 string pairs of 1024 characters each; `platform`: its push service, or `None`. |

A value out of these limits raises `pydantic.ValidationError`, for
example `String should match pattern '^\+[1-9][0-9]{6,14}$'` for the
number `0612345678`.

Their reprs show only the `message_id`: `SmsMessage(message_id=None)`
before it is queued. Queued messages are kept in the outbox as mail is,
until they are sent, cancelled, or dead-lettered, and then blanked (see
[What the outbox keeps](#what-the-outbox-keeps)).

The console backends (`ConsoleSmsBackend`, `ConsolePushBackend`, the
defaults) print what they would send. The memory backends
(`MemorySmsBackend`, `MemoryPushBackend`) keep it in `sms_outbox` and
`push_outbox`, for tests. Staging and production refuse both kinds.

The `NOTIFICATIONS` keys; the model behind them is
`NotificationsSettings`:

| `NOTIFICATIONS` key | Default | Meaning |
| --- | --- | --- |
| `SMS_BACKEND` | `...ConsoleSmsBackend` | Dotted path to an `SmsBackend`. |
| `PUSH_BACKEND` | `...ConsolePushBackend` | Dotted path to a `PushBackend`. |
| `PUSH_BACKENDS` | `{}` | A backend per platform, for notifications that name theirs. |
| `SMS_OPTIONS` | `{}` | The SMS provider's settings, which its backend reads. |
| `PUSH_OPTIONS` | `{}` | Each push provider's settings, by its name (`fcm`, `apns`, `huawei`). |
| `MAX_ATTEMPTS` | `5` | Tries of each message before it is dead-lettered. |
| `QUEUE` | `None` | The worker queue that sends them; `default` when `None`. |

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `pydantic.ValidationError` when you make a message | An address, a header, the subject, a number, or a size is refused. | Fix the value; never pass a list of addresses in one string. |
| `MailDeliveryFailed` in the worker's log | The server or the provider refused the message. | The worker tries again until `MAX_ATTEMPTS`; then see `messages list dead`. |
| `NotificationFailed` | The SMS or push provider refused. | The same: the worker tries again. |
| `PushTokenGone` | The device token is no longer known. | Nothing: the token is forgotten through `PushTokenExpired`. |
| `TemplateError`, `TemplateDoesNotExist` | A template has bad braces, lacks a value, or is missing. | Fix the template or the context. |
| `ImproperlyConfigured` at startup | A backend path names no backend, or its options are wrong. | Fix `MAIL` or `NOTIFICATIONS`. |
| The application refuses to start in production | A console or memory backend. | Name a real backend. |

## Testing

Point the test settings at the memory backends:

```python
# config/settings/test.py
MAIL = {"BACKEND": "domainkit.contrib.mail.backends.MemoryBackend"}
NOTIFICATIONS = {
    "SMS_BACKEND": "domainkit.contrib.notifications.MemorySmsBackend",
    "PUSH_BACKEND": "domainkit.contrib.notifications.MemoryPushBackend",
}
```

Run the worker once, then read the outbox:

```python
import pytest

from domainkit.contrib import mail
from domainkit.contrib.messaging import Worker
from domainkit.contrib.notifications import sms_outbox


@pytest.mark.anyio
@pytest.mark.usefixtures("db")
async def test_signing_up_sends_a_welcome() -> None:
    await sign_up(email="alice@example.com")
    await Worker().run_once()
    (message,) = mail.outbox
    assert message.subject == "Welcome"
    assert sms_outbox == []
```

The test plugin empties `mail.outbox`, `sms_outbox`, and `push_outbox`
before every test. More in [testing.md](testing.md).

## Common mistakes

- **Sending without `uow` inside a use case.** The message is stored at
  once, even if the use case then rolls back. Pass `uow=uow`.
- **Putting a reset code in a queued message.** It sits in the outbox in
  clear until it is sent. Queue the user's id, and build the message in
  the worker with `deliver`.
- **Putting several addresses in one string.** It is refused. Pass a list
  or a tuple of addresses.
- **Writing a loop in a template.** Templates have no loops or
  conditions. Build the text in Python, and pass it in.
- **Leaving the console backend on in production.** The application
  refuses to start. Name SMTP or a provider.

## See also

- [Messaging](contrib/messaging.md): the outbox, the worker, retries, and
  dead letters.
- [Logging in](auth/logging-in.md#password-reset): the password reset
  mail and its template.
- [Devices](auth/devices.md#push-tokens): push tokens, and how they are
  forgotten.
- [Extension points](reference/extension-points.md#backends-from-an-installed-package):
  backends from an installed package.

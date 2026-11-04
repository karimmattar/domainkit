# An SMS provider of your own

This recipe connects `domainkit.contrib.notifications` to a text-message
provider that domainkit does not ship: one with a JSON API and a bearer
token.

## The problem

`domainkit.contrib.notifications` sends text messages through the
outbox, by an `SmsBackend`. Twilio, Vonage, SNS, Unifonic, and Huawei's
SMN come with it ([mail.md](../mail.md#text-messages-and-push-notifications)).
Your provider is another one. You want its messages to leave with the
commit, to be retried when the provider fails, and to keep secret the
text they carry.

A backend of your own is a class with one method.

You need a project with `domainkit[sqlite,testing,httpclient]` installed.
Every step here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project

```bash
domainkit init alertsAPI alerts
cd alerts
python manage.py makemigrations -m "auth"
python manage.py migrate
```

### Step 2: write the backend

A backend subclasses `SmsBackend`, and answers `send_messages`: it sends
each message, and returns how many went. It raises `NotificationFailed`
when the provider refuses a message, or cannot be reached.

The backend reads its settings from `NOTIFICATIONS["SMS_OPTIONS"]`. Its
calls go through `domainkit.http.client`, with its timeouts, retries, and
circuit breaker:

```python title="src/config/sms.py (new file)"
"""Text messages through Example SMS's JSON API."""

from collections.abc import Sequence

from domainkit.apps import registry
from domainkit.contrib.notifications import (
    NotificationFailed,
    NotificationsSettings,
    SmsBackend,
    SmsMessage,
)
from domainkit.http.client import HttpClient
from pydantic import BaseModel, ConfigDict, SecretStr


class Options(BaseModel):
    """``NOTIFICATIONS["SMS_OPTIONS"]``: where the API is, and its token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    URL: str = "https://api.example-sms.com"
    TOKEN: SecretStr
    SENDER: str


class ExampleSmsBackend(SmsBackend):
    """POST each message to ``/v1/messages``, with the account's token."""

    def __init__(self) -> None:
        """Read the options, once per backend made."""
        options = registry.settings(NotificationsSettings).SMS_OPTIONS
        self.options = Options.model_validate(options)
        self.client = HttpClient(timeout=10, propagate=False)

    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Send each message; a refusal names the provider's error, never the text."""
        token = self.options.TOKEN.get_secret_value()
        for message in messages:
            answer = await self.client.post(
                f"{self.options.URL}/v1/messages",
                json={
                    "to": message.to,
                    "text": message.body,
                    "from": message.sender or self.options.SENDER,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if answer.status_code != 201:
                raise NotificationFailed(
                    f"Example SMS refused a message ({answer.status_code})."
                )
        return len(messages)
```

### Step 3: install the app and name the backend

Install the notifications app, and name the backend and its options. The
token comes from the environment:

```python title="src/config/settings/base.py"
INSTALLED_DOMAINS = [*INSTALLED_DOMAINS, "domainkit.contrib.notifications"]
NOTIFICATIONS = {
    "SMS_BACKEND": "config.sms.ExampleSmsBackend",
    "SMS_OPTIONS": {
        "TOKEN": env.str("SMS_TOKEN", "test-token"),
        "SENDER": "Alerts",
    },
}
```

A use case now queues a message with `send_sms(to, body, uow=uow)`. The
message leaves with the commit, and the worker hands it to the backend.

### Step 4: test the backend

`stub` sends the calls made in its block to a function instead of the
network. So the test sees each request, and chooses the answer. `db`
starts the project's domains (and an empty database) for the test:

```python title="tests/test_sms.py (new file)"
"""The backend posts each message with the token, and reports a refusal."""

import json

import httpx2
import pytest
from domainkit.contrib.notifications import NotificationFailed, SmsMessage
from domainkit.http.client import stub

from config.sms import ExampleSmsBackend

pytestmark = pytest.mark.anyio
MESSAGE = SmsMessage(to="+33612345678", body="Your code is 123456")


async def test_each_message_is_posted_with_the_token(db: None) -> None:
    """One POST per message, with the bearer token and the sender."""
    seen: list[httpx2.Request] = []

    def provider(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(201, json={"id": "m1"})

    with stub(provider):
        sent = await ExampleSmsBackend().send_messages([MESSAGE, MESSAGE])

    assert sent == 2
    assert len(seen) == 2
    assert seen[0].url == "https://api.example-sms.com/v1/messages"
    assert seen[0].headers["authorization"] == "Bearer test-token"
    assert json.loads(seen[0].content) == {
        "to": "+33612345678",
        "text": "Your code is 123456",
        "from": "Alerts",
    }


async def test_a_refusal_is_a_failure_the_worker_retries(db: None) -> None:
    """A 4xx is NotificationFailed, which names the status, not the text."""
    with (
        stub(lambda request: httpx2.Response(422)),
        pytest.raises(NotificationFailed, match=r"refused a message \(422\)"),
    ):
        await ExampleSmsBackend().send_messages([MESSAGE])
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

## How it works

1. A use case calls `send_sms(to, body, uow=uow)`. The message is stored
   in the outbox, in the same transaction as the use case's changes.
2. After the commit, the worker takes the message, and calls
   `send_messages` of the backend that `NOTIFICATIONS["SMS_BACKEND"]`
   names.
3. If the backend raises `NotificationFailed`, the worker retries the
   message later. After `MAX_ATTEMPTS`, it keeps the message as a dead
   letter (`manage.py messages list dead`).

Keep the text out of errors: a message holds codes and personal data. The
backend's error above names the provider's status, never the text.

A mail service has the same shape: subclass
`domainkit.contrib.mail.backends.BaseEmailBackend`, answer
`send_messages`, and name it in `MAIL["BACKEND"]` ([mail.md](../mail.md)).

## See also

- [Mail and notifications](../mail.md): every setting of `MAIL` and
  `NOTIFICATIONS`.
- [Extension points](../reference/extension-points.md#backends): every
  backend you can write.
- [Runtime](../runtime.md#outgoing-http-calls): `HttpClient`, its
  timeouts, and its circuit breaker.

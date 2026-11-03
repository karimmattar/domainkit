# An SMS provider of your own

`domainkit.contrib.notifications` sends text messages through the outbox,
by an `SmsBackend`. Twilio, Vonage, SNS, Unifonic, and Huawei's SMN come
with it ([mail.md](../mail.md#text-messages-and-push-notifications)); a provider of
your own is a class with one method, here one with a JSON API and a
bearer token.

You need a project with `domainkit[sqlite,testing,httpclient]` installed.
Every step here is run as written by `domainkit_tests/test_recipes.py`.

## The backend

```bash
domainkit init alertsAPI alerts
cd alerts
python manage.py makemigrations -m "auth"
python manage.py migrate
```

A backend subclasses `SmsBackend` and answers `send_messages`: send each,
return how many went, and raise `NotificationFailed` when the provider
refuses one or cannot be reached. The worker then retries the message,
and keeps it as a dead letter after `MAX_ATTEMPTS`. Its settings come
from `NOTIFICATIONS["SMS_OPTIONS"]`, and its calls go through
`domainkit.http.client`, with its timeouts, retries, and circuit breaker:

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

Install the notifications app, and name the backend and its options; the
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

A use case queues a message with `send_sms(to, body, uow=uow)`: it leaves
with the commit, and the worker hands it to the backend.

## A test of the backend

`stub` sends the calls made in its block to a function instead of the
network, so the test sees each request and chooses the answer; `db`
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

```bash
ruff check --fix .
ruff format --check .
pytest
```

A mail service is the same shape: subclass
`domainkit.contrib.mail.backends.BaseEmailBackend`, answer
`send_messages`, and name it in `MAIL["BACKEND"]`
([mail.md](../mail.md)).

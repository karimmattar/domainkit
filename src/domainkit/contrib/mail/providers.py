"""Mail by API: Amazon SES, SendGrid, Mailgun, Postmark.

Name one as ``MAIL.BACKEND``, with its settings in ``MAIL.OPTIONS``::

    MAIL = {
        "BACKEND": "domainkit.contrib.mail.providers.PostmarkBackend",
        "OPTIONS": {"SERVER_TOKEN": env.str("POSTMARK_TOKEN")},
        "DEFAULT_FROM_EMAIL": "Acme <hello@acme.example>",
    }

Each calls its provider's HTTP API through ``domainkit.http.client`` (the
``httpclient`` extra), with no SDK. SES is sent the message as MIME,
built as SMTP's is (``to_mime``), signed with SigV4; the others are sent
its parts: sender, recipients (Bcc as such, never in a header), subject,
text, HTML, ``Reply-To``, and extra headers. A refusal raises
``MailDeliveryFailed`` with the provider's error code, never an address,
and the worker tries again.
"""

import base64
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domainkit.apps import registry

from .backends import BaseEmailBackend, MailDeliveryFailed
from .message import EmailMessage
from .settings import MailSettings

if TYPE_CHECKING:
    import httpx2

    from domainkit.http.client import HttpClient

__all__ = ["MailgunBackend", "PostmarkBackend", "SendGridBackend", "SesBackend"]

TIMEOUT: Final = 10.0
_clients: dict[float, "HttpClient"] = {}


class _Options(BaseModel):
    """What every provider's options share."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    TIMEOUT: float = Field(default=TIMEOUT, gt=0, le=60)


class ProviderBackend(BaseEmailBackend):
    """A provider's HTTP API: ``MAIL.OPTIONS`` read into ``options``."""

    name: ClassVar[str]
    """The provider, as errors name it."""
    Options: ClassVar[type[_Options]]

    def __init__(self, options: Any = None) -> None:  # noqa: ANN401 - the provider's Options.
        """Read ``options``, or ``MAIL.OPTIONS``."""
        if options is None:
            options = registry.settings(MailSettings).OPTIONS
        self.options = (
            options
            if isinstance(options, self.Options)
            else self.Options.model_validate(options)
        )
        from domainkit.http.client import HttpClient

        timeout = self.options.TIMEOUT
        if timeout not in _clients:
            _clients[timeout] = HttpClient(timeout=timeout, propagate=False)
        self.client = _clients[timeout]

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Send each prepared message; return how many were sent.

        Raises:
            MailDeliveryFailed: The provider refused one, or could not be reached.
        """
        for message in messages:
            async with self.reached():
                await self.send(message)
        return len(messages)

    async def send(self, message: EmailMessage) -> None:
        """Send one prepared message."""
        raise NotImplementedError

    @asynccontextmanager
    async def reached(self) -> AsyncIterator[None]:
        """Turn a failed call into ``MailDeliveryFailed``, naming no URL."""
        from domainkit.cloud import http_errors

        try:
            yield
        except http_errors() as exc:
            raise MailDeliveryFailed(
                f"{self.name} could not be reached ({type(exc).__name__})."
            ) from None

    def refused(self, code: object) -> MailDeliveryFailed:
        """``MailDeliveryFailed`` naming the provider's error code."""
        return MailDeliveryFailed(f"{self.name} refused the message: {code}.")


def _json(response: "httpx2.Response") -> Any:  # noqa: ANN401 - any JSON.
    """The answer's JSON, or None."""
    try:
        return response.json()
    except ValueError:
        return None


def _person(value: str) -> dict[str, str]:
    """``Name <address>`` as ``{"email", "name"}`` (no name: the address alone)."""
    name, address = parseaddr(value)
    return {"email": address, "name": name} if name else {"email": address}


class SesOptions(_Options):
    """``MAIL.OPTIONS`` of ``SesBackend``."""

    REGION: str = Field(min_length=1)
    ACCESS_KEY_ID: str | None = None
    SECRET_ACCESS_KEY: SecretStr | None = None
    """With ``ACCESS_KEY_ID``; neither: the environment's credentials."""
    CONFIGURATION_SET: str | None = None
    URL: str | None = None
    """None: ``https://email.<REGION>.amazonaws.com``."""


class SesBackend(ProviderBackend):
    """Amazon SES's v2 API: the message as MIME, signed with SigV4."""

    name = "Amazon SES"
    Options = SesOptions
    options: SesOptions

    def __init__(self, options: Any = None) -> None:  # noqa: ANN401
        """Read the options, and where the credentials come from."""
        super().__init__(options)
        from domainkit.cloud.aws import credential_source

        secret = self.options.SECRET_ACCESS_KEY
        self.credentials = credential_source(
            self.options.REGION,
            self.options.ACCESS_KEY_ID,
            None if secret is None else secret.get_secret_value(),
        )

    async def send(self, message: EmailMessage) -> None:
        """Send the raw message to its recipients, Bcc included."""
        import json

        from domainkit.cloud.aws import signed_request

        options = self.options
        body: dict[str, Any] = {
            "FromEmailAddress": message.from_email,
            "Destination": {
                "ToAddresses": list(message.to),
                "CcAddresses": list(message.cc),
                "BccAddresses": list(message.bcc),
            },
            "Content": {
                "Raw": {"Data": base64.b64encode(message.to_mime().as_bytes()).decode()}
            },
        }
        if options.CONFIGURATION_SET:
            body["ConfigurationSetName"] = options.CONFIGURATION_SET
        url = options.URL or f"https://email.{options.REGION}.amazonaws.com"
        response = await signed_request(
            self.client,
            "POST",
            f"{url}/v2/email/outbound-emails",
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            region=options.REGION,
            service="ses",
            credentials=self.credentials,
        )
        if response.status_code != 200:
            error = _json(response) or {}
            raise self.refused(
                response.headers.get("x-amzn-ErrorType", "").split(":")[0]
                or error.get("__type")
                or response.status_code
            )


class SendGridOptions(_Options):
    """``MAIL.OPTIONS`` of ``SendGridBackend``."""

    API_KEY: SecretStr
    URL: str = "https://api.sendgrid.com"
    """``https://api.eu.sendgrid.com`` for EU data residency."""


class SendGridBackend(ProviderBackend):
    """SendGrid's v3 Mail Send API."""

    name = "SendGrid"
    Options = SendGridOptions
    options: SendGridOptions

    async def send(self, message: EmailMessage) -> None:
        """POST the message's parts; 202 is accepted."""
        personalization: dict[str, Any] = {"to": [_person(a) for a in message.to]}
        for field in ("cc", "bcc"):
            if values := getattr(message, field):
                personalization[field] = [_person(a) for a in values]
        content = [{"type": "text/plain", "value": message.body}]
        if message.html is not None:
            content.append({"type": "text/html", "value": message.html})
        payload: dict[str, Any] = {
            "personalizations": [personalization],
            "from": _person(message.from_email or ""),
            "subject": message.subject,
            "content": content,
            "headers": {**message.headers, "Message-ID": message.message_id or ""},
        }
        if message.reply_to:
            payload["reply_to_list"] = [_person(a) for a in message.reply_to]
        response = await self.client.post(
            f"{self.options.URL}/v3/mail/send",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.options.API_KEY.get_secret_value()}"
            },
        )
        if response.status_code not in (200, 202):
            raise self.refused(response.status_code)


class MailgunOptions(_Options):
    """``MAIL.OPTIONS`` of ``MailgunBackend``."""

    API_KEY: SecretStr
    DOMAIN: str = Field(min_length=1, pattern=r"^[A-Za-z0-9.-]+$")
    URL: str = "https://api.mailgun.net"
    """``https://api.eu.mailgun.net`` for a domain in the EU region."""


class MailgunBackend(ProviderBackend):
    """Mailgun's Messages API."""

    name = "Mailgun"
    Options = MailgunOptions
    options: MailgunOptions

    async def send(self, message: EmailMessage) -> None:
        """POST the message's parts as a form."""
        form: dict[str, Any] = {
            "from": message.from_email,
            "to": list(message.to),
            "subject": message.subject,
            "text": message.body,
            "h:Message-Id": message.message_id,
        }
        for field in ("cc", "bcc"):
            if values := getattr(message, field):
                form[field] = list(values)
        if message.html is not None:
            form["html"] = message.html
        if message.reply_to:
            form["h:Reply-To"] = ", ".join(message.reply_to)
        for header, value in message.headers.items():
            form[f"h:{header}"] = value
        secret = base64.b64encode(
            f"api:{self.options.API_KEY.get_secret_value()}".encode()
        ).decode()
        response = await self.client.post(
            f"{self.options.URL}/v3/{self.options.DOMAIN}/messages",
            data=form,
            headers={"Authorization": f"Basic {secret}"},
        )
        if response.status_code != 200:
            raise self.refused(response.status_code)


class PostmarkOptions(_Options):
    """``MAIL.OPTIONS`` of ``PostmarkBackend``."""

    SERVER_TOKEN: SecretStr
    MESSAGE_STREAM: str = "outbound"
    URL: str = "https://api.postmarkapp.com"


class PostmarkBackend(ProviderBackend):
    """Postmark's Email API."""

    name = "Postmark"
    Options = PostmarkOptions
    options: PostmarkOptions

    async def send(self, message: EmailMessage) -> None:
        """POST the message's parts; ``ErrorCode`` 0 is sent."""
        payload: dict[str, Any] = {
            "From": message.from_email,
            "To": ", ".join(message.to),
            "Subject": message.subject,
            "TextBody": message.body,
            "MessageStream": self.options.MESSAGE_STREAM,
            "Headers": [
                {"Name": name, "Value": value}
                for name, value in {
                    **message.headers,
                    "Message-ID": message.message_id or "",
                }.items()
            ],
        }
        for field, key in (("cc", "Cc"), ("bcc", "Bcc"), ("reply_to", "ReplyTo")):
            if values := getattr(message, field):
                payload[key] = ", ".join(values)
        if message.html is not None:
            payload["HtmlBody"] = message.html
        response = await self.client.post(
            f"{self.options.URL}/email",
            json=payload,
            headers={
                "Accept": "application/json",
                "X-Postmark-Server-Token": self.options.SERVER_TOKEN.get_secret_value(),
            },
        )
        code = (_json(response) or {}).get("ErrorCode")
        if response.status_code != 200 or code not in (0, None):
            raise self.refused(
                f"error {code if code is not None else response.status_code}"
            )

"""Text message providers: Twilio, Vonage, Amazon SNS, Unifonic, Huawei Cloud.

Name one in ``NOTIFICATIONS``, with its settings in ``SMS_OPTIONS``::

    NOTIFICATIONS = {
        "SMS_BACKEND": "domainkit.contrib.notifications.sms.TwilioSmsBackend",
        "SMS_OPTIONS": {
            "ACCOUNT_SID": "AC...",
            "AUTH_TOKEN": env.str("TWILIO_AUTH_TOKEN"),
            "FROM": "+15005550006",
        },
    }

Each calls its provider's HTTP API through ``domainkit.outbound.client``
(the ``httpclient`` extra), with no SDK. A message's ``sender`` replaces
the configured one where the provider allows it. A refusal raises
``NotificationFailed`` with the provider's error code, never the number
or the text, and the worker tries again. One of the codes the provider
documents for a message it will refuse again (``lasting``: a number that
is not valid or not a mobile's, a recipient who opted out or is barred, a
destination the account may not send to, a text it cannot take) raises
``NotificationRefused``, which is not tried again.
"""

import base64
import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from domainkit import clock

from .backends import NotificationFailed, NotificationRefused, SmsBackend
from .messages import SmsMessage
from .settings import NotificationsSettings

if TYPE_CHECKING:
    import httpx2

    from domainkit.outbound.client import HttpClient


__all__ = [
    "HuaweiSmsBackend",
    "SnsSmsBackend",
    "TwilioSmsBackend",
    "UnifonicSmsBackend",
    "VonageSmsBackend",
]

TIMEOUT: Final = 10.0
WSSE_REALM: Final = 'WSSE realm="SDP",profile="UsernameToken",type="Appkey"'


_clients: dict[tuple[float, bool], "HttpClient"] = {}


def shared_client(timeout: float, *, http2: bool = False) -> "HttpClient":
    """One client per timeout, shared by every backend made: its pool is kept.

    Text messages and push share it; its metrics call it ``notifications``.
    """
    from domainkit.outbound.client import HttpClient

    key = (timeout, http2)
    if key not in _clients:
        _clients[key] = HttpClient(
            timeout=timeout, propagate=False, http2=http2, name="notifications"
        )
    return _clients[key]


class _Options(BaseModel):
    """What every provider's options share."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    TIMEOUT: float = Field(default=TIMEOUT, gt=0, le=60)


class ProviderSmsBackend(SmsBackend):
    """A provider's HTTP API: ``SMS_OPTIONS`` read into ``options``."""

    name: ClassVar[str]
    """The provider, as errors name it."""
    Options: ClassVar[type[_Options]]
    lasting: ClassVar[frozenset[str]] = frozenset()
    """The provider's codes for a message it will refuse again, as it is."""

    def __init__(self, options: Any = None) -> None:  # noqa: ANN401 - the provider's Options.
        """Read ``options``, or ``NOTIFICATIONS.SMS_OPTIONS``."""
        if options is None:
            from domainkit.apps import registry

            options = registry.settings(NotificationsSettings).SMS_OPTIONS
        self.options = (
            options
            if isinstance(options, self.Options)
            else self.Options.model_validate(options)
        )
        self.client: HttpClient = shared_client(self.options.TIMEOUT)

    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Send each message; return how many were sent.

        Raises:
            NotificationFailed: The provider refused one, or could not be reached.
        """
        for message in messages:
            async with reached(self.name):
                await self.send(message)
        return len(messages)

    async def send(self, message: SmsMessage) -> None:
        """Send one message."""
        raise NotImplementedError

    def refused(self, code: object, *, shown: str = "") -> NotificationFailed:
        """The error for the provider's ``code``, which it keeps and names.

        ``NotificationRefused`` for one of ``lasting``, which the worker
        does not try again; ``NotificationFailed`` for any other. ``shown``
        comes before the code in the message.
        """
        text = str(code)
        kind = NotificationRefused if text in self.lasting else NotificationFailed
        return kind(f"{self.name} refused the message: {shown}{text}.", code=text)


@asynccontextmanager
async def reached(provider: str) -> AsyncIterator[None]:
    """Turn a call that failed, or a token refused, into ``NotificationFailed``.

    Its message names the provider and the failure's kind, never a URL.
    """
    from domainkit.cloud import CloudAuthError, http_errors

    failures: tuple[type[BaseException], ...] = (CloudAuthError, *http_errors())
    try:
        yield
    except failures as exc:
        raise NotificationFailed(
            f"{provider} could not be reached ({type(exc).__name__})."
        ) from None


def json_of(response: "httpx2.Response") -> Any:  # noqa: ANN401 - any JSON.
    """The answer's JSON, or None."""
    try:
        return response.json()
    except ValueError:
        return None


def _basic(user: str, password: str) -> str:
    """An ``Authorization`` header for HTTP Basic."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class TwilioOptions(_Options):
    """``SMS_OPTIONS`` of ``TwilioSmsBackend``."""

    ACCOUNT_SID: str = Field(min_length=1)
    AUTH_TOKEN: SecretStr
    """The account's auth token, or an API key's secret with ``API_KEY_SID``."""
    API_KEY_SID: str | None = None
    FROM: str | None = None
    """The number or sender id messages come from."""
    MESSAGING_SERVICE_SID: str | None = None
    """A messaging service that picks the sender, instead of ``FROM``."""
    URL: str = "https://api.twilio.com"

    @model_validator(mode="after")
    def _a_sender(self) -> "TwilioOptions":
        """Messages come from ``FROM`` or a messaging service."""
        if not (self.FROM or self.MESSAGING_SERVICE_SID):
            raise ValueError("set FROM or MESSAGING_SERVICE_SID")
        return self


class TwilioSmsBackend(ProviderSmsBackend):
    """Twilio's Messages API."""

    name = "Twilio"
    Options = TwilioOptions
    options: TwilioOptions
    lasting = frozenset({"21211", "21408", "21610", "21612", "21614"})
    """An invalid ``To`` (``21211``), not a mobile number (``21614``), a
    recipient who replied STOP (``21610``), a region the account's geo
    permissions block (``21408``), and a ``To`` it cannot reach from this
    sender (``21612``)."""

    async def send(self, message: SmsMessage) -> None:
        """POST the message to the account's Messages."""
        options = self.options
        form = {"To": message.to, "Body": message.body}
        if message.sender or options.FROM:
            form["From"] = message.sender or options.FROM or ""
        if options.MESSAGING_SERVICE_SID:
            form["MessagingServiceSid"] = options.MESSAGING_SERVICE_SID
        response = await self.client.post(
            f"{options.URL}/2010-04-01/Accounts/{options.ACCOUNT_SID}/Messages.json",
            data=form,
            headers={
                "Authorization": _basic(
                    options.API_KEY_SID or options.ACCOUNT_SID,
                    options.AUTH_TOKEN.get_secret_value(),
                )
            },
        )
        if response.status_code not in (200, 201):
            body = json_of(response) or {}
            raise self.refused(body.get("code", response.status_code), shown="error ")


class VonageOptions(_Options):
    """``SMS_OPTIONS`` of ``VonageSmsBackend``."""

    API_KEY: str = Field(min_length=1)
    API_SECRET: SecretStr
    FROM: str = Field(min_length=1)
    URL: str = "https://rest.nexmo.com"


class VonageSmsBackend(ProviderSmsBackend):
    """Vonage's SMS API (formerly Nexmo)."""

    name = "Vonage"
    Options = VonageOptions
    options: VonageOptions
    lasting = frozenset({"3", "6", "7", "12", "33"})
    """A parameter's value refused (``3``), a message it cannot route, such
    as an unknown prefix (``6``), a barred number (``7``), a text too long
    (``12``), and a number de-activated (``33``)."""

    async def send(self, message: SmsMessage) -> None:
        """POST the message; every part must be accepted (status ``0``)."""
        options = self.options
        response = await self.client.post(
            f"{options.URL}/sms/json",
            data={
                "api_key": options.API_KEY,
                "api_secret": options.API_SECRET.get_secret_value(),
                "from": message.sender or options.FROM,
                "to": message.to.removeprefix("+"),
                "text": message.body,
                "type": "text" if message.body.isascii() else "unicode",
                "client-ref": str(message.message_id or ""),
            },
        )
        parts = (json_of(response) or {}).get("messages") or [{}]
        failed = [part.get("status") for part in parts if part.get("status") != "0"]
        if response.status_code != 200 or failed:
            raise self.refused(
                failed[0] if failed else response.status_code, shown="status "
            )


class SnsOptions(_Options):
    """``SMS_OPTIONS`` of ``SnsSmsBackend``."""

    REGION: str = Field(min_length=1)
    ACCESS_KEY_ID: str | None = None
    SECRET_ACCESS_KEY: SecretStr | None = None
    """With ``ACCESS_KEY_ID``; neither: the environment's credentials."""
    SENDER_ID: str | None = None
    SMS_TYPE: str = Field(
        default="Transactional", pattern="^(Transactional|Promotional)$"
    )
    URL: str | None = None
    """None: ``https://sns.<REGION>.amazonaws.com``."""


class SnsSmsBackend(ProviderSmsBackend):
    """Amazon SNS's Publish, to a phone number, signed with SigV4."""

    name = "Amazon SNS"
    Options = SnsOptions
    options: SnsOptions
    lasting = frozenset(
        {"InvalidParameter", "InvalidParameterValue", "ParameterValueInvalid"}
    )
    """Publish's errors for a parameter it refuses, such as the number."""

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

    async def send(self, message: SmsMessage) -> None:
        """Publish the message to its number, transactional unless set."""
        from domainkit.cloud.aws import signed_request

        options = self.options
        attributes = {"AWS.SNS.SMS.SMSType": options.SMS_TYPE}
        if message.sender or options.SENDER_ID:
            attributes["AWS.SNS.SMS.SenderID"] = (
                message.sender or options.SENDER_ID or ""
            )
        form = {
            "Action": "Publish",
            "Version": "2010-03-31",
            "PhoneNumber": message.to,
            "Message": message.body,
        }
        for number, (name, value) in enumerate(sorted(attributes.items()), start=1):
            entry = f"MessageAttributes.entry.{number}"
            form[f"{entry}.Name"] = name
            form[f"{entry}.Value.DataType"] = "String"
            form[f"{entry}.Value.StringValue"] = value
        response = await signed_request(
            self.client,
            "POST",
            options.URL or f"https://sns.{options.REGION}.amazonaws.com/",
            body=urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            region=options.REGION,
            service="sns",
            credentials=self.credentials,
        )
        if response.status_code != 200:
            raise self.refused(_aws_code(response.text) or response.status_code)


def _aws_code(text: str) -> str | None:
    """The ``<Code>`` of an AWS query API's error, if it has one."""
    start, end = text.find("<Code>"), text.find("</Code>")
    if start < 0 or end < start:
        return None
    return text[start + 6 : end][:64]


class UnifonicOptions(_Options):
    """``SMS_OPTIONS`` of ``UnifonicSmsBackend``."""

    APP_SID: SecretStr
    SENDER_ID: str = Field(min_length=1)
    URL: str = "https://el.cloud.unifonic.com"


class UnifonicSmsBackend(ProviderSmsBackend):
    """Unifonic's SMS API."""

    name = "Unifonic"
    Options = UnifonicOptions
    options: UnifonicOptions
    lasting = frozenset(
        {
            *("ER-02", "ER-03", "ER-04", "ER-05", "ER-07", "ER-13", "ER-14"),
            *("410", "412", "481", "482"),
        }
    )
    """A number it does not take: malformed, too long or short, unroutable
    (``ER-02`` to ``ER-05``, statuses ``410``, ``481``, ``482``); a text
    filtered (``ER-07``) or too long (``412``); a country the account may
    not send to (``ER-13``); a number on the blacklist (``ER-14``)."""

    async def send(self, message: SmsMessage) -> None:
        """POST the message; ``success`` must be true."""
        options = self.options
        response = await self.client.post(
            f"{options.URL}/rest/SMS/messages",
            data={
                "AppSid": options.APP_SID.get_secret_value(),
                "SenderID": message.sender or options.SENDER_ID,
                "Recipient": message.to.removeprefix("+"),
                "Body": message.body,
                "responseType": "JSON",
                "CorrelationID": str(message.message_id or ""),
            },
        )
        body = json_of(response) or {}
        if response.status_code != 200 or str(body.get("success")).lower() != "true":
            raise self.refused(body.get("errorCode") or response.status_code)


class HuaweiSmsOptions(_Options):
    """``SMS_OPTIONS`` of ``HuaweiSmsBackend``."""

    URL: str = Field(min_length=1)
    """The region's endpoint: ``https://smsapi.ap-southeast-1.myhuaweicloud.com:443``."""
    APP_KEY: str = Field(min_length=1)
    APP_SECRET: SecretStr
    SENDER: str = Field(min_length=1)
    """The channel number messages are sent from."""
    TEMPLATE_ID: str = Field(min_length=1)
    """A template whose one variable (``${1}``) is the whole text."""
    SIGNATURE: str | None = None
    """The signature name, where the region asks for one (Chinese mainland)."""


class HuaweiSmsBackend(ProviderSmsBackend):
    """Huawei Cloud's Message & SMS service (``batchSendSms``), WSSE-signed."""

    name = "Huawei Cloud SMS"
    Options = HuaweiSmsOptions
    options: HuaweiSmsOptions
    lasting = frozenset({"E000503", "E200028"})
    """A parameter it refuses, such as the number (``E000503``), and a text
    the template's variable cannot take (``E200028``)."""

    async def send(self, message: SmsMessage) -> None:
        """POST the message as the template's one variable; code ``000000``."""
        options = self.options
        form = {
            "from": message.sender or options.SENDER,
            "to": message.to,
            "templateId": options.TEMPLATE_ID,
            "templateParas": json.dumps([message.body], ensure_ascii=False),
        }
        if options.SIGNATURE:
            form["signature"] = options.SIGNATURE
        response = await self.client.post(
            f"{options.URL.rstrip('/')}/sms/batchSendSms/v1",
            data=form,
            headers={
                "Authorization": WSSE_REALM,
                "X-WSSE": wsse(options.APP_KEY, options.APP_SECRET.get_secret_value()),
            },
        )
        body = json_of(response) or {}
        if response.status_code != 200 or body.get("code") != "000000":
            raise self.refused(body.get("code") or response.status_code)


def wsse(key: str, secret: str) -> str:
    """Huawei's ``X-WSSE`` header: a digest of a nonce, the time, and the secret."""
    nonce = secrets.token_hex(16)
    created = clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha256(f"{nonce}{created}{secret}".encode()).digest()
    ).decode()
    return (
        f'UsernameToken Username="{key}",PasswordDigest="{digest}",'
        f'Nonce="{nonce}",Created="{created}"'
    )

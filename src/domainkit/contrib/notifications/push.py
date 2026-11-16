"""Push providers: Firebase Cloud Messaging, Apple's APNs, Huawei Push Kit.

Name one in ``NOTIFICATIONS``, or one per platform, with each one's
settings in ``PUSH_OPTIONS`` under its ``name``::

    NOTIFICATIONS = {
        "PUSH_BACKEND": "domainkit.contrib.notifications.push.FcmPushBackend",
        "PUSH_BACKENDS": {
            "apns": "domainkit.contrib.notifications.push.ApnsPushBackend",
            "huawei": "domainkit.contrib.notifications.push.HuaweiPushBackend",
        },
        "PUSH_OPTIONS": {
            "fcm": {"PROJECT_ID": "acme-shop", "CREDENTIALS_FILE": "/run/fcm.json"},
            "apns": {"TEAM_ID": "...", "KEY_ID": "...", "PRIVATE_KEY": env.str(...),
                     "TOPIC": "com.acme.shop"},
            "huawei": {"APP_ID": "...", "APP_SECRET": env.str(...)},
        },
    }

Each calls its service's HTTP API through ``domainkit.http.client`` (the
``push`` extra): FCM's HTTP v1 with a Google service account's token (or
the metadata server's), APNs over HTTP/2 with a provider token (ES256)
kept 50 minutes, Push Kit with its app's OAuth token.

Each backend reads a refusal from its service's answer (``refusal``,
which a subclass may read differently without copying ``send``). A token
the service does not know raises ``PushTokenGone``: the worker drops the
notification and publishes ``PushTokenExpired``. A message the service
would refuse again, as it is, raises ``NotificationRefused``, which is
not tried again. Any other refusal raises ``NotificationFailed`` with
the service's code and the fields it named, and the worker tries again:
credentials, permissions, quotas, and outages may change meanwhile.

``check_token`` asks a service about a token without delivering anything,
where it can: FCM's and Push Kit's dry run (``validate_only``). APNs has
none: only the token's form is checked.

FCM answers ``INVALID_ARGUMENT``, naming no field, both to a token it
never made and to a message it cannot take ("Android message is too
big"); its guidance takes the token for invalid only when the message is
valid. So ``FcmPushBackend.send`` asks a dry run of the token alone
before it forgets one: a token the dry run refuses is gone, and the
message refused for good otherwise (ADR 0169).
"""

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domainkit.cloud import TokenSource, oauth_token

from .backends import (
    NotificationFailed,
    NotificationRefused,
    PushBackend,
    PushTokenGone,
)
from .messages import PushMessage
from .settings import NotificationsSettings
from .sms import TIMEOUT, json_of, reached, shared_client

if TYPE_CHECKING:
    import httpx2

__all__ = ["ApnsPushBackend", "FcmPushBackend", "HuaweiPushBackend", "Refusal"]

FCM_SCOPE: Final = "https://www.googleapis.com/auth/firebase.messaging"
APNS_TOKEN_LIFETIME: Final = 50 * 60
"""Seconds a provider token is used: Apple takes them for an hour."""
APNS_GONE: Final = frozenset(
    {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
)
APNS_LASTING: Final = frozenset({400, 404, 405, 413})
"""APNs' statuses for a request it will refuse again: its headers, path, or size."""
FCM_LASTING: Final = frozenset({"INVALID_ARGUMENT", "SENDER_ID_MISMATCH"})
"""FCM's codes for a message it will refuse again (but a token it does not know)."""
FCM_TOKEN_FIELD: Final = "message.token"  # noqa: S105 - a field's name.
"""The field FCM names when the token it refuses is the only thing wrong."""
HUAWEI_GONE: Final = "80300007"
"""Push Kit's code for tokens it does not know."""
HUAWEI_SENT: Final = "80000000"
"""Push Kit's code for a message it took."""
HUAWEI_LASTING: Final = frozenset({"80100003", "80300008"})
"""Push Kit's codes for a message it cannot take: its structure, or its size."""
_HEX_TOKEN: Final = re.compile(r"[0-9a-fA-F]{32,200}")


@dataclass(frozen=True, slots=True)
class Refusal:
    """How a push service refused a notification, read from its answer."""

    code: str
    """The service's error code (``UNREGISTERED``, ``BadDeviceToken``), or status."""
    gone: bool = False
    """It does not know the token: the notification is dropped, the token forgotten."""
    lasting: bool = False
    """It would refuse the message again, as it is: it is not tried again."""
    fields: tuple[str, ...] = ()
    """The fields of the message it named, never their values."""


class _Options(BaseModel):
    """What every service's options share."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    TIMEOUT: float = Field(default=TIMEOUT, gt=0, le=60)


class ProviderPushBackend(PushBackend):
    """A push service's HTTP API: ``PUSH_OPTIONS[name]`` read into ``options``."""

    name: ClassVar[str]
    """The key of its options in ``PUSH_OPTIONS``."""
    title: ClassVar[str]
    """The service, as errors name it."""
    Options: ClassVar[type[_Options]]
    http2: ClassVar[bool] = False

    def __init__(self, options: Any = None) -> None:  # noqa: ANN401 - the service's Options.
        """Read ``options``, or ``NOTIFICATIONS.PUSH_OPTIONS[name]``."""
        if options is None:
            from domainkit.apps import registry

            config = registry.settings(NotificationsSettings)
            options = config.PUSH_OPTIONS.get(self.name, {})
        self.options = (
            options
            if isinstance(options, self.Options)
            else self.Options.model_validate(options)
        )
        self.client = shared_client(self.options.TIMEOUT, http2=self.http2)

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Send each notification; return how many were sent.

        Raises:
            PushTokenGone: The service no longer knows a token.
            NotificationFailed: It refused one, or could not be reached.
        """
        for message in messages:
            async with reached(self.title):
                await self.send(message)
        return len(messages)

    async def send(self, message: PushMessage) -> None:
        """Send one notification."""
        raise NotImplementedError

    def refused(self, reason: object) -> NotificationFailed:
        """``NotificationFailed`` naming the service's reason."""
        return NotificationFailed(f"{self.title} refused the notification: {reason}.")

    def refusal(self, response: "httpx2.Response") -> Refusal:
        """Read the service's refusal from its answer to ``send``.

        Each service reads its own; a subclass reads one case differently
        by overriding this, and calling it for the others.
        """
        return Refusal(str(response.status_code))

    def failure(self, message: PushMessage, refusal: Refusal) -> NotificationFailed:
        """What ``send`` raises for ``refusal``.

        ``PushTokenGone`` for a token the service does not know,
        ``NotificationRefused`` for a lasting refusal, and otherwise
        ``NotificationFailed``, which the worker tries again.
        """
        if refusal.gone:
            return PushTokenGone(message.token, self.title)
        text = f"{self.title} refused the notification: {refusal.code}"
        if refusal.fields:
            text += f" ({', '.join(refusal.fields)})"
        kind = NotificationRefused if refusal.lasting else NotificationFailed
        return kind(f"{text}.", code=refusal.code, fields=refusal.fields)

    def unchecked(self, token: str, refusal: Refusal) -> NotificationFailed:
        """What ``check_token`` raises for ``refusal``.

        ``PushTokenGone`` for a token the service does not know; otherwise
        ``NotificationFailed``: the service could not tell.
        """
        if refusal.gone:
            return PushTokenGone(token, self.title)
        return NotificationFailed(
            f"{self.title} could not check the token: {refusal.code}.",
            code=refusal.code,
            fields=refusal.fields,
        )


class FcmOptions(_Options):
    """``PUSH_OPTIONS["fcm"]``: the Firebase project, and who calls it."""

    PROJECT_ID: str = Field(min_length=1, pattern=r"^[a-z0-9-]+$")
    CREDENTIALS_FILE: str | None = None
    """A service account's key file; None: the metadata server's account."""
    URL: str = "https://fcm.googleapis.com"


_google_accounts: dict[str | None, Any] = {}


class FcmPushBackend(ProviderPushBackend):
    """Firebase Cloud Messaging's HTTP v1 API (Android, iOS, the web)."""

    name = "fcm"
    title = "FCM"
    Options = FcmOptions
    options: FcmOptions

    async def send(self, message: PushMessage) -> None:
        """POST the message to the project; a refusal raises (``refusal``).

        An ``INVALID_ARGUMENT`` that names no field may be the token or the
        message: a dry run of the token alone (``check_token``) tells.
        The token is gone if FCM refuses it alone; else the message is
        refused for good, and the token kept. A dry run that cannot tell
        raises ``NotificationFailed``: the worker tries again.
        """
        payload: dict[str, Any] = {
            "token": message.token,
            "notification": {"title": message.title, "body": message.body},
        }
        if message.data:
            payload["data"] = message.data
        response = await self.post({"message": payload})
        if response.status_code == 200:
            return
        refusal = self.refusal(response)
        if _either(refusal):
            await self.check_token(message.token)
        raise self.failure(message, refusal)

    async def check_token(self, token: str) -> None:
        """A dry run (``validate_only``) of a message to ``token``: nothing is sent.

        FCM checks the message, its token included, as it would send it,
        and answers as ``send`` reads; the message is the token alone, so
        an ``INVALID_ARGUMENT`` naming no other field is the token.
        """
        async with reached(self.title):
            response = await self.post(
                {"validate_only": True, "message": {"token": token}}
            )
        if response.status_code == 200:
            return
        refusal = self.refusal(response)
        if refusal.code == "INVALID_ARGUMENT" and set(refusal.fields) <= {
            FCM_TOKEN_FIELD
        }:
            refusal = Refusal(refusal.code, gone=True, fields=refusal.fields)
        raise self.unchecked(token, refusal)

    async def post(self, body: dict[str, Any]) -> "httpx2.Response":
        """POST ``body`` to the project's ``messages:send``, as its account."""
        options = self.options
        token = await _google_account(options.CREDENTIALS_FILE).token(FCM_SCOPE)
        return await self.client.post(
            f"{options.URL}/v1/projects/{options.PROJECT_ID}/messages:send",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    def refusal(self, response: "httpx2.Response") -> Refusal:
        """FCM's error code and the fields it named, and whether the token is gone.

        As FCM's guidance has it: ``UNREGISTERED`` is a token gone, and so is
        ``INVALID_ARGUMENT`` with a field violation of ``message.token``
        alone. ``INVALID_ARGUMENT`` naming other fields is a message it
        cannot take, and ``SENDER_ID_MISMATCH`` a token of another sender:
        both lasting. ``INVALID_ARGUMENT`` naming no field is lasting here
        too, though it may be the token: ``send`` asks a dry run before it
        decides. A ``404`` without ``UNREGISTERED`` is no token gone: a wrong
        ``PROJECT_ID`` would forget every token. An answer of another shape
        is read as far as it goes, never raising.
        """
        error = _object(_object(json_of(response)).get("error"))
        details = [each for each in _items(error.get("details")) if _object(each)]
        codes = sorted(
            {str(each["errorCode"]) for each in details if each.get("errorCode")}
        )
        fields = tuple(
            str(violation["field"])
            for each in details
            for violation in _items(each.get("fieldViolations"))
            if _object(violation).get("field")
        )
        status = error.get("status")
        code = (codes or [str(status or response.status_code)])[0]
        if "UNREGISTERED" in codes:
            return Refusal("UNREGISTERED", gone=True)
        if code == "INVALID_ARGUMENT" and fields == (FCM_TOKEN_FIELD,):
            return Refusal(code, gone=True, fields=fields)
        lasting = code in FCM_LASTING or response.status_code == 400
        return Refusal(code, lasting=lasting, fields=fields)


def _either(refusal: Refusal) -> bool:
    """Whether FCM's ``refusal`` may be the token as well as the message."""
    return refusal.code == "INVALID_ARGUMENT" and not (refusal.fields or refusal.gone)


def _object(value: object) -> dict[str, Any]:
    """``value`` if a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[Any]:
    """``value`` if a JSON array, else an empty one."""
    return value if isinstance(value, list) else []


def _google_account(credentials_file: str | None) -> TokenSource:
    """The account of ``credentials_file`` (or the metadata server), kept."""
    if credentials_file not in _google_accounts:
        from domainkit.cloud.google import MetadataAccount, ServiceAccount

        _google_accounts[credentials_file] = (
            MetadataAccount()
            if credentials_file is None
            else ServiceAccount.from_file(credentials_file)
        )
    account: TokenSource = _google_accounts[credentials_file]
    return account


class ApnsOptions(_Options):
    """``PUSH_OPTIONS["apns"]``: the team's key, and the app's bundle id."""

    TEAM_ID: str = Field(min_length=1)
    KEY_ID: str = Field(min_length=1)
    PRIVATE_KEY: SecretStr
    """The ``.p8`` key (PEM, ES256) that signs provider tokens."""
    TOPIC: str = Field(min_length=1)
    """The app's bundle id."""
    SANDBOX: bool = False
    """Apple's development service, for builds signed for development."""


_apns_tokens: dict[tuple[str, str], tuple[str, float]] = {}


class ApnsPushBackend(ProviderPushBackend):
    """Apple's push service (APNs), over HTTP/2, with a provider token."""

    name = "apns"
    title = "APNs"
    Options = ApnsOptions
    options: ApnsOptions
    http2 = True

    async def send(self, message: PushMessage) -> None:
        """POST the alert to the device; Apple's reasons for a lost token are gone."""
        options = self.options
        if not _HEX_TOKEN.fullmatch(message.token):
            raise PushTokenGone(message.token, self.title)
        host = (
            "https://api.sandbox.push.apple.com"
            if options.SANDBOX
            else "https://api.push.apple.com"
        )
        headers = {
            "authorization": f"bearer {self.provider_token()}",
            "apns-topic": options.TOPIC,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        if message.message_id is not None:
            headers["apns-id"] = str(message.message_id).upper()
        response = await self.client.post(
            f"{host}/3/device/{message.token}",
            json={
                "aps": {"alert": {"title": message.title, "body": message.body}},
                **message.data,
            },
            headers=headers,
        )
        if response.status_code == 200:
            return
        raise self.failure(message, self.refusal(response))

    async def check_token(self, token: str) -> None:
        """The token's form alone: APNs checks no token without delivering to it.

        A token that is not hex is gone, as ``send`` reads it; any other
        is taken, and the first notification tells.
        """
        if not _HEX_TOKEN.fullmatch(token):
            raise PushTokenGone(token, self.title)

    def refusal(self, response: "httpx2.Response") -> Refusal:
        """Apple's reason; ``410`` and its reasons for a lost token are gone.

        A request it will refuse again (``400``: a header or the payload;
        ``404``: the path; ``405``; ``413``: too large) is lasting.
        """
        status = response.status_code
        reason = str((json_of(response) or {}).get("reason") or status)
        return Refusal(
            reason,
            gone=status == 410 or reason in APNS_GONE,
            lasting=status in APNS_LASTING,
        )

    def provider_token(self) -> str:
        """The team's provider token (ES256), made again every 50 minutes."""
        import jwt

        options = self.options
        key = (options.TEAM_ID, options.KEY_ID)
        held = _apns_tokens.get(key)
        now = time.time()
        if held is not None and held[1] > now:
            return held[0]
        token = jwt.encode(
            {"iss": options.TEAM_ID, "iat": int(now)},
            options.PRIVATE_KEY.get_secret_value(),
            algorithm="ES256",
            headers={"kid": options.KEY_ID},
        )
        _apns_tokens[key] = (token, now + APNS_TOKEN_LIFETIME)
        return token


class HuaweiPushOptions(_Options):
    """``PUSH_OPTIONS["huawei"]``: the app's id and secret (AppGallery Connect)."""

    APP_ID: str = Field(min_length=1, pattern=r"^[0-9]+$")
    APP_SECRET: SecretStr
    TOKEN_URL: str = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"  # noqa: S105
    URL: str = "https://push-api.cloud.huawei.com"


class HuaweiApp(TokenSource):
    """A Push Kit app's OAuth tokens (client credentials)."""

    def __init__(self, options: HuaweiPushOptions) -> None:
        """Ask for tokens as ``options``' app."""
        super().__init__()
        self.options = options

    async def _fetch(self, scope: str) -> tuple[str, float]:
        """A new token for the app (``scope`` is unused)."""
        options = self.options
        response = await shared_client(options.TIMEOUT).post(
            options.TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": options.APP_ID,
                "client_secret": options.APP_SECRET.get_secret_value(),
            },
        )
        return oauth_token(response, "Huawei's OAuth service")


_huawei_apps: dict[str, HuaweiApp] = {}


class HuaweiPushBackend(ProviderPushBackend):
    """Huawei Push Kit's HTTP API, for devices without Google's services."""

    name = "huawei"
    title = "Push Kit"
    Options = HuaweiPushOptions
    options: HuaweiPushOptions

    async def send(self, message: PushMessage) -> None:
        """POST the message; code ``80000000`` is sent, ``80300007`` gone."""
        payload: dict[str, Any] = {
            "notification": {"title": message.title, "body": message.body},
            "android": {"notification": {"click_action": {"type": 3}}},
            "token": [message.token],
        }
        if message.data:
            payload["data"] = json.dumps(message.data)
        response = await self.post({"validate_only": False, "message": payload})
        if _sent(response):
            return
        raise self.failure(message, self.refusal(response))

    async def check_token(self, token: str) -> None:
        """A test message (``validate_only``) to ``token``: nothing is sent.

        Push Kit checks the message's format; a token it does not know
        answers ``80300007``, as ``send`` reads it.
        """
        async with reached(self.title):
            response = await self.post(
                {"validate_only": True, "message": {"data": "{}", "token": [token]}}
            )
        if not _sent(response):
            raise self.unchecked(token, self.refusal(response))

    async def post(self, body: dict[str, Any]) -> "httpx2.Response":
        """POST ``body`` to the app's ``messages:send``, with its OAuth token."""
        options = self.options
        app = _huawei_apps.setdefault(options.APP_ID, HuaweiApp(options))
        return await self.client.post(
            f"{options.URL}/v1/{options.APP_ID}/messages:send",
            json=body,
            headers={"Authorization": f"Bearer {await app.token('push')}"},
        )

    def refusal(self, response: "httpx2.Response") -> Refusal:
        """Push Kit's code: ``80300007`` is gone; a message it cannot take, lasting."""
        code = str((json_of(response) or {}).get("code") or response.status_code)
        return Refusal(
            code,
            gone=code == HUAWEI_GONE,
            lasting=code in HUAWEI_LASTING or response.status_code == 400,
        )


def _sent(response: "httpx2.Response") -> bool:
    """Whether Push Kit took the message: ``200`` with its code ``80000000``."""
    code = (json_of(response) or {}).get("code")
    return response.status_code == 200 and code == HUAWEI_SENT

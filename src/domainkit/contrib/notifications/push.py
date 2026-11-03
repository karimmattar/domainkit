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
kept 50 minutes, Push Kit with its app's OAuth token. A token the
service no longer knows raises ``PushTokenGone``: the worker drops the
notification and publishes ``PushTokenExpired``. Any other refusal
raises ``NotificationFailed`` with the service's reason, and the worker
tries again.
"""

import json
import re
import time
from collections.abc import Sequence
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domainkit.cloud import TokenSource, oauth_token

from .backends import NotificationFailed, PushBackend, PushTokenGone
from .messages import PushMessage
from .settings import NotificationsSettings
from .sms import TIMEOUT, _json, reached, shared_client

__all__ = ["ApnsPushBackend", "FcmPushBackend", "HuaweiPushBackend"]

FCM_SCOPE: Final = "https://www.googleapis.com/auth/firebase.messaging"
APNS_TOKEN_LIFETIME: Final = 50 * 60
"""Seconds a provider token is used: Apple takes them for an hour."""
APNS_GONE: Final = frozenset(
    {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
)
HUAWEI_GONE: Final = "80300007"
"""Push Kit's code for tokens it does not know."""
_HEX_TOKEN: Final = re.compile(r"[0-9a-fA-F]{32,200}")


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
        """POST the message to the project; a 404 or ``UNREGISTERED`` is gone."""
        options = self.options
        token = await _google_account(options.CREDENTIALS_FILE).token(FCM_SCOPE)
        payload: dict[str, Any] = {
            "token": message.token,
            "notification": {"title": message.title, "body": message.body},
        }
        if message.data:
            payload["data"] = message.data
        response = await self.client.post(
            f"{options.URL}/v1/projects/{options.PROJECT_ID}/messages:send",
            json={"message": payload},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 200:
            return
        error = (_json(response) or {}).get("error") or {}
        codes = {
            detail.get("errorCode")
            for detail in error.get("details") or []
            if isinstance(detail, dict)
        }
        if response.status_code == 404 or "UNREGISTERED" in codes:
            raise PushTokenGone(message.token, self.title)
        raise self.refused(error.get("status") or response.status_code)


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
        reason = (_json(response) or {}).get("reason")
        if response.status_code == 410 or reason in APNS_GONE:
            raise PushTokenGone(message.token, self.title)
        raise self.refused(reason or response.status_code)

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
        options = self.options
        app = _huawei_apps.setdefault(options.APP_ID, HuaweiApp(options))
        payload: dict[str, Any] = {
            "notification": {"title": message.title, "body": message.body},
            "android": {"notification": {"click_action": {"type": 3}}},
            "token": [message.token],
        }
        if message.data:
            payload["data"] = json.dumps(message.data)
        response = await self.client.post(
            f"{options.URL}/v1/{options.APP_ID}/messages:send",
            json={"validate_only": False, "message": payload},
            headers={"Authorization": f"Bearer {await app.token('push')}"},
        )
        code = (_json(response) or {}).get("code")
        if response.status_code == 200 and code == "80000000":
            return
        if code == HUAWEI_GONE:
            raise PushTokenGone(message.token, self.title)
        raise self.refused(code or response.status_code)

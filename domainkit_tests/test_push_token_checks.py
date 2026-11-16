"""A push token checked when it is registered (user report N2).

``POST /auth/devices/push-tokens`` stored any string under the platform
named, and the first push found out: a token FCM never made failed every
notification to its user until the worker forgot it. With
``DEVICES.PUSH_TOKEN_CHECK`` the token's service is asked first,
delivering nothing (FCM's and Push Kit's dry run, APNs' token form), and
a token it does not know is refused. A service that fails, or is slow,
lets the token in: a registration never waits on an outage.

The services' answers are those of their references, given by a stub.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator, Sequence
from typing import Any, ClassVar

import httpx2
import pytest

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.notifications import (
    MemoryPushBackend,
    NotificationFailed,
    NotificationRefused,
    PushBackend,
    PushMessage,
    check_push_token,
    push_outbox,
)
from domainkit.contrib.notifications import push as push_module
from domainkit.contrib.notifications.backends import PushTokenGone
from domainkit.contrib.notifications.push import (
    ApnsPushBackend,
    FcmPushBackend,
    HuaweiPushBackend,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit_tests.test_devices import (  # noqa: F401 - the fixture.
    App,
    app,
    signed_in,
)
from domainkit_tests.test_notification_providers import (  # noqa: F401 - fixture.
    APNS,
    Recorder,
    provider,
)

NOT_FCMS = {
    "error": {
        "code": 400,
        "message": "The registration token is not a valid FCM registration token",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": "INVALID_ARGUMENT",
            }
        ],
    }
}
UNREGISTERED = {
    "error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}
}
CHECKED = "domainkit_tests.test_push_token_checks.Service"


@pytest.fixture(autouse=True)
def fresh_accounts() -> Iterator[None]:
    """Forget the provider tokens a test's backends kept."""
    yield
    push_module._google_accounts.clear()
    push_module._huawei_apps.clear()
    push_module._apns_tokens.clear()


def metadata_token() -> httpx2.Response:
    """The metadata server's access token for FCM."""
    return httpx2.Response(200, json={"access_token": "ya29", "expires_in": 3600})


@pytest.mark.anyio
async def test_fcm_checks_a_token_with_a_dry_run(provider: Any) -> None:  # noqa: ANN401, F811
    """``validate_only``: FCM answers as it would to a send; nothing is sent."""
    calls: Recorder = provider(
        metadata_token(),
        httpx2.Response(200, json={"name": "projects/acme/messages/fake"}),
        httpx2.Response(400, json=NOT_FCMS),
        httpx2.Response(404, json=UNREGISTERED),
        httpx2.Response(503, json={"error": {"status": "UNAVAILABLE"}}),
    )
    fcm = FcmPushBackend({"PROJECT_ID": "acme"})

    await fcm.check_token("a-real-token")
    for _ in range(2):
        with pytest.raises(PushTokenGone):
            await fcm.check_token("made-up")
    with pytest.raises(NotificationFailed) as failed:
        await fcm.check_token("a-real-token")

    assert type(failed.value) is NotificationFailed
    assert str(failed.value) == "FCM could not check the token: UNAVAILABLE."
    assert calls.json(1) == {
        "validate_only": True,
        "message": {"token": "a-real-token"},
    }
    assert calls.requests[1].url.path == "/v1/projects/acme/messages:send"


TOO_BIG = {
    "error": {
        "code": 400,
        "message": "Android message is too big",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": "INVALID_ARGUMENT",
            }
        ],
    }
}
"""FCM's answer to a message over its size: the same shape as a token it never made."""
PUSHED = PushMessage(token="a-real-token", title="Contract", body="x" * 4000)


@pytest.mark.anyio
async def test_fcm_keeps_a_token_whose_message_was_too_big(provider: Any) -> None:  # noqa: ANN401, F811
    """A message too big is refused for good; the token, which a dry run takes, kept.

    FCM answers it as it answers a token it never made: ``INVALID_ARGUMENT``,
    its own error, no field. 2.4 forgot the token, so one large message
    lost a working phone (user report N1).
    """
    calls: Recorder = provider(
        metadata_token(),
        httpx2.Response(400, json=TOO_BIG),
        httpx2.Response(200, json={"name": "projects/acme/messages/fake"}),
    )
    fcm = FcmPushBackend({"PROJECT_ID": "acme"})

    with pytest.raises(NotificationFailed) as refused:
        await fcm.send_messages([PUSHED])

    assert type(refused.value) is NotificationRefused
    assert str(refused.value) == "FCM refused the notification: INVALID_ARGUMENT."
    assert calls.json(2) == {
        "validate_only": True,
        "message": {"token": "a-real-token"},
    }


@pytest.mark.anyio
async def test_fcm_forgets_a_token_the_dry_run_refuses_too(provider: Any) -> None:  # noqa: ANN401, F811
    """The same answer, and the token alone refused too: the token is gone.

    A dry run that cannot tell (FCM unavailable) forgets nothing: the
    worker tries the message again.
    """
    provider(
        metadata_token(),
        httpx2.Response(400, json=NOT_FCMS),
        httpx2.Response(400, json=NOT_FCMS),
        httpx2.Response(400, json=TOO_BIG),
        httpx2.Response(503, json={"error": {"status": "UNAVAILABLE"}}),
    )
    fcm = FcmPushBackend({"PROJECT_ID": "acme"})

    with pytest.raises(PushTokenGone):
        await fcm.send_messages([PUSHED])
    with pytest.raises(NotificationFailed) as failed:
        await fcm.send_messages([PUSHED])

    assert type(failed.value) is NotificationFailed
    assert str(failed.value) == "FCM could not check the token: UNAVAILABLE."


@pytest.mark.anyio
async def test_push_kit_checks_a_token_with_a_test_message(provider: Any) -> None:  # noqa: ANN401, F811
    """``validate_only``: ``80300007`` is a token it does not know."""
    calls: Recorder = provider(
        httpx2.Response(200, json={"access_token": "hw", "expires_in": 3600}),
        httpx2.Response(200, json={"code": "80000000"}),
        httpx2.Response(200, json={"code": "80300007"}),
        httpx2.Response(400, json={"code": "80100003"}),
    )
    huawei = HuaweiPushBackend({"APP_ID": "1234", "APP_SECRET": "s"})

    await huawei.check_token("token")
    with pytest.raises(PushTokenGone):
        await huawei.check_token("made-up")
    with pytest.raises(NotificationFailed) as failed:
        await huawei.check_token("token")

    assert type(failed.value) is NotificationFailed
    assert failed.value.code == "80100003"
    sent = calls.json(1)
    assert (sent["validate_only"], sent["message"]["token"]) == (True, ["token"])


@pytest.mark.anyio
async def test_apns_checks_a_tokens_form_alone(provider: Any) -> None:  # noqa: ANN401, F811
    """Apple checks no token without delivering: hex passes, anything else is gone."""
    calls: Recorder = provider(httpx2.Response(500))
    apns = ApnsPushBackend(APNS)

    await apns.check_token("a" * 64)
    with pytest.raises(PushTokenGone):
        await apns.check_token("fcm:not-apples")

    assert calls.requests == []


@pytest.mark.anyio
async def test_a_backend_that_cannot_ask_takes_every_token() -> None:
    """The port's default: nothing to ask, nothing refused, nothing sent."""
    await MemoryPushBackend().check_token("anything")

    assert push_outbox == []


class Service(PushBackend):
    """A push service whose answer to a check the test sets."""

    answer: ClassVar[str] = "known"
    asked: ClassVar[list[str]] = []

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Take them."""
        return len(messages)

    async def check_token(self, token: str) -> None:
        """Answer as ``answer`` says."""
        Service.asked.append(token)
        if Service.answer == "gone":
            raise PushTokenGone(token, "Service")
        if Service.answer == "down":
            raise NotificationFailed("Service could not be reached (ConnectError).")
        if Service.answer == "slow":
            await asyncio.sleep(10)


@pytest.fixture
def service() -> Iterator[type[Service]]:
    """Every platform's push backend is ``Service``, which knows every token."""
    Service.answer, Service.asked = "known", []
    platforms = dict.fromkeys(("fcm", "apns", "huawei"), CHECKED)
    with override_settings(
        NOTIFICATIONS={"PUSH_BACKEND": CHECKED, "PUSH_BACKENDS": platforms}
    ):
        yield Service


type Register = Callable[[str], Any]


def registering(client: Any, headers: dict[str, str]) -> Register:  # noqa: ANN401
    """``register(token)``: the answer to registering ``token`` for FCM."""

    def register(token: str) -> Any:  # noqa: ANN401 - a response.
        return client.post(
            "/auth/devices/push-tokens",
            json={"token": token, "platform": "fcm"},
            headers=headers,
        )

    return register


def test_a_token_its_service_does_not_know_is_refused(
    app: App,  # noqa: F811
    service: type[Service],
) -> None:
    """``422 push_token_refused``, and nothing kept; a known token is kept.

    Without the check, the made-up token was kept, and every notification
    to its user failed until the worker heard so from FCM.
    """
    client = app(PUSH_TOKEN_CHECK={"ENABLED": True})
    headers = signed_in(client)
    register = registering(client, headers)

    service.answer = "gone"
    refused = register("made-up")
    service.answer = "known"
    kept = register("real")

    assert refused.status_code == 422, refused.json()
    assert refused.json()["code"] == "push_token_refused"
    assert "made-up" not in json.dumps(refused.json())
    assert kept.status_code == 201
    listed = client.get("/auth/devices/push-tokens", headers=headers).json()
    assert [each["id"] for each in listed] == [kept.json()["id"]]
    assert service.asked == ["made-up", "real"]


@pytest.mark.parametrize("answer", ["down", "slow"])
def test_a_service_that_fails_or_is_slow_lets_the_token_in(
    app: App,  # noqa: F811
    service: type[Service],
    caplog: pytest.LogCaptureFixture,
    answer: str,
) -> None:
    """Fail open, within ``TIMEOUT``, with a warning that names no token."""
    client = app(PUSH_TOKEN_CHECK={"ENABLED": True, "TIMEOUT": 0.05})
    register = registering(client, signed_in(client))
    service.answer = answer

    with caplog.at_level(logging.WARNING, "domainkit.contrib.notifications"):
        kept = register("a-token")

    assert kept.status_code == 201, kept.json()
    (record,) = [
        each
        for each in caplog.records
        if each.name == "domainkit.contrib.notifications.delivery"
    ]
    assert record.getMessage().startswith("A push service ")
    assert "registration" in record.getMessage()
    assert "a-token" not in caplog.text


def test_without_the_setting_no_service_is_asked(
    app: App,  # noqa: F811
    service: type[Service],
) -> None:
    """Off by default: the token is kept as given, as before."""
    client = app()
    service.answer = "gone"

    assert registering(client, signed_in(client))("made-up").status_code == 201
    assert service.asked == []


def test_check_push_token_answers_false_only_for_a_token_gone(
    app: App,  # noqa: F811
    service: type[Service],
) -> None:
    """Gone: False. Unreachable, slow, or unable to ask: True."""
    app()
    answers = {}
    for answer in ("known", "gone", "down", "slow"):
        service.answer = answer
        answers[answer] = run_async(check_push_token("t", "fcm", within=0.01))

    assert answers == {"known": True, "gone": False, "down": True, "slow": True}


def test_the_check_needs_notifications(
    app: App,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PUSH_TOKEN_CHECK`` without notifications refuses to start."""
    app(PUSH_TOKEN_CHECK={"ENABLED": True})
    config = registry.get_domain_config("devices")
    installed = registry.is_installed
    monkeypatch.setattr(
        registry,
        "is_installed",
        lambda name: name != "domainkit.contrib.notifications" and installed(name),
    )

    with pytest.raises(
        ImproperlyConfigured,
        match=r"PUSH_TOKEN_CHECK needs 'domainkit\.contrib\.notifications'",
    ):
        config.ready()


def test_the_checks_timeout_is_bounded() -> None:
    """A person's app waits for it: at most 10 seconds."""
    from pydantic import ValidationError

    from domainkit.contrib.devices.settings import DevicesSettings

    assert DevicesSettings().PUSH_TOKEN_CHECK.ENABLED is False
    with pytest.raises(ValidationError):
        DevicesSettings.model_validate({"PUSH_TOKEN_CHECK": {"TIMEOUT": 30}})

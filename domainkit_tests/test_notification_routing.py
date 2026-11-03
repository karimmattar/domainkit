"""Push notifications by platform, tokens a service forgot, options from settings."""

from collections.abc import Sequence
from typing import ClassVar

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.mail.providers import PostmarkBackend, SesBackend
from domainkit.contrib.mail.settings import MailSettings
from domainkit.contrib.notifications import (
    PushBackend,
    PushMessage,
    deliver_push,
    get_push_backend,
    push_outbox,
)
from domainkit.contrib.notifications.backends import PushTokenGone
from domainkit.contrib.notifications.delivery import send_queued_push
from domainkit.contrib.notifications.events import PushTokenExpired, token_digest
from domainkit.contrib.notifications.push import FcmPushBackend
from domainkit.contrib.notifications.sms import TwilioSmsBackend
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_notifications import (  # noqa: F401 - fixtures.
    ADAPTERS,
    DEPLOYED,
    Install,
    empty_outboxes,
    immediate_retries,
    install,
)

pytestmark = pytest.mark.anyio

HERE = "domainkit_tests.test_notification_routing"


class HuaweiDevices(PushBackend):
    """A platform's backend that keeps what it is given, or loses its tokens."""

    sent: ClassVar[list[PushMessage]] = []
    gone: ClassVar[bool] = False

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Keep ``messages``, unless their tokens are gone."""
        if HuaweiDevices.gone:
            raise PushTokenGone(messages[0].token, "Push Kit")
        HuaweiDevices.sent.extend(messages)
        return len(messages)


@pytest.fixture(autouse=True)
def fresh() -> None:
    """Nothing sent, tokens known."""
    HuaweiDevices.sent.clear()
    HuaweiDevices.gone = False


def message(platform: str | None = None) -> PushMessage:
    """A notification to one token of ``platform``."""
    return PushMessage(token="tok", title="Hi", body="", platform=platform)


async def test_each_platform_goes_to_its_backend(install: Install) -> None:  # noqa: F811
    """PUSH_BACKENDS by platform; others, and none, to PUSH_BACKEND."""
    install(PUSH_BACKENDS={"huawei": f"{HERE}.HuaweiDevices"})

    sent = await deliver_push([message("huawei"), message("fcm"), message()])

    assert sent == 3
    assert [m.platform for m in HuaweiDevices.sent] == ["huawei"]
    assert [m.platform for m in push_outbox] == ["fcm", None]
    assert isinstance(get_push_backend("huawei"), HuaweiDevices)


async def test_a_token_its_service_forgot_is_dropped_and_said(install: Install) -> None:  # noqa: F811
    """No retry: PushTokenExpired, with the token's digest only."""
    install(PUSH_BACKENDS={"huawei": f"{HERE}.HuaweiDevices"})
    HuaweiDevices.gone = True
    heard: list[PushTokenExpired] = []

    async def hear(event: PushTokenExpired) -> None:
        heard.append(event)

    event_bus.subscribe(PushTokenExpired, hear)
    try:
        await send_queued_push(message("huawei").prepared())
    finally:
        event_bus.unsubscribe(PushTokenExpired, hear)

    assert [event.token_digest for event in heard] == [token_digest("tok")]


def test_platform_backends_are_checked_where_deployed(install: Install) -> None:  # noqa: F811
    """A memory backend for a platform is refused in production."""
    memory = "domainkit.contrib.notifications.backends.MemoryPushBackend"

    with pytest.raises(ImproperlyConfigured, match=r"PUSH_BACKENDS\['huawei'\]"):
        install(settings=DEPLOYED, **ADAPTERS, PUSH_BACKENDS={"huawei": memory})


def test_provider_backends_read_their_options_from_settings(install: Install) -> None:  # noqa: F811
    """SMS_OPTIONS, PUSH_OPTIONS by name, MAIL.OPTIONS; wrong ones refuse to start."""
    install(
        SMS_BACKEND="domainkit.contrib.notifications.sms.TwilioSmsBackend",
        SMS_OPTIONS={"ACCOUNT_SID": "AC1", "AUTH_TOKEN": "t", "FROM": "+15005550006"},
        PUSH_OPTIONS={"fcm": {"PROJECT_ID": "acme"}},
    )

    assert TwilioSmsBackend().options.ACCOUNT_SID == "AC1"
    assert FcmPushBackend().options.PROJECT_ID == "acme"
    with (
        override_settings(MAIL={"OPTIONS": {"SERVER_TOKEN": "pm"}}),
        pytest.MonkeyPatch.context() as patch,
    ):
        from domainkit.apps import registry

        patch.setattr(
            registry,
            "settings",
            lambda kind: MailSettings(OPTIONS={"SERVER_TOKEN": "pm"}),
        )
        assert PostmarkBackend().options.SERVER_TOKEN.get_secret_value() == "pm"
        assert SesBackend({"REGION": "eu-west-1"}).options.CONFIGURATION_SET is None
    with pytest.raises(ImproperlyConfigured):
        install(
            SMS_BACKEND="domainkit.contrib.notifications.sms.TwilioSmsBackend",
            SMS_OPTIONS={"ACCOUNT_SID": "AC1"},
        )

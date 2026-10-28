"""The delivery backends share one base: mail, text messages, push (DSGN-10)."""

from typing import Any

import pytest

from domainkit.contrib.mail import backends as mail
from domainkit.contrib.notifications import backends as notifications
from domainkit.contrib.notifications.messages import PushMessage, SmsMessage
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.delivery import (
    ConsoleDelivery,
    DeliveryBackend,
    MemoryDelivery,
    load_backend,
)


def test_every_backend_is_a_delivery_backend_under_its_old_name() -> None:
    """The public classes keep their names, paths, and kinds; only the base is new."""
    ports = (mail.BaseEmailBackend, notifications.SmsBackend, notifications.PushBackend)
    memory: tuple[type[DeliveryBackend[Any]], ...] = (
        mail.MemoryBackend,
        notifications.MemorySmsBackend,
        notifications.MemoryPushBackend,
    )
    console: tuple[type[DeliveryBackend[Any]], ...] = (
        mail.ConsoleBackend,
        notifications.ConsoleSmsBackend,
        notifications.ConsolePushBackend,
    )

    assert all(issubclass(port, DeliveryBackend) and port.deployable for port in ports)
    for port, kept, written in zip(ports, memory, console, strict=True):
        assert issubclass(kept, port)
        assert issubclass(kept, MemoryDelivery)
        assert issubclass(written, port)
        assert issubclass(written, ConsoleDelivery)
        assert not kept.deployable
        assert not written.deployable
    assert mail.SmtpBackend.deployable
    assert mail.MemoryBackend.outbox is mail.outbox
    assert notifications.MemorySmsBackend.outbox is notifications.sms_outbox
    assert notifications.MemoryPushBackend.outbox is notifications.push_outbox


@pytest.mark.anyio
async def test_memory_and_console_backends_send_alike(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each keeps or writes every message and counts them."""
    sms = SmsMessage(to="+15550100", body="Your code is 123456")
    push = PushMessage(token="t" * 16, title="Hello", body="There", data={"a": "b"})
    notifications.sms_outbox.clear()

    assert await notifications.MemorySmsBackend().send_messages([sms, sms]) == 2
    assert await notifications.ConsoleSmsBackend().send_messages([sms]) == 1
    assert await notifications.ConsolePushBackend().send_messages([push]) == 1
    assert await mail.ConsoleBackend().send_messages([]) == 0

    assert notifications.sms_outbox == [sms, sms]
    rule = "-" * 79
    assert capsys.readouterr().out == (
        f"SMS to +15550100:\nYour code is 123456\n{rule}\n"
        f"Push: Hello\nThere\n{{'a': 'b'}}\n{rule}\n"
    )
    notifications.sms_outbox.clear()


def test_backends_load_by_path_or_are_refused() -> None:
    """A path that imports nothing, or names another class, is refused by setting."""
    loaded = load_backend(
        "MAIL.BACKEND",
        "domainkit.contrib.mail.backends.MemoryBackend",
        mail.BaseEmailBackend,
    )
    assert isinstance(loaded, mail.MemoryBackend)
    with pytest.raises(ImproperlyConfigured, match=r"^MAIL\.BACKEND: .*nowhere"):
        load_backend("MAIL.BACKEND", "nowhere.Backend", mail.BaseEmailBackend)
    with pytest.raises(
        ImproperlyConfigured,
        match=r"^NOTIFICATIONS\.SMS_BACKEND: 'domainkit.contrib.mail.backends."
        r"MemoryBackend' is not a SmsBackend\.$",
    ):
        load_backend(
            "NOTIFICATIONS.SMS_BACKEND",
            "domainkit.contrib.mail.backends.MemoryBackend",
            notifications.SmsBackend,
        )
    with pytest.raises(ImproperlyConfigured, match="is not a BaseEmailBackend"):
        load_backend(
            "MAIL.BACKEND", "domainkit.contrib.mail.outbox", mail.BaseEmailBackend
        )

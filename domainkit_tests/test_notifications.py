"""Text messages and push notifications through the outbox."""

import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import ExitStack
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib import notifications
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.tables import DEAD, messages
from domainkit.contrib.notifications import (
    NotificationFailed,
    PushBackend,
    PushMessage,
    SmsBackend,
    SmsMessage,
    deliver_push,
    deliver_sms,
    send_push,
    send_sms,
)
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, notes

pytestmark = pytest.mark.anyio

MESSAGING = "domainkit.contrib.messaging"
NOTIFICATIONS = "domainkit.contrib.notifications"
ALPHA = "domainkit_tests.sample_domains.alpha"
BACKENDS = "domainkit.contrib.notifications.backends"
MEMORY = {
    "SMS_BACKEND": f"{BACKENDS}.MemorySmsBackend",
    "PUSH_BACKEND": f"{BACKENDS}.MemoryPushBackend",
}
FAST = {
    "RETRY_BASE_SECONDS": 0.001,
    "RETRY_MAX_SECONDS": 0.001,
    "TASK_TIMEOUT": 5,
    "VISIBILITY_TIMEOUT": 10,
}
PHONE = "+33612345678"

type Install = Callable[..., None]


class GatewaySms(SmsBackend):
    """A provider adapter that fails ``failures`` times first."""

    failures: ClassVar[int] = 0
    sent: ClassVar[list[SmsMessage]] = []

    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Fail, or keep ``messages``."""
        if GatewaySms.failures:
            GatewaySms.failures -= 1
            raise NotificationFailed("gateway answered 503")
        GatewaySms.sent.extend(messages)
        return len(messages)


class PushService(PushBackend):
    """A provider adapter that keeps what it is given."""

    sent: ClassVar[list[PushMessage]] = []

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Keep ``messages``."""
        PushService.sent.extend(messages)
        return len(messages)


class NotABackend:
    """Sends nothing."""


@pytest.fixture(autouse=True)
def immediate_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retries due at once."""
    monkeypatch.setattr(worker_module, "backoff", lambda attempts, config: 0.0)


@pytest.fixture(autouse=True)
def empty_outboxes() -> Iterator[None]:
    """Start and end every test with nothing sent."""
    for sent in (
        notifications.sms_outbox,
        notifications.push_outbox,
        GatewaySms.sent,
        PushService.sent,
    ):
        sent.clear()
    GatewaySms.failures = 0
    yield
    notifications.sms_outbox.clear()
    notifications.push_outbox.clear()


@pytest.fixture
def install(database_url: str) -> Iterator[Install]:
    """``install(**NOTIFICATIONS)``: messaging and notifications."""
    with ExitStack() as stack:

        def install_notifications(
            *,
            settings: dict[str, Any] | None = None,
            **options: Any,  # noqa: ANN401 - NOTIFICATIONS options.
        ) -> None:
            registry.clear()
            values = {
                "INSTALLED_DOMAINS": [MESSAGING, NOTIFICATIONS, ALPHA],
                "DATABASES": {"default": {"URL": database_url}},
                "MESSAGING": FAST,
                "NOTIFICATIONS": {**MEMORY, **options},
                **(settings or {}),
            }
            stack.enter_context(override_settings(**values))
            setup()

        yield install_notifications
    registry.clear()


@pytest.fixture
async def tables(install: Install) -> AsyncIterator[Install]:
    """Install notifications, and create the outbox and notes tables."""
    install()
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
        await connection.run_sync(notes.create)
    yield install
    await connections.close_all()


async def outbox_rows() -> list[dict[str, Any]]:
    """Every outbox row, oldest first."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(messages).order_by(messages.c.created_at)
        )
        return [dict(row._mapping) for row in rows]


async def test_text_messages_go_out_once_the_change_commits(tables: Install) -> None:
    """Stored with the commit, sent by the worker, with an id for the provider."""
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        queued = await send_sms(PHONE, "Your code is 123456", sender="Billing", uow=uow)
        await uow.commit()

    (row,) = await outbox_rows()
    assert (row["target"], row["max_attempts"]) == (
        "domainkit.notifications.send_sms",
        5,
    )
    assert notifications.sms_outbox == []
    assert await Worker().run_once() == 1
    assert notifications.sms_outbox == [queued]
    assert queued.message_id is not None
    assert (queued.to, queued.body, queued.sender) == (
        PHONE,
        "Your code is 123456",
        "Billing",
    )


async def test_notifications_of_a_rolled_back_change_are_never_sent(
    tables: Install,
) -> None:
    """No commit, no text message, no push notification."""
    async with NotesUnitOfWork() as uow:
        await send_sms(PHONE, "Code", uow=uow)
        await send_push("device-token", "Title", "Body", uow=uow)

    assert await outbox_rows() == []
    assert await Worker().run_once() == 0


async def test_push_notifications_without_a_unit_of_work_are_queued_at_once(
    tables: Install,
) -> None:
    """``send_push`` alone stores the task in its own transaction."""
    queued = await send_push(
        "device-token", "Shipped", "On its way", data={"order": "42"}
    )

    (row,) = await outbox_rows()
    assert row["target"] == "domainkit.notifications.send_push"
    await Worker().run_once()
    assert notifications.push_outbox == [queued]
    assert queued.data == {"order": "42"}


async def test_messages_queue_themselves(tables: Install) -> None:
    """``send`` on a message queues it, with or without a unit of work."""
    sms = await SmsMessage(to=PHONE, body="Hi").send()
    async with NotesUnitOfWork() as uow:
        push = await PushMessage(token="t", title="T", body="B").send(uow)
        await uow.commit()

    assert await Worker().run_once() == 2
    assert notifications.sms_outbox == [sms]
    assert notifications.push_outbox == [push]


async def test_failures_are_retried_with_the_same_id(tables: Install) -> None:
    """A provider that fails gets the same message, with the same id, again."""
    tables(SMS_BACKEND="domainkit_tests.test_notifications.GatewaySms")
    GatewaySms.failures = 2
    queued = await send_sms(PHONE, "Code")

    await Worker().run_once()

    assert GatewaySms.sent == [queued]
    ((row),) = await outbox_rows()
    assert row["attempts"] == 3


async def test_settings_set_attempts_and_queue(tables: Install) -> None:
    """``MAX_ATTEMPTS`` dead-letters; ``QUEUE`` picks the workers."""
    tables(
        SMS_BACKEND="domainkit_tests.test_notifications.GatewaySms",
        MAX_ATTEMPTS=2,
        QUEUE="texts",
    )
    GatewaySms.failures = 5
    await send_sms(PHONE, "Code")

    assert await Worker(queues=["default"]).run_once() == 0
    assert await Worker(queues=["texts"]).run_once() == 2

    (row,) = await outbox_rows()
    assert (row["queue"], row["status"], row["last_error"]) == (
        "texts",
        DEAD,
        "NotificationFailed: gateway answered 503",
    )


async def test_deliver_sends_at_once(tables: Install) -> None:
    """``deliver_sms`` and ``deliver_push`` skip the outbox, ids included."""
    sms = SmsMessage(to=PHONE, body="Now")
    push = PushMessage(token="t", title="Now", body="")

    assert await deliver_sms([sms, sms]) == 2
    assert await deliver_push([push]) == 1

    assert await outbox_rows() == []
    first, second = notifications.sms_outbox
    assert first.message_id is not None
    assert first.message_id != second.message_id
    assert notifications.push_outbox[0].message_id is not None


async def test_console_backends_write_what_they_send(
    tables: Install, capsys: pytest.CaptureFixture[str]
) -> None:
    """For development: the number and text, or the title, text, and data."""
    tables(
        SMS_BACKEND=f"{BACKENDS}.ConsoleSmsBackend",
        PUSH_BACKEND=f"{BACKENDS}.ConsolePushBackend",
    )

    await deliver_sms([SmsMessage(to=PHONE, body="Code 1")])
    await deliver_push([PushMessage(token="t", title="Hi", body="B", data={"k": "v"})])

    assert capsys.readouterr().out == (
        f"SMS to {PHONE}:\nCode 1\n{'-' * 79}\nPush: Hi\nB\n{{'k': 'v'}}\n{'-' * 79}\n"
    )


def test_notifications_need_messaging() -> None:
    """Without the worker, nothing would send them."""
    registry.clear()
    with (
        override_settings(INSTALLED_DOMAINS=[NOTIFICATIONS, ALPHA]),
        pytest.raises(
            ImproperlyConfigured, match=r"needs 'domainkit\.contrib\.messaging'"
        ),
    ):
        setup()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"SMS_BACKEND": "domainkit_tests.test_notifications.Missing"}, "SMS_BACKEND"),
        (
            {"PUSH_BACKEND": "domainkit_tests.test_notifications.NotABackend"},
            "PUSH_BACKEND: .* is not a PushBackend",
        ),
        (
            {"SMS_BACKEND": "domainkit_tests.test_notifications.PushService"},
            "is not a SmsBackend",
        ),
        ({"QUEUE": "no spaces"}, "QUEUE"),
        ({"MAX_ATTEMPTS": 0}, "MAX_ATTEMPTS"),
    ],
)
def test_backends_are_checked_at_startup(
    install: Install, options: dict[str, Any], message: str
) -> None:
    """A wrong path or setting fails when the application starts."""
    with pytest.raises(ImproperlyConfigured, match=message):
        install(**options)


DEPLOYED = {
    "ENVIRONMENT": "production",
    "ALLOWED_HOSTS": ["api.example.com"],
    # No network database, which would need verified TLS here.
    "DATABASES": {"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
}
ADAPTERS = {
    "SMS_BACKEND": "domainkit_tests.test_notifications.GatewaySms",
    "PUSH_BACKEND": "domainkit_tests.test_notifications.PushService",
}


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({}, "NOTIFICATIONS.SMS_BACKEND .*MemorySmsBackend' is for development"),
        (
            {"SMS_BACKEND": ADAPTERS["SMS_BACKEND"]},
            "NOTIFICATIONS.PUSH_BACKEND .* write an adapter .* for production",
        ),
        (
            {"PUSH_BACKEND": ADAPTERS["PUSH_BACKEND"]},
            "NOTIFICATIONS.SMS_BACKEND",
        ),
    ],
)
def test_deployments_refuse_development_backends(
    install: Install, options: dict[str, Any], message: str
) -> None:
    """Staging and production need a provider adapter for each channel."""
    with pytest.raises(ImproperlyConfigured, match=message):
        install(settings=DEPLOYED, **options)


def test_deployments_accept_adapters(install: Install) -> None:
    """Adapters that deliver pass the deployment check."""
    install(settings=DEPLOYED, **ADAPTERS)

    assert isinstance(notifications.get_sms_backend(), GatewaySms)
    assert isinstance(notifications.get_push_backend(), PushService)


@pytest.mark.parametrize("number", ["+33612345678", "+1234567", "+123456789012345"])
def test_phone_numbers_are_e164(number: str) -> None:
    """A plus, a country code, and up to 15 digits."""
    assert SmsMessage(to=number, body="x").to == number


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"to": "0612345678"}, "to"),
        ({"to": "+0612345678"}, "to"),
        ({"to": "+123456"}, "to"),
        ({"to": "+1234567890123456"}, "to"),
        ({"to": "+33 6 12 34 56 78"}, "to"),
        ({"to": "+33612345678\n"}, "to"),
        ({"body": ""}, "at least 1 character"),
        ({"body": "x" * 1601}, "at most 1600"),
        ({"sender": "TwelveChars1"}, "sender"),
        ({"sender": "Bad\nSender"}, "sender"),
        ({"sender": "Bad-Sender"}, "sender"),
        ({"extra": "x"}, "Extra inputs"),
    ],
)
def test_text_messages_are_bounded(fields: dict[str, Any], message: str) -> None:
    """Numbers, lengths, and sender ids are checked."""
    with pytest.raises(ValidationError, match=message):
        SmsMessage(**{"to": PHONE, "body": "x", **fields})


def test_text_messages_at_their_limits_are_fine() -> None:
    """1600 characters, an 11-character sender, or a number as sender."""
    assert SmsMessage(to=PHONE, body="x" * 1600, sender="Eleven Char")
    assert SmsMessage(to=PHONE, body="x", sender=PHONE).sender == PHONE


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"token": ""}, "at least 1 character"),
        ({"token": "t" * 4097}, "at most 4096"),
        ({"token": "a\nb"}, "line breaks"),
        ({"title": "a\r\nb"}, "line breaks"),
        ({"title": "x" * 256}, "at most 255"),
        ({"body": "x" * 4097}, "at most 4096"),
        ({"data": {str(n): "v" for n in range(51)}}, "at most 50"),
        ({"data": {"": "v"}}, "data keys have 1 to 1024"),
        ({"data": {"k" * 1025: "v"}}, "data keys"),
        ({"data": {"k": "v" * 1025}}, "values 1024 or fewer"),
    ],
)
def test_push_notifications_are_bounded(fields: dict[str, Any], message: str) -> None:
    """Tokens and titles are one line; everything has a size limit."""
    with pytest.raises(ValidationError, match=message):
        PushMessage(**{"token": "t", "title": "T", "body": "B", **fields})


def test_push_notifications_at_their_limits_are_fine() -> None:
    """50 data items of 1024 characters, a 4096-character token."""
    data = {f"{n:04}" + "k" * 1020: "v" * 1024 for n in range(50)}

    message = PushMessage(token="t" * 4096, title="x" * 255, body="b\nc", data=data)

    assert len(message.data) == 50


def test_reprs_show_no_number_token_or_text() -> None:
    """Numbers, tokens, and one-time codes stay out of reprs."""
    sms = SmsMessage(to=PHONE, body="Code 123456").prepared()
    push = PushMessage(token="secret-token", title="Code", body="654321").prepared()

    assert repr(sms) == f"SmsMessage(message_id={sms.message_id!r})"
    for hidden in (PHONE, "123456", "secret-token", "654321"):
        assert hidden not in repr(sms) + repr(push) + str(sms) + str(push)
    assert sms.prepared() is sms

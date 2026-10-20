"""Email through the outbox: queued with the commit, sent by the worker."""

import email
import email.policy
import logging
import smtplib
import socket
import ssl
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.mail import (
    BaseEmailBackend,
    EmailMessage,
    MailDeliveryFailed,
    SmtpBackend,
    SmtpSettings,
    TemplateDoesNotExist,
    TemplateError,
    deliver,
    render_mail,
    send,
    send_mail,
)
from domainkit.contrib.mail.message import MAX_RECIPIENTS, check_address
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.tables import DEAD, DONE, messages
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakePackage
from domainkit_tests.fake_smtp import (
    SmtpServer,
    certificate,
    server_context,
    smtp_server,
)
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, notes

pytestmark = pytest.mark.anyio

MESSAGING = "domainkit.contrib.messaging"
MAIL = "domainkit.contrib.mail"
ALPHA = "domainkit_tests.sample_domains.alpha"
MEMORY = "domainkit.contrib.mail.backends.MemoryBackend"
CONSOLE = "domainkit.contrib.mail.backends.ConsoleBackend"
FAST = {
    "RETRY_BASE_SECONDS": 0.001,
    "RETRY_MAX_SECONDS": 0.001,
    "TASK_TIMEOUT": 5,
    "VISIBILITY_TIMEOUT": 10,
}

DEPLOYED = {
    "ENVIRONMENT": "production",
    "ALLOWED_HOSTS": ["api.example.com"],
    # No network database, which would need verified TLS here.
    "DATABASES": {"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
}

type Install = Callable[..., None]


class FlakyBackend(BaseEmailBackend):
    """Fails ``failures`` times, then keeps what it is given."""

    failures: ClassVar[int] = 0
    sent: ClassVar[list[EmailMessage]] = []

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Fail, or keep ``messages``."""
        if FlakyBackend.failures:
            FlakyBackend.failures -= 1
            raise MailDeliveryFailed("SMTPServerDisconnected")
        FlakyBackend.sent.extend(messages)
        return len(messages)


class NotABackend:
    """Has nothing to do with mail."""


@pytest.fixture(autouse=True)
def immediate_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retries due at once."""
    monkeypatch.setattr(worker_module, "backoff", lambda attempts, config: 0.0)


@pytest.fixture(autouse=True)
def empty_outbox() -> Iterator[None]:
    """Start and end every test with no mail sent."""
    mail.outbox.clear()
    FlakyBackend.sent.clear()
    FlakyBackend.failures = 0
    yield
    mail.outbox.clear()


@pytest.fixture
def install(database_url: str) -> Iterator[Install]:
    """``install(**MAIL)``: messaging and mail on an empty database."""
    with ExitStack() as stack:

        def install_mail(
            *,
            settings: dict[str, Any] | None = None,
            **options: Any,  # noqa: ANN401 - MAIL options.
        ) -> None:
            registry.clear()
            values = {
                "INSTALLED_DOMAINS": [MESSAGING, MAIL, ALPHA],
                "DATABASES": {"default": {"URL": database_url}},
                "MESSAGING": FAST,
                "MAIL": {"BACKEND": MEMORY, **options},
                **(settings or {}),
            }
            stack.enter_context(override_settings(**values))
            setup()

        yield install_mail
    registry.clear()


@pytest.fixture
async def tables(install: Install) -> AsyncIterator[Install]:
    """Install mail, and create the outbox and notes tables."""
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


async def write_note(*, commit: bool, **mail_args: Any) -> None:  # noqa: ANN401 - send_mail arguments.
    """Write a note and queue mail with it; commit, or roll back."""
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        await send_mail(uow=uow, **mail_args)
        if commit:
            await uow.commit()


WELCOME: dict[str, Any] = {
    "subject": "Welcome",
    "message": "Hello, Alice.",
    "recipient_list": ["Alice <alice@example.com>"],
}


async def test_mail_goes_out_once_the_change_commits(tables: Install) -> None:
    """The task is stored with the commit; the worker sends the message."""
    await write_note(commit=True, **WELCOME)

    (row,) = await outbox_rows()
    assert (row["kind"], row["target"], row["queue"], row["max_attempts"]) == (
        "task",
        "domainkit.mail.send",
        "default",
        5,
    )
    assert mail.outbox == []

    assert await Worker().run_once() == 1

    (sent,) = mail.outbox
    assert (sent.subject, sent.body, sent.to) == (
        "Welcome",
        "Hello, Alice.",
        ("Alice <alice@example.com>",),
    )
    assert sent.from_email == "webmaster@localhost"
    assert sent.message_id is not None
    assert sent.message_id.endswith("@localhost>")
    assert sent.message_id == row["payload"]["message"]["message_id"]


async def test_mail_of_a_rolled_back_change_is_never_sent(tables: Install) -> None:
    """No commit, no message: nothing is stored, and the worker finds nothing."""
    await write_note(commit=False, **WELCOME)
    async with NotesUnitOfWork() as uow:
        await EmailMessage(subject="Hi", body="Text", to=("a@x.org",)).send(uow)

    assert await outbox_rows() == []
    assert await Worker().run_once() == 0
    assert mail.outbox == []


async def test_mail_without_a_unit_of_work_is_queued_at_once(tables: Install) -> None:
    """``send`` alone stores the task in its own transaction."""
    message = EmailMessage(subject="Hi", body="Text", to=("a@example.com",))

    queued = await message.send()

    assert queued.from_email == "webmaster@localhost"
    assert queued.message_id is not None
    assert message.message_id is None  # The original is left as it was.
    (row,) = await outbox_rows()
    assert row["target"] == "domainkit.mail.send"
    await Worker().run_once()
    assert mail.outbox == [queued]


async def test_html_alternatives_and_senders_travel_with_the_message(
    tables: Install,
) -> None:
    """``send_mail`` takes an HTML body and a sender of its own."""
    queued = await send_mail(
        "Report",
        "Plain",
        ["a@example.com", "b@example.org"],
        from_email="Reports <reports@billing.example.com>",
        html_message="<p>Rich</p>",
    )

    await Worker().run_once()

    assert mail.outbox == [queued]
    assert queued.html == "<p>Rich</p>"
    assert queued.message_id is not None
    assert queued.message_id.endswith("@billing.example.com>")


async def test_failed_sends_are_retried_until_they_work(tables: Install) -> None:
    """Each failure is retried; the same message, with the same id, goes out."""
    tables(BACKEND="domainkit_tests.test_mail.FlakyBackend")
    FlakyBackend.failures = 2
    queued = await send_mail(**WELCOME)

    await Worker().run_once()  # Retries are due at once here.

    (row,) = await outbox_rows()
    assert (row["status"], row["attempts"]) == (DONE, 3)
    assert FlakyBackend.sent == [queued]


async def test_mail_settings_set_attempts_and_queue(tables: Install) -> None:
    """``MAX_ATTEMPTS`` dead-letters; ``QUEUE`` picks the workers."""
    tables(
        BACKEND="domainkit_tests.test_mail.FlakyBackend",
        MAX_ATTEMPTS=2,
        QUEUE="mail",
    )
    FlakyBackend.failures = 5
    await send_mail(**WELCOME)

    assert await Worker(queues=["default"]).run_once() == 0
    assert await Worker(queues=["mail"]).run_once() == 2

    (row,) = await outbox_rows()
    assert (row["queue"], row["max_attempts"], row["status"]) == ("mail", 2, DEAD)
    assert row["last_error"] == "MailDeliveryFailed: SMTPServerDisconnected"
    assert FlakyBackend.sent == []


async def test_deliver_sends_at_once(tables: Install) -> None:
    """``deliver`` skips the outbox, for work already in the worker."""
    message = EmailMessage(subject="Now", body="Text", to=("a@example.com",))

    assert await deliver([message, message]) == 2

    assert await outbox_rows() == []
    assert [sent.subject for sent in mail.outbox] == ["Now", "Now"]
    assert mail.outbox[0].from_email == "webmaster@localhost"
    assert mail.outbox[0].message_id != mail.outbox[1].message_id


def test_mail_needs_messaging(database_url: str) -> None:
    """Without the worker, nothing would send it."""
    registry.clear()
    with (
        override_settings(INSTALLED_DOMAINS=[MAIL, ALPHA]),
        pytest.raises(
            ImproperlyConfigured,
            match=r"needs 'domainkit\.contrib\.messaging' in INSTALLED_DOMAINS: "
            r"the worker sends the mail\.$",
        ),
    ):
        setup()


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        ("domainkit_tests.test_mail.Missing", "MAIL.BACKEND: .*Missing"),
        ("domainkit_tests.test_mail.NotABackend", "is not a BaseEmailBackend"),
    ],
)
def test_backends_are_checked_at_startup(
    install: Install, backend: str, message: str
) -> None:
    """A wrong path fails when the application starts, not at the first email."""
    with pytest.raises(ImproperlyConfigured, match=message):
        install(BACKEND=backend)


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("backend", [MEMORY, CONSOLE])
def test_deployments_refuse_development_backends(
    install: Install, environment: str, backend: str
) -> None:
    """Memory keeps mail in the process; the console prints reset links."""
    deployed = {**DEPLOYED, "ENVIRONMENT": environment}

    with pytest.raises(
        ImproperlyConfigured,
        match=f"for development and tests; use one that delivers mail in {environment}",
    ):
        install(BACKEND=backend, settings=deployed)


def test_deployments_accept_smtp_over_tls(install: Install) -> None:
    """The default backend, encrypted, passes."""
    install(BACKEND="domainkit.contrib.mail.backends.SmtpBackend", settings=DEPLOYED)

    assert isinstance(mail.get_backend(), SmtpBackend)


def test_settings_check_the_sender_and_queue(install: Install) -> None:
    """A sender that is not one address, or a bad queue name, is refused."""
    with pytest.raises(ImproperlyConfigured, match="DEFAULT_FROM_EMAIL"):
        install(DEFAULT_FROM_EMAIL="a@b.c, d@e.f")
    with pytest.raises(ImproperlyConfigured, match="QUEUE"):
        install(QUEUE="no spaces")
    with pytest.raises(ValidationError, match="USER and PASSWORD go together"):
        SmtpSettings(USER="mailer")
    assert SmtpSettings().port == 587
    assert SmtpSettings(SECURITY="tls").port == 465
    assert SmtpSettings(SECURITY="none").port == 25
    assert SmtpSettings(PORT=2525).port == 2525
    assert "hunter2" not in repr(SmtpSettings(USER="u", PASSWORD="hunter2"))


@pytest.mark.parametrize(
    "address",
    [
        "alice@example.com",
        "Alice <alice@example.com>",
        '"Smith, Alice" <alice@example.com>',
        "Zoë <zoe@example.com>",
        "o'brien+tag@mail.example.co.uk",
        "  padded@example.com  ",
        "x@localhost",
        "a@xn--bcher-kva.example",
    ],
)
def test_addresses_people_use_are_accepted(address: str) -> None:
    """Plain addresses, display names, and punycode domains are fine."""
    assert check_address(address) == address.strip()


def longest(local: int = 64, domain: int = 189) -> str:
    """An address with a local part and a domain of these lengths."""
    labels = ["d" * 63] * (domain // 64) + ["d" * (domain % 64)]
    return "l" * local + "@" + ".".join(labels)


def test_addresses_may_be_as_long_as_the_rfcs_allow() -> None:
    """64 characters before the @, 254 in all, 320 with a display name."""
    address = longest()
    named = "N" * 63 + f" <{address}>"

    assert (len(address), len(named)) == (254, 320)
    assert check_address(address) == address
    assert check_address(named) == named
    for refused in (longest(domain=190), "N" * 64 + f" <{address}>", longest(65, 188)):
        with pytest.raises(ValueError, match="not an email address"):
            check_address(refused)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "alice",
        "@example.com",
        "alice@",
        "alice@@example.com",
        "a@b.c, d@e.f",
        "alice@example.com\r\nBcc: eve@example.com",
        "alice@example.com\nBcc: eve@example.com",
        "Alice\r\n <alice@example.com>",
        "alice@exa mple.com",
        "alice@-example.com",
        "alice@example-.com",
        "a..b@example.com",
        ".a@example.com",
        "zoë@example.com",
        "alice@bücher.example",
        "a" * 65 + "@example.com",
        "a@" + "b" * 64 + ".com",
        "a@" + ".".join(["b" * 63] * 4) + ".com",
        "Name " + "x" * 320 + " <a@example.com>",
        "a@example.com\x00",
        "Al\u2028ice <a@example.com>",
    ],
)
def test_anything_else_is_not_an_address(address: str) -> None:
    """Lists, header injection, and malformed addresses are refused unquoted."""
    with pytest.raises(ValueError, match=r"email address|line breaks") as refused:
        check_address(address)

    assert str(refused.value) in {
        "not an email address: user@example.com or Name <...>",
        "line breaks and control characters are not allowed",
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"subject": "Hi\r\nBcc: eve@example.com"}, "line breaks"),
        ({"subject": "Hi\nthere"}, "line breaks"),
        ({"subject": "x" * 999}, "at most 998"),
        ({"to": ()}, "1 to 100 addresses"),
        ({"to": tuple(f"u{n}@example.com" for n in range(101))}, "1 to 100"),
        (
            {"to": ("a@x.org",), "bcc": tuple(f"u{n}@x.org" for n in range(100))},
            "1 to 100",
        ),
        ({"to": ("bad",)}, "email address"),
        ({"from_email": "a@x.org\nCc: eve@x.org"}, "line breaks"),
        ({"reply_to": ("nope",)}, "email address"),
        ({"headers": {"Bcc": "eve@example.com"}}, "may not replace"),
        ({"headers": {"subject": "Other"}}, "may not replace"),
        ({"headers": {"X-Tag\r\nBcc": "x"}}, "may not replace"),
        ({"headers": {"X Tag": "x"}}, "may not replace"),
        ({"headers": {"-Tag": "x"}}, "may not replace"),
        ({"headers": {"X-" + "a" * 63: "x"}}, "may not replace"),
        ({"headers": {"X-Tag": "a\r\nBcc: eve@example.com"}}, "line breaks"),
        ({"headers": {"X-Tag": "a" * 999}}, "998 or fewer"),
        ({"headers": {f"X-{n}": "x" for n in range(21)}}, "at most 20"),
        ({"body": "x" * 1_000_001}, "at most 1000000"),
        ({"html": "x" * 1_000_001}, "at most 1000000"),
        ({"message_id": "<no-at-sign>"}, "message_id"),
        ({"unknown": "field"}, "Extra inputs"),
    ],
)
def test_messages_refuse_header_injection_and_excess(
    fields: dict[str, Any], message: str
) -> None:
    """Nothing in a message can add a header or a recipient; sizes are bounded."""
    values: dict[str, Any] = {
        "subject": "Hi",
        "body": "Text",
        "to": ("a@example.com",),
        **fields,
    }

    with pytest.raises(ValidationError, match=message):
        EmailMessage(**values)


def test_messages_at_their_limits_are_fine() -> None:
    """A hundred recipients, twenty headers, a 998-character subject."""
    message = EmailMessage(
        subject="x" * 998,
        body="",
        to=tuple(f"u{n}@x.org" for n in range(50)),
        cc=tuple(f"c{n}@x.org" for n in range(49)),
        bcc=("u1@x.org",),
        headers={f"X-{n}": "a" * 998 for n in range(19)} | {"X-" + "a" * 62: "v"},
    )

    assert len(message.recipients()) == MAX_RECIPIENTS - 1  # u1 comes once.
    assert message.recipients()[:2] == ["u0@x.org", "u1@x.org"]


def test_the_mime_form_hides_bcc_and_keeps_every_part() -> None:
    """Headers in order, Bcc in the envelope only, HTML as an alternative."""
    message = EmailMessage(
        subject="Ünïcode subject",
        body="Plain body",
        html="<p>Rich</p>",
        to=("Zoë <zoe@example.com>", "b@example.com"),
        cc=("c@example.com",),
        bcc=("hidden@example.com",),
        reply_to=("help@example.com",),
        headers={"List-Unsubscribe": "<https://example.com/u>"},
    ).prepared("Sender <sender@example.org>")

    raw = message.to_mime().as_bytes()
    parsed = email.message_from_bytes(raw, policy=email.policy.default)

    assert b"\r\nFrom: Sender <sender@example.org>\r\n" in raw  # SMTP's form.
    assert b"\n" not in raw.replace(b"\r\n", b"")

    assert parsed["Subject"] == "Ünïcode subject"
    assert parsed["From"] == "Sender <sender@example.org>"
    assert parsed["To"] == "Zoë <zoe@example.com>, b@example.com"
    assert parsed["Cc"] == "c@example.com"
    assert parsed["Reply-To"] == "help@example.com"
    assert parsed["Bcc"] is None
    assert parsed["Message-ID"] == message.message_id
    assert parsed["List-Unsubscribe"] == "<https://example.com/u>"
    assert parsed["Date"] is not None
    assert parsed.get_content_type() == "multipart/alternative"
    text, rich = parsed.iter_parts()
    assert text.get_content().splitlines() == ["Plain body"]
    assert rich.get_content_type() == "text/html"
    assert rich.get_content().splitlines() == ["<p>Rich</p>"]
    assert message.recipients() == [
        "zoe@example.com",
        "b@example.com",
        "c@example.com",
        "hidden@example.com",
    ]


def test_headers_are_spelled_as_mail_programs_write_them() -> None:
    """Each name as the RFCs spell it; the date of the clock, in UTC."""
    message = EmailMessage(
        subject="S",
        body="B",
        to=("a@example.com",),
        cc=("c@example.com",),
        reply_to=("r@example.com",),
    ).prepared("s@example.org")

    with clock.freeze_time("2026-01-02T03:04:05Z"):
        raw = message.to_mime().as_bytes()

    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    assert {line.partition(b":")[0] for line in head} >= {
        b"From",
        b"To",
        b"Cc",
        b"Reply-To",
        b"Subject",
        b"Date",
        b"Message-ID",
    }
    assert b"Date: Fri, 02 Jan 2026 03:04:05 +0000" in head


def test_plain_messages_have_one_part_and_no_optional_headers() -> None:
    """Without HTML, cc, or reply-to, those headers are absent."""
    message = EmailMessage(subject="S", body="B", to=("a@example.com",)).prepared(
        "s@example.org"
    )

    parsed = email.message_from_bytes(message.to_mime().as_bytes())

    assert parsed.get_content_type() == "text/plain"
    assert (parsed["Cc"], parsed["Reply-To"]) == (None, None)


def test_messages_must_be_prepared_before_sending() -> None:
    """A message without a sender or an id has no MIME form yet."""
    message = EmailMessage(subject="S", body="B", to=("a@example.com",))

    with pytest.raises(ValueError, match="Prepare the message") as refused:
        message.to_mime()
    assert str(refused.value) == "Prepare the message before sending it: prepared()."
    with pytest.raises(ValueError, match="Prepare the message"):
        message.model_copy(update={"from_email": "a@example.com"}).to_mime()

    prepared = message.prepared("Me <me@example.org>")
    again = prepared.prepared("other@example.net")
    assert prepared.from_email == "Me <me@example.org>"
    assert again == prepared  # A sender and an id, once given, stay.


def test_reprs_show_no_recipient_and_no_text() -> None:
    """Messages carry reset links and addresses: their repr names neither."""
    message = EmailMessage(
        subject="Reset", body="https://x/reset?token=s3cret", to=("a@example.com",)
    ).prepared("me@example.org")

    assert repr(message) == f"EmailMessage(message_id={message.message_id!r})"
    for hidden in ("s3cret", "a@example.com", "Reset", "me@example.org"):
        assert hidden not in repr(message)
        assert hidden not in str(message)


async def test_the_console_backend_writes_each_message(
    tables: Install, capsys: pytest.CaptureFixture[str]
) -> None:
    """For development: the MIME form, then a rule."""
    tables(BACKEND=CONSOLE)
    message = EmailMessage(subject="Console", body="Look here", to=("a@x.org",))

    assert await deliver([message, message]) == 2

    out = capsys.readouterr().out
    assert out.count("Subject: Console") == 2
    assert out.count("\n" + "-" * 79 + "\n") == 2
    assert "Look here" in out


# SMTP ---------------------------------------------------------------------------


@pytest.fixture
def tls_files(tmp_path: Path) -> tuple[Path, Path]:
    """A certificate for localhost, which also serves as its own CA."""
    return certificate(tmp_path)


def smtp_settings(server: SmtpServer, **options: Any) -> SmtpSettings:  # noqa: ANN401 - settings values.
    """Settings that reach ``server`` on 127.0.0.1."""
    return SmtpSettings(HOST="localhost", PORT=server.port, **options)


PREPARED = EmailMessage(
    subject="Over SMTP",
    body="Body",
    to=("Alice <alice@example.com>",),
    bcc=("bob@example.com",),
).prepared("Sender <sender@example.org>")


async def test_smtp_sends_over_starttls_with_a_checked_certificate(
    tables: Install, tls_files: tuple[Path, Path]
) -> None:
    """STARTTLS, the server's certificate checked against TLS_CA, then AUTH."""
    cert, key = tls_files
    server = SmtpServer(tls=server_context(cert, key), user="mailer", password="s3cret")
    with smtp_server(server):
        backend = SmtpBackend(
            smtp_settings(server, TLS_CA=cert, USER="mailer", PASSWORD="s3cret")
        )
        assert await backend.send_messages([PREPARED, PREPARED]) == 2

    assert server.commands[:4] == ["EHLO", "STARTTLS", "EHLO", "AUTH"]
    assert server.commands[-1] == "QUIT"
    first, second = server.received
    assert (first.sender, first.recipients) == (
        "sender@example.org",
        ["alice@example.com", "bob@example.com"],
    )
    parsed = email.message_from_bytes(first.data)
    assert (parsed["Subject"], parsed["Bcc"]) == ("Over SMTP", None)
    assert second.data == first.data


async def test_smtp_speaks_tls_at_once_on_port_465(
    tables: Install, tls_files: tuple[Path, Path]
) -> None:
    """``SECURITY = "tls"``: the connection is encrypted from the first byte."""
    cert, key = tls_files
    server = SmtpServer(tls=server_context(cert, key), implicit=True)
    with smtp_server(server):
        backend = SmtpBackend(smtp_settings(server, SECURITY="tls", TLS_CA=cert))
        assert await backend.send_messages([PREPARED]) == 1

    assert "STARTTLS" not in server.commands
    assert len(server.received) == 1


@pytest.mark.parametrize("security", ["starttls", "tls"])
async def test_smtp_refuses_a_certificate_it_cannot_check(
    tables: Install, tls_files: tuple[Path, Path], tmp_path: Path, security: str
) -> None:
    """A server certificate no trusted CA signed fails the delivery."""
    cert, key = tls_files
    other, _ = certificate(tmp_path, "other")
    server = SmtpServer(tls=server_context(cert, key), implicit=security == "tls")
    with smtp_server(server):
        backend = SmtpBackend(smtp_settings(server, TLS_CA=other, SECURITY=security))
        with pytest.raises(MailDeliveryFailed) as failed:
            await backend.send_messages([PREPARED])

    assert str(failed.value) == "SSLCertVerificationError"
    assert server.received == []


async def test_smtp_needs_tls_unless_debugging(tables: Install) -> None:
    """Plain text is refused outside DEBUG, and works with it (a local catcher)."""
    server = SmtpServer()
    with pytest.raises(ImproperlyConfigured, match='SECURITY = "none"') as refused:
        SmtpBackend(SmtpSettings(SECURITY="none"))
    assert str(refused.value) == (
        'MAIL.SMTP.SECURITY = "none" sends mail in plain text; use "starttls" or '
        '"tls" (only DEBUG allows it).'
    )
    with pytest.raises(ImproperlyConfigured, match='SECURITY = "none"'):
        tables(
            SMTP={"SECURITY": "none"},
            BACKEND="domainkit.contrib.mail.backends.SmtpBackend",
        )

    with override_settings(DEBUG=True), smtp_server(server):
        backend = SmtpBackend(smtp_settings(server, SECURITY="none"))
        assert await backend.send_messages([PREPARED]) == 1
    assert server.commands[0] == "EHLO"
    assert "STARTTLS" not in server.commands


async def test_smtp_without_starttls_offered_fails(tables: Install) -> None:
    """A server that offers no STARTTLS is not sent anything in plain text."""
    server = SmtpServer()
    with smtp_server(server):
        backend = SmtpBackend(smtp_settings(server))
        with pytest.raises(MailDeliveryFailed, match="SMTPNotSupportedError"):
            await backend.send_messages([PREPARED])

    assert server.received == []
    assert "MAIL" not in server.commands


@pytest.mark.parametrize(
    ("server", "error"),
    [
        (
            SmtpServer(user="mailer", password="right"),
            "SMTPAuthenticationError: the server answered 535",
        ),
        (
            SmtpServer(refuse={"alice@example.com", "bob@example.com"}),
            "SMTPRecipientsRefused: the server refused every recipient",
        ),
    ],
)
async def test_smtp_refusals_name_no_address(
    tables: Install, server: SmtpServer, error: str
) -> None:
    """Wrong credentials and refused recipients fail with the code alone."""
    with override_settings(DEBUG=True), smtp_server(server):
        options = {"USER": "mailer", "PASSWORD": "wrong"} if server.user else {}
        backend = SmtpBackend(smtp_settings(server, SECURITY="none", **options))
        with pytest.raises(MailDeliveryFailed) as failed:
            await backend.send_messages([PREPARED])

    assert str(failed.value) == error
    assert failed.value.__cause__ is None
    assert failed.value.__suppress_context__
    assert server.received == []
    assert server.commands[-1] in {"QUIT", "RSET"}


async def test_smtp_logs_partly_refused_messages_without_addresses(
    tables: Install, caplog: pytest.LogCaptureFixture
) -> None:
    """Some recipients refused: the rest get it, and the log counts the others."""
    server = SmtpServer(refuse={"bob@example.com"})
    with override_settings(DEBUG=True), smtp_server(server):
        backend = SmtpBackend(smtp_settings(server, SECURITY="none"))
        assert await backend.send_messages([PREPARED]) == 1

    (received,) = server.received
    assert received.recipients == ["alice@example.com"]
    (record,) = [
        r for r in caplog.records if r.name == "domainkit.contrib.mail.backends"
    ]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"The SMTP server refused 1 of 2 recipients of {PREPARED.message_id}"
    )
    assert "bob" not in caplog.text


async def test_smtp_failures_to_connect_are_delivery_failures(tables: Install) -> None:
    """Nothing listening: the worker will try again."""
    server = SmtpServer()
    with smtp_server(server):
        port = server.port
    with override_settings(DEBUG=True):
        backend = SmtpBackend(SmtpSettings(PORT=port, SECURITY="none", TIMEOUT=2))
    with pytest.raises(MailDeliveryFailed, match="ConnectionRefusedError"):
        await backend.send_messages([PREPARED])


@pytest.mark.parametrize("security", ["starttls", "tls"])
async def test_smtp_gives_up_on_a_silent_server(tables: Install, security: str) -> None:
    """``TIMEOUT`` bounds the wait for a server that accepts and says nothing."""
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        backend = SmtpBackend(
            SmtpSettings(
                HOST="127.0.0.1",
                PORT=silent.getsockname()[1],
                SECURITY=security,
                TIMEOUT=0.3,
            )
        )
        with pytest.raises(MailDeliveryFailed, match=r"Timeout|Disconnected"):
            await backend.send_messages([PREPARED])


async def test_smtp_envelopes_come_from_the_sender(tables: Install) -> None:
    """A ``Sender`` header never becomes the envelope's sender."""
    server = SmtpServer()
    message = PREPARED.model_copy(update={"headers": {"Sender": "x@example.net"}})
    with override_settings(DEBUG=True), smtp_server(server):
        backend = SmtpBackend(smtp_settings(server, SECURITY="none"))
        assert await backend.send_messages([message]) == 1

    (received,) = server.received
    assert received.sender == "sender@example.org"


async def test_smtp_drops_a_connection_the_server_hung_up(
    tables: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A QUIT the server cannot answer still closes the socket."""
    closed: list[bool] = []
    original_close = smtplib.SMTP.close

    def quit_fails(self: smtplib.SMTP) -> tuple[int, bytes]:
        raise smtplib.SMTPServerDisconnected("gone")

    def close(self: smtplib.SMTP) -> None:
        closed.append(True)
        original_close(self)

    monkeypatch.setattr(smtplib.SMTP, "quit", quit_fails)
    monkeypatch.setattr(smtplib.SMTP, "close", close)
    server = SmtpServer()
    with override_settings(DEBUG=True), smtp_server(server):
        backend = SmtpBackend(smtp_settings(server, SECURITY="none"))
        assert await backend.send_messages([PREPARED]) == 1

    assert closed == [True]
    assert len(server.received) == 1


async def test_smtp_sends_nothing_for_no_messages(tables: Install) -> None:
    """No connection is opened for an empty batch."""
    backend = SmtpBackend(SmtpSettings(HOST="unreachable.invalid"))

    assert await backend.send_messages([]) == 0


def test_smtp_uses_the_mail_settings_by_default(install: Install) -> None:
    """Without settings of its own, the backend reads ``MAIL.SMTP``."""
    install(SMTP={"HOST": "smtp.example.com", "SECURITY": "tls"})

    assert SmtpBackend().config == SmtpSettings(HOST="smtp.example.com", SECURITY="tls")
    assert ssl.create_default_context().check_hostname


# Templates ----------------------------------------------------------------------


TEMPLATES = {
    "templates/mail/billing/invoice.subject.txt": (
        "Invoice {{ invoice.number }}\nfor {{ name }}\n"
    ),
    "templates/mail/billing/invoice.txt": (
        "Dear {{ name }},\n{{invoice.total}} is due.\n"
    ),
    "templates/mail/billing/invoice.html": (
        '<p>Dear {{ name }}</p><a href="{{ link }}">pay</a>'
    ),
    "templates/mail/billing/plain.txt": "Only text for {{ name }}.",
}


class Invoice:
    """An object whose attributes templates read."""

    number = 42
    total = "10.00 EUR"
    _secret = "hidden"

    def pay(self) -> None:
        """A method, which templates may not call."""


@pytest.fixture
def billing(install: Install, make_package: MakePackage) -> Install:
    """A domain with mail templates, installed after mail."""
    make_package("billing_mail", TEMPLATES)

    def install_with(**options: Any) -> None:  # noqa: ANN401 - MAIL options.
        domains = [MESSAGING, MAIL, ALPHA, "billing_mail"]
        install(settings={"INSTALLED_DOMAINS": domains}, **options)

    install_with()
    return install_with


def test_templates_render_text_and_escaped_html(billing: Install) -> None:
    """Values fill the subject and both bodies; HTML escapes them."""
    message = render_mail(
        "billing/invoice",
        {"name": "<Zoë & Co>", "invoice": Invoice(), "link": 'https://x/?a=1&b="2"'},
        to=["zoe@example.com"],
        from_email="billing@example.com",
        reply_to=["help@example.com"],
    )

    assert message.subject == "Invoice 42 for <Zoë & Co>"
    assert message.body == "Dear <Zoë & Co>,\n10.00 EUR is due.\n"
    assert message.html == (
        "<p>Dear &lt;Zoë &amp; Co&gt;</p>"
        '<a href="https://x/?a=1&amp;b=&quot;2&quot;">pay</a>'
    )
    assert (message.to, message.from_email, message.reply_to) == (
        ("zoe@example.com",),
        "billing@example.com",
        ("help@example.com",),
    )


def test_templates_without_html_or_subject(billing: Install) -> None:
    """The HTML body and the subject file are optional."""
    message = render_mail("billing/plain", {"name": "Ann"}, to=["a@example.com"])

    assert (message.subject, message.body, message.html) == (
        "",
        "Only text for Ann.",
        None,
    )


def test_template_dirs_come_first(billing: Install, tmp_path: Path) -> None:
    """A project overrides a domain's template in ``MAIL.TEMPLATE_DIRS``."""
    override = tmp_path / "mail"
    (override / "billing").mkdir(parents=True)
    (override / "billing" / "plain.txt").write_text(
        "Ours, {{ name }}.", encoding="utf-8"
    )
    billing(TEMPLATE_DIRS=[str(override)])

    message = render_mail("billing/plain", {"name": "Ann"}, to=["a@example.com"])

    assert message.body == "Ours, Ann."


@pytest.mark.parametrize(
    ("template", "context", "error", "message"),
    [
        (
            "billing/invoice",
            {"name": "x", "link": "y"},
            TemplateError,
            "needs 'invoice.number'",
        ),
        (
            "billing/plain",
            {"nom": "x"},
            TemplateError,
            "needs 'name', which is missing",
        ),
        (
            "billing/missing",
            {},
            TemplateDoesNotExist,
            "No mail template 'billing/missing'",
        ),
        ("../billing/plain", {}, TemplateError, "lower-case words"),
        ("/etc/passwd", {}, TemplateError, "lower-case words"),
        ("Billing/Plain", {}, TemplateError, "lower-case words"),
    ],
)
def test_template_errors(
    billing: Install,
    template: str,
    context: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    """Missing values, unknown templates, and paths outside the directories."""
    with pytest.raises(error, match=message):
        render_mail(template, context, to=["a@example.com"])


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{ invoice._secret }}", "braces hold a name"),
        ("{{ _private }}", "braces hold a name"),
        ("{{ invoice.pay }}", "'invoice.pay' is a method"),
        ("{{ invoice.missing }}", "needs 'invoice.missing'"),
        ("{{ name.upper }}", "is a method"),
        ("{{ name }", "braces hold a name"),
        ("{ name }}", "braces hold a name"),
        ("{{ 1 + 1 }}", "braces hold a name"),
        ("{{}}", "braces hold a name"),
        ("{{ name }} and }}", "braces hold a name"),
        ("{{ name }}{{ name }}, then {{", "braces hold a name"),
    ],
)
def test_templates_reach_no_private_name_or_method(template: str, message: str) -> None:
    """Only public attributes and keys; anything else in braces is an error."""
    from domainkit.contrib.mail.templates import render_string

    with pytest.raises(TemplateError, match=message) as raised:
        render_string(template, {"invoice": Invoice(), "name": "Ann"})
    if message == "braces hold a name":
        assert str(raised.value) == (
            "Template braces hold a name such as {{ user.email }}; names start "
            "with a letter."
        )


def test_template_values_are_not_templates() -> None:
    """A value holding braces is printed as it is, never rendered again."""
    from domainkit.contrib.mail.templates import render_string

    assert (
        render_string("{{ a }}-{{b.c}}", {"a": "{{ b }}", "b": {"c": 1}}) == "{{ b }}-1"
    )
    assert render_string("{{ a }}", {"a": None}) == "None"
    assert render_string("{{ a }}", {"a": "<b>"}, escape=True) == "&lt;b&gt;"
    assert render_string("no braces", {}) == "no braces"


async def test_templated_mail_is_queued_like_any_other(billing: Install) -> None:
    """``render_mail`` returns a message; ``send`` queues it."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
    message = render_mail("billing/plain", {"name": "Ann"}, to=["a@example.com"])

    queued = await send(message)
    await Worker().run_once()

    assert mail.outbox == [queued]
    await connections.close_all()

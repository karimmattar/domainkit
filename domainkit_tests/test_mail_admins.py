"""Admins hear of errors: one mail per kind and place, with no values in it."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from domainkit import clock
from domainkit.contrib import mail
from domainkit.contrib.mail import MailSettings
from domainkit.contrib.mail import admins as admins_module
from domainkit.contrib.mail.admins import mail_admins, report_to_admins
from domainkit_tests.test_mail import (
    Install,
    empty_outbox,
    immediate_retries,
    install,
    tables,
)

pytestmark = pytest.mark.anyio
ADMINS = ("ops@example.com", "dev@example.com")
__all__ = ["empty_outbox", "immediate_retries", "install", "tables"]


@pytest.fixture(autouse=True)
def forget_reports() -> None:
    """Every test starts with no report sent lately."""
    admins_module._last_sent.clear()


def failure(secret: str) -> ValueError:
    """A raised error whose message holds what a user sent."""
    try:
        raise ValueError(f"card {secret} refused")
    except ValueError as exc:
        return exc


def test_an_error_is_mailed_once_per_kind_and_place(install: Install) -> None:
    """Its type, place, frames, and context; never its message; once a while."""
    install(ADMINS=ADMINS, ADMINS_EVERY=timedelta(minutes=5))
    context = {"request_id": "req-1", "path": "/pay"}
    with clock.freeze_time("2026-01-01T09:00:00Z") as frozen:
        report_to_admins(failure("4111111111111111"), context)
        report_to_admins(failure("4000000000000002"), context)
        frozen.tick(timedelta(minutes=6))
        report_to_admins(failure("5555555555554444"), context)

    first, later = mail.outbox
    assert first.to == ADMINS
    assert first.subject == "Error: ValueError in failure"
    assert "builtins.ValueError was raised at " in first.body
    assert "  request_id: req-1" in first.body
    assert " in failure" in first.body
    assert "4111" not in first.body + first.subject
    assert later.subject == first.subject


def test_nobody_named_means_nothing_sent(install: Install) -> None:
    """Without ADMINS, reports and mails go nowhere."""
    install()
    report_to_admins(failure("x"), {})

    assert mail.outbox == []


async def test_mail_admins_writes_to_them(tables: Install) -> None:
    """Django's mail_admins: subject and body, to every admin; or nothing."""
    tables(ADMINS=ADMINS)
    sent = await mail_admins("Backup done", "All tables saved.")

    assert sent is not None
    assert sent.to == ADMINS
    tables()
    assert await mail_admins("Backup done", "All tables saved.") is None


def test_an_error_without_a_traceback_and_many_kinds(install: Install) -> None:
    """Never raised: an unknown place. Past the memory's size, it starts over."""
    install(ADMINS=ADMINS)
    report_to_admins(RuntimeError("never raised"), {})
    for each in range(admins_module.MAX_REMEMBERED + 1):
        admins_module._due((str(each), "here"), 300)

    assert "an unknown place" in mail.outbox[0].body
    assert len(admins_module._last_sent) < admins_module.MAX_REMEMBERED


def test_admins_are_addresses() -> None:
    """A name that is not an address is refused."""
    with pytest.raises(ValidationError):
        MailSettings(ADMINS=("not an address",))

"""A user is told by mail when a device is bound to their account (ADR 0137).

NIST SP 800-63B-4 asks that a subscriber be notified, by a channel other
than the one that bound it, when an authenticator is added.
"""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest

from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.devices.tasks import send_bound_notice
from domainkit.management.base import run_async
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_ciba import PHONE, App, Phone, app

__all__ = ["app"]  # The fixture the tests take.

SUBJECT = "A device was bound to your account"


@pytest.fixture(autouse=True)
def _no_mail_yet() -> Iterator[None]:
    """Each test starts with an empty outbox."""
    mail.outbox.clear()
    yield
    mail.outbox.clear()


def test_binding_a_device_mails_its_user(app: App) -> None:
    """Whichever way it was bound: the device's name, to the account's address."""
    phone = Phone(app(PHONE), email="alice@example.com")

    run_worker()

    [notice] = mail.outbox
    assert (notice.subject, tuple(notice.to)) == (SUBJECT, ("alice@example.com",))
    assert "Hello alice," in notice.body
    assert "bound to your account: Phone." in notice.body
    assert phone.device.id not in notice.body  # The name, never the id.


def test_the_notice_can_be_turned_off(app: App) -> None:
    """``DEVICES.BOUND_NOTICE = False``: nothing is queued."""
    client = app(PHONE)
    with override_settings(DEVICES={"BOUND_NOTICE": False}):
        Phone(client, email="alice@example.com")
        run_worker()

    assert mail.outbox == []


def test_nothing_goes_where_no_mail_can(app: App) -> None:
    """No address, a user turned off, a device or a user that is gone."""
    phone = Phone(app(PHONE))  # No address.
    device = UUID(phone.device.id)
    bob = create_user("bob", email="bob@example.com")

    async def turn_off(user_id: UUID) -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(user_id))
            user.is_active = False
            await uow.commit()

    run_worker()
    assert run_async(send_bound_notice(phone.user_id, device)) is False
    assert run_async(send_bound_notice(bob, uuid4())) is False  # No such device.
    assert run_async(send_bound_notice(uuid4(), device)) is False  # No such user.
    run_async(turn_off(phone.user_id))
    assert run_async(send_bound_notice(phone.user_id, device)) is False
    assert mail.outbox == []

"""In the phone modes, the ways into an account are the phone's to add (ADR 0134).

With ``CIBA.APPROVE_LOGINS`` set, a user who bound a device binds another,
keeps or removes a push token, and makes a personal token only by a
request one of their devices signs: a stolen web session whose thief knows
the password adds no phone of its own.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import update

from domainkit.conf import override_settings
from domainkit.contrib.auth.repositories import TokenStore
from domainkit.contrib.auth.tables import tokens
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.test_auth_http import PASSWORD, bearer
from domainkit_tests.test_auth_login_origins import origins
from domainkit_tests.test_ciba import PHONE, App, Phone, app
from domainkit_tests.test_ciba_logins import refused, web_login
from domainkit_tests.test_devices import Device, bind

__all__ = ["app"]  # The fixture the tests take.

DEVICE_LOGIN = {"DEVICE_LOGIN": True}


def devices_of(phone: Phone) -> list[str]:
    """The ids of the devices ``phone``'s user bound."""
    answer = phone.client.get("/auth/devices", headers=phone.headers)
    assert answer.status_code == 200, answer.json()
    return [each["id"] for each in answer.json()]


def logins() -> list[tuple[str | None, UUID | None]]:
    """How each stored login signed in, and its device: no other token."""
    return [(method, device) for method, device in origins() if method is not None]


def another(phone: Phone, device: Device, **body: Any) -> Any:  # noqa: ANN401
    """Bind ``device`` by a request ``phone`` signs: the response."""
    answer = phone.send(
        "POST",
        "/auth/devices",
        {
            "name": "New phone",
            "public_key": device.public(),
            "password": PASSWORD,
            **body,
        },
    )
    if answer.status_code == 201:
        device.id = answer.json()["id"]
    return answer


def test_a_web_session_binds_no_device_of_its_own(app: App) -> None:
    """The audit's finding: the password alone bound a second phone.

    That device then counted as the user's: it listed the logins, and
    signed the real phone out.
    """
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    thief = Device()

    answer = bind(web, laptop, thief)

    assert refused(answer), answer.json()
    assert devices_of(phone) == [phone.device.id]
    assert phone.send("GET", "/auth/me").status_code == 200


def test_the_phone_binds_another_phone_which_logs_in_by_its_key(app: App) -> None:
    """The old phone signs the bind; the new one's login is its own."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    new = Device()

    answer = another(phone, new)

    assert answer.status_code == 201, answer.json()
    assert set(devices_of(phone)) == {phone.device.id, new.id}
    assert logins() == [("password", UUID(phone.device.id))]  # Still the old one's.
    with override_settings(DEVICES=DEVICE_LOGIN):
        logged_in = phone.client.post(
            "/auth/devices/login", headers=new.headers("POST", "/auth/devices/login")
        )
    assert logged_in.status_code == 200, logged_in.json()
    assert logins()[-1] == ("device", UUID(new.id))
    me = phone.client.get("/auth/me", headers=bearer(logged_in.json()["access_token"]))
    assert me.json()["username"] == "alice"


def test_a_signed_bind_never_marks_the_signers_login_as_the_new_devices(
    app: App,
) -> None:
    """A login from before 2.2 has no device: binding another leaves it so.

    Marked as the new device's, it would end when that device is unbound,
    and stay when the old one is.
    """
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))

    async def unmark() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(tokens).values(device_id=None))

    run_async(unmark())

    assert another(phone, Device()).status_code == 201

    assert logins() == [("password", None)]


def test_push_tokens_are_the_phones_to_keep_and_remove(app: App) -> None:
    """A web session could take the pushes, or stop the phone's."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    [kept] = web.get("/auth/devices/push-tokens", headers=laptop).json()

    taken = web.post(
        "/auth/devices/push-tokens",
        json={"token": "the-thiefs", "platform": "fcm"},
        headers=laptop,
    )
    stopped = web.delete(f"/auth/devices/push-tokens/{kept['id']}", headers=laptop)
    renewed = phone.send(
        "POST", "/auth/devices/push-tokens", {"token": "renewed", "platform": "fcm"}
    )
    removed = phone.send("DELETE", f"/auth/devices/push-tokens/{kept['id']}")

    assert refused(taken), taken.json()
    assert refused(stopped), stopped.json()
    assert renewed.status_code == 201, renewed.json()
    assert removed.status_code == 204
    left = web.get("/auth/devices/push-tokens", headers=laptop).json()
    assert [each["id"] for each in left] == [renewed.json()["id"]]


def test_a_personal_token_is_the_phones_to_make(app: App) -> None:
    """It would outlast the phone ending the other logins."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    body = {"name": "script", "password": PASSWORD}

    stolen = web.post("/auth/tokens", json=body, headers=laptop)
    made = phone.send("POST", "/auth/tokens", body)

    assert refused(stolen), stolen.json()
    assert made.status_code == 201, made.json()
    assert [method for method, _ in logins()] == ["password", "push"]
    assert len(origins()) == len(logins()) + 2  # A refresh token, and the script's.


def test_outside_the_phone_modes_a_login_adds_them_as_before(app: App) -> None:
    """``APPROVE_LOGINS`` off: the password, as before."""
    phone = Phone(app(PHONE))
    second = Device()

    bound = bind(phone.client, phone.headers, second)
    pushed = phone.client.post(
        "/auth/devices/push-tokens",
        json={"token": "another", "platform": "apns"},
        headers=phone.headers,
    )
    made = phone.client.post(
        "/auth/tokens",
        json={"name": "script", "password": PASSWORD},
        headers=phone.headers,
    )
    removed = phone.client.delete(
        f"/auth/devices/push-tokens/{pushed.json()['id']}", headers=phone.headers
    )

    assert bound.status_code == 201, bound.json()
    assert pushed.status_code == 201, pushed.json()
    assert made.status_code == 201, made.json()
    assert removed.status_code == 204


def test_a_device_goes_with_its_logins_or_not_at_all(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbinding wrote twice: a failure between left a login with no device."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))

    async def fail(self: TokenStore, user_id: UUID, device_id: UUID) -> int:
        raise RuntimeError("The database went away.")

    monkeypatch.setattr(TokenStore, "end_device_logins", fail)
    failed = phone.send("DELETE", f"/auth/devices/{phone.device.id}")
    monkeypatch.undo()

    assert failed.status_code == 500
    assert devices_of(phone) == [phone.device.id]
    assert phone.send("GET", "/auth/me").status_code == 200


def test_a_device_is_bound_with_its_logins_mark_or_not_at_all(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding wrote twice too: the device, then the mark on its login."""
    phone = Phone(app(PHONE))

    async def fail(self: TokenStore, *args: object, **named: object) -> int:
        raise RuntimeError("The database went away.")

    monkeypatch.setattr(TokenStore, "attach_device", fail)
    failed = bind(phone.client, phone.headers, Device())
    monkeypatch.undo()

    assert failed.status_code == 500
    assert devices_of(phone) == [phone.device.id]


def test_check_warns_of_a_phone_a_password_change_signs_out(app: App) -> None:
    """``ciba.W001``: the phone is the identity, yet ``"end"`` is the setting."""
    from domainkit.apps import registry

    app()
    config = registry.get_domain_config("ciba")

    def warnings() -> list[str]:
        return [found.id for found in config.weak_settings()]  # type: ignore[attr-defined]

    assert warnings() == []
    with override_settings(CIBA={"APPROVE_LOGINS": True}):
        assert warnings() == ["ciba.W001"]
        for chosen in ("keep", "ask"):
            with override_settings(AUTH={"CREDENTIAL_CHANGE_DEVICE_LOGINS": chosen}):
                assert warnings() == []

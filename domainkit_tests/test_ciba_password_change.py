"""What a password change does to the bound devices' logins (ADR 0131).

``AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS``: ``"end"`` (the default) ends them
with the other logins, ``"keep"`` keeps them, ``"ask"`` lets the request
say. A phone's login is the one its device signed, or bound the device.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from domainkit.conf import override_settings, settings
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user, login
from domainkit_tests.test_auth_login_origins import origins
from domainkit_tests.test_auth_mfa import clock, code_at, secret_of
from domainkit_tests.test_auth_reset_codes import (
    CODE,
    NEW_PASSWORD,
    ask_reset,
    drain,
)
from domainkit_tests.test_auth_reset_codes import code_in as code_of
from domainkit_tests.test_ciba import PHONE, App, Phone, app
from domainkit_tests.test_ciba_logins import web_login

__all__ = ["app", "clock"]  # The fixtures the tests take.

EMAIL = "alice@example.com"


@contextmanager
def device_logins(mode: str) -> Iterator[None]:
    """``AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS = mode`` inside."""
    with override_settings(
        AUTH={**settings.AUTH, "CREDENTIAL_CHANGE_DEVICE_LOGINS": mode}  # type: ignore[attr-defined]
    ):
        yield


def change(client: TestClient, headers: dict[str, str], **body: Any) -> Any:  # noqa: ANN401
    """``POST /auth/password`` from ``PASSWORD`` to ``NEW_PASSWORD``."""
    return client.post(
        "/auth/password",
        json={"old_password": PASSWORD, "new_password": NEW_PASSWORD, **body},
        headers=headers,
    )


def alive(phone: Phone) -> bool:
    """Whether the phone's own login still works."""
    return bool(phone.send("GET", "/auth/me").status_code == 200)


def last_code() -> str:
    """The code of the latest mail that carries one.

    The notice of a bound device carries none, and the worker may send it
    after the reset's mail.
    """
    from domainkit.contrib import mail

    return next(
        code_of(sent.body) for sent in reversed(mail.outbox) if CODE.search(sent.body)
    )


def mailed_reset_code(client: TestClient) -> str:
    """Ask a reset for ``EMAIL``; the code mailed."""
    ask_reset(client, identifier=EMAIL)
    drain()
    return last_code()


def test_the_phones_login_is_marked_when_it_binds_or_signs(app: App) -> None:
    """The login that bound the key, and a login the device signed, are its."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    device = UUID(phone.device.id)

    signed = phone.send(
        "POST", "/auth/login", {"identifier": "alice", "password": PASSWORD}
    )

    assert signed.status_code == 200, signed.json()
    assert [entry for entry in origins() if entry[1] is not None] == [
        ("password", device),  # The login that bound the device.
        ("password", device),  # Its signed login.
    ]


def test_by_default_a_password_change_signs_the_phone_out_too(app: App) -> None:
    """``"end"``: every other login ends, the phone's too, as before 2.2."""
    phone = Phone(app(PHONE))
    web = app()
    laptop = bearer(login(web, "alice")["access_token"])
    tablet = bearer(login(web, "alice")["access_token"])

    assert change(web, laptop).status_code == 204

    assert not alive(phone)
    assert web.get("/auth/me", headers=tablet).status_code == 401
    assert web.get("/auth/me", headers=laptop).status_code == 200


def test_keep_ends_the_other_logins_but_the_phones(app: App) -> None:
    """``"keep"``: the web sessions and personal tokens end; the phone stays."""
    phone = Phone(app(PHONE))
    web = app()
    laptop = bearer(login(web, "alice")["access_token"])
    tablet = bearer(login(web, "alice")["access_token"])

    with device_logins("keep"):
        answer = change(web, laptop)

    assert answer.status_code == 204, answer.json()
    assert alive(phone)
    assert web.get("/auth/me", headers=tablet).status_code == 401
    assert web.get("/auth/me", headers=laptop).status_code == 200


def test_ask_keeps_the_phone_unless_the_request_says_otherwise(app: App) -> None:
    """``"ask"``: left out or true keeps the phone; false ends it too."""
    phone = Phone(app(PHONE))
    web = app()
    laptop = bearer(login(web, "alice")["access_token"])

    with device_logins("ask"):
        assert change(web, laptop).status_code == 204
        assert alive(phone)
        back = web.post(
            "/auth/password",
            json={
                "old_password": NEW_PASSWORD,
                "new_password": PASSWORD,
                "keep_device_logins": True,
            },
            headers=laptop,
        )
        assert back.status_code == 204, back.json()
        assert alive(phone)
        assert change(web, laptop, keep_device_logins=False).status_code == 204
    assert not alive(phone)
    assert web.get("/auth/me", headers=laptop).status_code == 200


def test_the_request_may_not_choose_when_the_setting_decides(app: App) -> None:
    """``keep_device_logins`` under ``"end"`` or ``"keep"``: 422; nothing changes."""
    create_user("alice")
    web = app()
    laptop = bearer(login(web, "alice")["access_token"])

    for mode in ("end", "keep"):
        with device_logins(mode):
            answer = change(web, laptop, keep_device_logins=True)
        assert (answer.status_code, answer.json()["code"]) == (
            422,
            "device_logins_not_asked",
        )
    assert login(web, "alice")["access_token"]  # The old password still works.


def test_in_the_phone_modes_only_the_phone_signs_the_phone_out(app: App) -> None:
    """``"ask"`` with false from a web session: ``403 phone_required``."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    tablet = web_login(web, phone)

    with device_logins("ask"):
        refused = change(web, laptop, keep_device_logins=False)
        kept = change(web, laptop)

    assert (refused.status_code, refused.json()["code"]) == (403, "phone_required")
    assert kept.status_code == 204, kept.json()
    assert alive(phone)
    assert web.get("/auth/me", headers=tablet).status_code == 401
    with device_logins("ask"):
        signed = phone.send(
            "POST",
            "/auth/password",
            {
                "old_password": NEW_PASSWORD,
                "new_password": PASSWORD,
                "keep_device_logins": False,
            },
        )
    assert signed.status_code == 204, signed.json()
    assert alive(phone)  # Its own login is the one asking.
    assert web.get("/auth/me", headers=laptop).status_code == 401


def test_a_reset_keeps_or_ends_the_phones_login_as_a_change_does(app: App) -> None:
    """The reset by code follows the setting; ``"keep"`` keeps the phone."""
    phone = Phone(app(PHONE), email=EMAIL)
    web = app()
    laptop = bearer(login(web, "alice")["access_token"])

    with device_logins("keep"):
        answer = web.post(
            "/auth/password-reset/code",
            json={
                "identifier": EMAIL,
                "code": mailed_reset_code(web),
                "new_password": NEW_PASSWORD,
            },
        )

    assert answer.status_code == 204, answer.json()
    assert alive(phone)
    assert web.get("/auth/me", headers=laptop).status_code == 401


def test_a_reset_asking_to_end_the_phone_needs_it_and_spends_nothing(
    app: App,
) -> None:
    """Phone modes, ``"ask"``, false, unsigned: 403, and the code still works."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True), email=EMAIL)
    web = app()
    code = mailed_reset_code(web)
    body = {"identifier": EMAIL, "code": code, "new_password": NEW_PASSWORD}

    with device_logins("ask"):
        refused = web.post(
            "/auth/password-reset/code", json={**body, "keep_device_logins": False}
        )
        wrong = web.post(
            "/auth/password-reset/code",
            json={**body, "code": "000000", "keep_device_logins": False},
        )
        reset = web.post("/auth/password-reset/code", json=body)

    assert (refused.status_code, refused.json()["code"]) == (403, "phone_required")
    assert (wrong.status_code, wrong.json()["code"]) == (
        400,
        "invalid_verification_code",
    )
    assert reset.status_code == 204, reset.json()
    assert alive(phone)


def test_a_reset_may_end_the_phones_login_too(app: App) -> None:
    """``"end"`` and ``"ask"`` with false, outside the phone modes."""
    for mode, extra in (("end", {}), ("ask", {"keep_device_logins": False})):
        phone = Phone(app(PHONE), username=f"alice-{mode}", email=f"{mode}@x.example")
        web = app()
        ask_reset(web, identifier=f"{mode}@x.example")
        drain()
        with device_logins(mode):
            answer = web.post(
                "/auth/password-reset/code",
                json={
                    "identifier": f"{mode}@x.example",
                    "code": last_code(),
                    "new_password": NEW_PASSWORD,
                    **extra,
                },
            )
        assert answer.status_code == 204, answer.json()
        assert not alive(phone)


def test_unbinding_a_device_ends_its_logins_and_is_the_phones_to_do(
    app: App,
) -> None:
    """Phone modes: a web session cannot unbind the phone; the phone can."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)

    refused = web.delete(f"/auth/devices/{phone.device.id}", headers=laptop)
    unbound = phone.send("DELETE", f"/auth/devices/{phone.device.id}")

    assert (refused.status_code, refused.json()["code"]) == (403, "phone_required")
    assert unbound.status_code == 204, unbound.json()
    assert phone.client.get("/auth/me", headers=phone.headers).status_code == 401
    assert web.get("/auth/me", headers=laptop).status_code == 200


def test_credentials_naming_no_login_mark_nothing(app: App) -> None:
    """A certificate, say: no token and no family to mark as the device's."""
    from uuid import uuid4

    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.management.base import run_async

    alice = create_user("alice")
    app()

    async def attach() -> int:
        async with AuthUnitOfWork() as uow:
            return await uow.tokens.attach_device(alice, uuid4())

    assert run_async(attach()) == 0


# The second factor (ADR 0132).


def turn_on(
    client: TestClient,
    headers: dict[str, str],
    clock: list[float],
    **body: Any,  # noqa: ANN401 - fields of the request.
) -> Any:  # noqa: ANN401 - a response.
    """Start the authenticator app, then confirm it with ``body`` added."""
    started = client.post(
        "/auth/mfa/totp", json={"password": PASSWORD}, headers=headers
    )
    assert started.status_code == 200, started.json()
    return client.post(
        "/auth/mfa/totp/confirm",
        json={"code": code_at(secret_of(started.json()), clock[0]), **body},
        headers=headers,
    )


def test_turning_the_second_factor_on_follows_the_setting(
    app: App, clock: list[float]
) -> None:
    """``"end"`` signs the phone out; ``"keep"`` keeps it; ``"ask"`` asks."""
    for mode, extra, kept in (
        ("end", {}, False),
        ("keep", {}, True),
        ("ask", {}, True),
        ("ask", {"keep_device_logins": False}, False),
    ):
        name = f"{mode}-{len(extra)}"
        phone = Phone(app(PHONE), username=name)
        web = app()
        laptop = bearer(login(web, name)["access_token"])
        tablet = bearer(login(web, name)["access_token"])
        with device_logins(mode):
            answer = turn_on(web, laptop, clock, **extra)
        assert answer.status_code == 200, (mode, answer.json())
        assert alive(phone) is kept, mode
        assert web.get("/auth/me", headers=tablet).status_code == 401
        assert web.get("/auth/me", headers=laptop).status_code == 200


def test_the_confirmation_may_not_choose_when_the_setting_decides(
    app: App, clock: list[float]
) -> None:
    """``keep_device_logins`` under ``"keep"``: 422, and the factor stays off."""
    create_user("alice")
    web = app()
    laptop = bearer(login(web, "alice")["access_token"])

    with device_logins("keep"):
        answer = turn_on(web, laptop, clock, keep_device_logins=True)

    assert (answer.status_code, answer.json()["code"]) == (
        422,
        "device_logins_not_asked",
    )
    assert web.get("/auth/mfa", headers=laptop).json()["enabled"] is False


def test_in_the_phone_modes_only_the_phone_manages_the_factors(
    app: App, clock: list[float]
) -> None:
    """A web session cannot add or change a second factor; the phone can."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    reauth = {"password": PASSWORD, "code": "123456"}

    refused = [
        web.post("/auth/mfa/totp", json={"password": PASSWORD}, headers=laptop),
        web.post("/auth/mfa/totp/confirm", json={"code": "123456"}, headers=laptop),
        web.post("/auth/mfa/totp/disable", json=reauth, headers=laptop),
        web.post("/auth/mfa/recovery-codes", json=reauth, headers=laptop),
        web.post(
            "/auth/passkeys/register/options",
            json={"password": PASSWORD},
            headers=laptop,
        ),
        web.delete(f"/auth/passkeys/{UUID(int=1)}", headers=laptop),
    ]

    assert [(each.status_code, each.json()["code"]) for each in refused] == [
        (403, "phone_required")
    ] * len(refused)
    started = phone.send("POST", "/auth/mfa/totp", {"password": PASSWORD})
    assert started.status_code == 200, started.json()
    confirmed = phone.send(
        "POST",
        "/auth/mfa/totp/confirm",
        {"code": code_at(secret_of(started.json()), clock[0])},
    )
    assert confirmed.status_code == 200, confirmed.json()
    assert alive(phone)

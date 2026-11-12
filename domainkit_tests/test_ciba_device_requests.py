"""A new phone shows a QR code, and a phone the user bound scans it (ADR 0137).

The request is made for one key, and signed by it: only that key redeems
it. It names no account and reaches nobody: the phone that scans it, a
bound one, makes it its user's, and approves it.
"""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from domainkit import clock
from domainkit.conf import override_settings
from domainkit_tests.test_auth_http import bearer
from domainkit_tests.test_auth_login_origins import origins
from domainkit_tests.test_ciba import PHONE, App, Phone, app, sign_in_pushes
from domainkit_tests.test_devices import Device

__all__ = ["app"]  # The fixture the tests take.

NEW = ("192.0.2.44", 50002)
ASK = "/auth/app-sign-in/device"
REDEEM = "/auth/app-sign-in/device/token"
CLAIM = "/auth/app-sign-in/requests/claim"


def signed(
    client: TestClient, device: Device, path: str, payload: dict[str, Any]
) -> Any:  # noqa: ANN401
    """``POST path`` with ``payload``, signed by ``device``'s key: the response.

    Signed at the clock's time, which a test may freeze.
    """
    body = json.dumps(payload).encode()
    moment = int(clock.now().timestamp() * 1000)
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            **device.headers("POST", path, body, when=moment),
        },
    )


def ask(client: TestClient, device: Device) -> Any:  # noqa: ANN401
    """The new phone asks for a code to show: the response."""
    return signed(
        client, device, ASK, {"name": "New phone", "public_key": device.public()}
    )


def scan(phone: Phone, started: dict[str, Any]) -> Any:  # noqa: ANN401
    """``phone`` scans the new phone's QR code: the response."""
    return phone.send("POST", CLAIM, {"code": started["qr"]})


def redeem(client: TestClient, device: Device, handle: str) -> Any:  # noqa: ANN401
    """The new phone redeems its request, past the poll interval: the response."""
    with clock.freeze_time(clock.now() + timedelta(seconds=6)):
        return signed(
            client, device, REDEEM, {"request": handle, "public_key": device.public()}
        )


def answer(response: Any) -> tuple[int, str | None]:  # noqa: ANN401 - a response.
    """The status and the problem's code."""
    return response.status_code, response.json().get("code")


def devices_of(phone: Phone) -> int:
    """How many devices ``phone``'s user bound."""
    return len(phone.client.get("/auth/devices", headers=phone.headers).json())


def test_the_bound_phone_scans_a_new_one_which_is_bound_and_logged_in(
    app: App,
) -> None:
    """Shown by its code, scanned, seen by name and address, approved, bound."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new = app(NEW), Device()

    started = ask(client, new)
    assert started.status_code == 200, started.json()
    assert started.headers["Cache-Control"] == "no-store"
    assert set(started.json()) == {
        "request",
        "expires_in",
        "interval",
        "qr",
        "qr_expires_in",
    }
    assert phone.waiting() == []  # Nothing reaches the phone before it scans.
    assert sign_in_pushes() == []
    early = signed(
        client,
        new,
        REDEEM,
        {"request": started.json()["request"], "public_key": new.public()},
    )
    scanned = scan(phone, started.json())
    assert scanned.status_code == 200, scanned.json()
    shown = scanned.json()
    assert (shown["kind"], shown["channel"]) == ("device", "qr")
    assert shown["binding_message"] == "New phone"
    assert (shown["address"], shown["same_network"]) == (NEW[0], False)
    assert shown["match_required"] is False  # Scanning the code is the proof.
    approved = phone.approve(shown["id"])
    bound = redeem(client, new, started.json()["request"])

    assert answer(early) == (400, "authorization_pending")
    assert approved.status_code == 204, approved.json()
    assert bound.status_code == 201, bound.json()
    assert bound.json()["device"]["name"] == "New phone"
    new.id = bound.json()["device"]["id"]
    assert devices_of(phone) == 2
    assert origins()[-1] == ("device", UUID(new.id))
    listing = client.get(
        "/auth/tokens",
        headers={
            **bearer(bound.json()["access_token"]),
            **new.headers("GET", "/auth/tokens"),
        },
    )
    assert listing.status_code == 200, listing.json()  # The new phone manages logins.
    again = redeem(client, new, started.json()["request"])
    assert answer(again)[0] == 400  # It works once.
    assert devices_of(phone) == 2


def test_the_request_names_no_account(app: App) -> None:
    """An identifier is refused: a stranger cannot aim a request at anyone."""
    Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new = app(NEW), Device()

    named = signed(
        client,
        new,
        ASK,
        {"identifier": "alice", "name": "New phone", "public_key": new.public()},
    )

    assert named.status_code == 422


def test_the_key_binds_to_the_account_of_the_phone_that_scanned(app: App) -> None:
    """Whose the new phone becomes is the scanner's to say, not the asker's."""
    alice = Phone(app(PHONE, APPROVE_LOGINS=True))
    bob = Phone(app(("198.51.100.10", 50003)), "bob")
    client, new = app(NEW), Device()
    started = ask(client, new).json()

    shown = scan(bob, started).json()
    assert bob.approve(shown["id"]).status_code == 204
    bound = redeem(client, new, started["request"])

    assert bound.status_code == 201, bound.json()
    assert (devices_of(bob), devices_of(alice)) == (2, 1)


def test_a_code_is_scanned_once_and_only_in_time(app: App) -> None:
    """A second scan, or a scan past ``QR_TTL``, claims nothing."""
    alice = Phone(app(PHONE, APPROVE_LOGINS=True))
    bob = Phone(app(("198.51.100.10", 50003)), "bob")
    client = app(NEW)

    with clock.freeze_time("2026-11-11T10:00:00Z") as frozen:
        first = ask(client, Device()).json()
        assert scan(alice, first).status_code == 200
        frozen.tick(1)
        assert answer(scan(bob, first)) == (422, "qr_code_expired")
        late_key = Device()
        late = ask(client, late_key).json()
        frozen.tick(61)
        assert answer(scan(alice, late)) == (422, "qr_code_expired")
        assert answer(redeem(client, late_key, late["request"])) == (
            400,
            "expired_token",
        )


def test_on_another_network_the_scan_can_be_refused(app: App) -> None:
    """``QR_SAME_NETWORK = "require"``: a code shown elsewhere cannot be claimed."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True, QR_SAME_NETWORK="require"))
    client, new = app(NEW), Device()
    started = ask(client, new).json()

    refused = scan(phone, started)

    assert answer(refused) == (403, "different_network")
    assert phone.waiting() == []
    assert devices_of(phone) == 1


def test_only_the_key_that_asked_redeems(app: App) -> None:
    """A thief who learned the handle binds no key of their own."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new, thief = app(NEW), Device(), Device()
    started = ask(client, new).json()
    phone.approve(scan(phone, started).json()["id"])

    stolen = redeem(client, thief, started["request"])
    unsigned = client.post(
        REDEEM, json={"request": started["request"], "public_key": new.public()}
    )
    forged = signed(
        client,
        thief,
        REDEEM,
        {"request": started["request"], "public_key": new.public()},
    )
    own = redeem(client, new, started["request"])

    assert answer(stolen)[0] == 400
    assert answer(unsigned) == (401, "device_signature_refused")
    assert answer(forged) == (401, "device_signature_refused")
    assert own.status_code == 201, own.json()
    assert devices_of(phone) == 2


def test_a_request_the_key_did_not_sign_is_refused(app: App) -> None:
    """Unsigned, or signed by another key: 401, and no code is made."""
    client, new, other = app(NEW, APPROVE_LOGINS=True), Device(), Device()
    payload = {"name": "New phone", "public_key": new.public()}

    unsigned = client.post(ASK, json=payload)
    forged = signed(client, other, ASK, payload)
    garbage = signed(client, new, ASK, {**payload, "public_key": "bm90LWEta2V5"})

    assert answer(unsigned) == (401, "device_signature_refused")
    assert answer(forged) == (401, "device_signature_refused")
    assert answer(garbage) == (422, "unsupported_device_key")


def test_a_denied_request_binds_nothing(app: App) -> None:
    """The user scans, sees a phone they do not know, and says no."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new = app(NEW), Device()
    started = ask(client, new).json()

    assert phone.deny(scan(phone, started).json()["id"]).status_code == 204

    assert answer(redeem(client, new, started["request"])) == (400, "access_denied")
    assert devices_of(phone) == 1


def test_past_max_devices_the_approved_key_is_refused(app: App) -> None:
    """``DEVICES.MAX_DEVICES`` holds on this way in too."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new = app(NEW), Device()
    started = ask(client, new).json()
    phone.approve(scan(phone, started).json()["id"])

    with override_settings(DEVICES={"MAX_DEVICES": 1}):
        full = redeem(client, new, started["request"])

    assert answer(full) == (409, "too_many_keys")
    assert devices_of(phone) == 1


def test_a_user_turned_off_after_the_approval_gets_no_device(app: App) -> None:
    """Approved, then deactivated: nothing is bound, nobody logs in."""
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.contrib.auth.models import UserId
    from domainkit.management.base import run_async

    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new = app(NEW), Device()
    started = ask(client, new).json()
    phone.approve(scan(phone, started).json()["id"])

    async def turn_off() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(phone.user_id))
            user.is_active = False
            await uow.commit()

    run_async(turn_off())

    assert answer(redeem(client, new, started["request"]))[0] == 400

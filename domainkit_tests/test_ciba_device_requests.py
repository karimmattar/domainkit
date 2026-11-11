"""A new phone asks to be bound, and the user's bound phone approves (ADR 0137).

The request is made for one key, and signed by it: only that key redeems
it, and only a request a bound device signs approves it.
"""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from domainkit import clock
from domainkit.conf import override_settings
from domainkit_tests.test_auth_http import bearer, create_user
from domainkit_tests.test_auth_login_origins import origins
from domainkit_tests.test_ciba import PHONE, App, Phone, app
from domainkit_tests.test_devices import Device

__all__ = ["app"]  # The fixture the tests take.

NEW = ("192.0.2.44", 50002)
ASK = "/auth/app-sign-in/device"
REDEEM = "/auth/app-sign-in/device/token"


def signed(
    client: TestClient, device: Device, path: str, payload: dict[str, Any]
) -> Any:  # noqa: ANN401
    """``POST path`` with ``payload``, signed by ``device``'s key: the response."""
    body = json.dumps(payload).encode()
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            **device.headers("POST", path, body),
        },
    )


def ask(client: TestClient, device: Device, identifier: str = "alice") -> Any:  # noqa: ANN401
    """The new phone asks to be bound: the response."""
    return signed(
        client,
        device,
        ASK,
        {"identifier": identifier, "name": "New phone", "public_key": device.public()},
    )


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


def test_the_bound_phone_approves_a_new_one_which_is_bound_and_logged_in(
    app: App,
) -> None:
    """Asked with its key, shown by name, approved with the number, bound."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new = app(NEW), Device()

    started = ask(client, new)
    assert started.status_code == 200, started.json()
    assert started.headers["Cache-Control"] == "no-store"
    [waiting] = phone.waiting()
    assert (waiting["kind"], waiting["binding_message"]) == ("device", "New phone")
    assert (waiting["address"], waiting["match_required"]) == (NEW[0], True)
    early = signed(
        client,
        new,
        REDEEM,
        {"request": started.json()["request"], "public_key": new.public()},
    )
    approved = phone.approve(waiting["id"], match_code=started.json()["match_code"])
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


def test_only_the_key_that_asked_redeems(app: App) -> None:
    """A thief who learned the handle binds no key of their own."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True, NUMBER_MATCHING=False))
    client, new, thief = app(NEW), Device(), Device()
    started = ask(client, new).json()
    phone.approve(phone.waiting()[0]["id"])

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


def test_a_request_the_key_did_not_sign_asks_nobody(app: App) -> None:
    """Unsigned, or signed by another key: 401, and the phone is shown nothing."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, new, other = app(NEW), Device(), Device()
    payload = {"identifier": "alice", "name": "New phone", "public_key": new.public()}

    unsigned = client.post(ASK, json=payload)
    forged = signed(client, other, ASK, payload)
    garbage = signed(client, new, ASK, {**payload, "public_key": "bm90LWEta2V5"})

    assert answer(unsigned) == (401, "device_signature_refused")
    assert answer(forged) == (401, "device_signature_refused")
    assert answer(garbage) == (422, "unsupported_device_key")
    assert phone.waiting() == []


def test_whoever_is_named_the_answer_is_the_same(app: App) -> None:
    """Unknown, or with no bound phone: a request nobody can approve."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    create_user("bob")
    client = app(NEW)

    answers = [ask(client, Device(), who) for who in ("alice", "bob", "nobody")]

    assert [each.status_code for each in answers] == [200, 200, 200]
    assert len({frozenset(each.json()) for each in answers}) == 1
    assert len(phone.waiting()) == 1
    decoy, key = answers[2].json()["request"], Device()
    assert answer(redeem(client, key, decoy))[0] == 400


def test_a_denied_or_misnumbered_request_binds_nothing(app: App) -> None:
    """The user says no, or types another number: the key stays out."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    client, first, second = app(NEW), Device(), Device()

    denied = ask(client, first).json()
    phone.deny(phone.waiting()[0]["id"])
    wrong = ask(client, second).json()
    other = "00" if wrong["match_code"] != "00" else "01"
    phone.approve(phone.waiting()[0]["id"], match_code=other)

    assert answer(redeem(client, first, denied["request"])) == (400, "access_denied")
    assert answer(redeem(client, second, wrong["request"])) == (400, "access_denied")
    assert devices_of(phone) == 1


def test_past_max_devices_the_approved_key_is_refused(app: App) -> None:
    """``DEVICES.MAX_DEVICES`` holds on this way in too."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True, NUMBER_MATCHING=False))
    client, new = app(NEW), Device()
    started = ask(client, new).json()
    phone.approve(phone.waiting()[0]["id"])

    with override_settings(DEVICES={"MAX_DEVICES": 1}):
        full = redeem(client, new, started["request"])

    assert answer(full) == (409, "too_many_keys")
    assert devices_of(phone) == 1


def test_a_user_turned_off_after_the_approval_gets_no_device(app: App) -> None:
    """Approved, then deactivated: nothing is bound, nobody logs in."""
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.contrib.auth.models import UserId
    from domainkit.management.base import run_async

    phone = Phone(app(PHONE, APPROVE_LOGINS=True, NUMBER_MATCHING=False))
    client, new = app(NEW), Device()
    started = ask(client, new).json()
    phone.approve(phone.waiting()[0]["id"])

    async def turn_off() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(phone.user_id))
            user.is_active = False
            await uow.commit()

    run_async(turn_off())

    assert answer(redeem(client, new, started["request"]))[0] == 400

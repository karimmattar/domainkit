"""A bound device or a passkey confirms one act (ADR 0144, user report A6).

The mobile app asks for an authorization of an act it makes itself,
shows it, and confirms it with its digest, signed by its bound device
(``POST /devices/authorizations/{id}``): ``hwk`` and ``phone``. A page
confirms one with a passkey, answering a challenge made for that
authorization alone (``POST /passkeys/authorizations/{id}``): ``hwk`` and
``mfa``, never ``phone``. The act is the sample's rename, spent once.
"""

import base64
import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import (
    Action,
    on_action_confirmation,
    register_action,
)
from domainkit.contrib.auth.apps import action_problems
from domainkit.contrib.auth.tables import authorizations
from domainkit.contrib.devices.authorizations import PASSKEY_CONFIRMATION
from domainkit.contrib.devices.webauthn import b64decode, verify_signature
from domainkit.db import connections
from domainkit.ddd import fingerprint
from domainkit.management.base import run_async
from domainkit_tests import sample_action_routes as acts
from domainkit_tests.conftest import ExitStackClients, Migrated
from domainkit_tests.support.auth import audited, bearer, create_user, login
from domainkit_tests.test_devices import (
    DEVICES,
    SETTINGS,
    Authenticator,
    Device,
    add_passkey,
    bind,
)

ROUTES = "domainkit_tests.sample_devices_action_routes.router"
PHONE_ONLY = {**DEVICES.settings["AUTH"], "AUTHORIZATIONS": {"METHODS": ["phone"]}}
"""``AUTH`` with every act asking the phone."""
type App = Callable[..., TestClient]


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(**DEVICES)``: devices and the sample acts, the rename registered."""
    with migrated(DEVICES), ExitStackClients() as clients:
        clients.enter(override_settings(ROOT_ROUTER=ROUTES))
        register_action(acts.RENAME, domain="shop")

        def make(**devices: Any) -> TestClient:  # noqa: ANN401 - DEVICES keys.
            clients.enter(override_settings(DEVICES=devices))
            return clients.client(("testclient", 50000))

        yield make


class Session:
    """A login of a user at the sample API: the app's, or a page's."""

    def __init__(self, client: TestClient, user_id: UUID, username: str) -> None:
        """Log ``username`` in: a session of its own."""
        self.client = client
        self.user_id = user_id
        self.username = username
        self.headers = bearer(login(client, username)["access_token"])

    @classmethod
    def of_new_user(cls, client: TestClient, username: str = "ann") -> "Session":
        """``username`` made and logged in."""
        return cls(client, create_user(username), username)

    def again(self) -> "Session":
        """Another login of the same user: another session."""
        return Session(self.client, self.user_id, self.username)

    def ask(self, name: str = "Al") -> Any:  # noqa: ANN401 - a response.
        """Ask for an authorization of renaming oneself ``name``."""
        return self.client.post(
            "/me/name/authorization", json={"first_name": name}, headers=self.headers
        )

    def asked(self, name: str = "Al") -> str:
        """The id of a new authorization of renaming oneself ``name``."""
        answer = self.ask(name)
        assert answer.status_code == 201, answer.json()
        authorization_id: str = answer.json()["id"]
        return authorization_id

    def digest(self, name: str = "Al") -> str:
        """The act's digest, as the app works it out from the act it shows."""
        return fingerprint(
            "accounts.rename", {"first_name": name, "user_id": str(self.user_id)}
        )

    def confirm(self, authorization_id: str, device: Device | None, digest: str) -> Any:  # noqa: ANN401 - a response.
        """Confirm it with ``digest``, signed by ``device`` (unsigned if None)."""
        path = f"/auth/devices/authorizations/{authorization_id}"
        body = json.dumps({"digest": digest}, separators=(",", ":")).encode()
        signing = {} if device is None else device.headers("POST", path, body)
        return self.client.post(
            path,
            content=body,
            headers={**self.headers, "Content-Type": "application/json", **signing},
        )

    def options(self, authorization_id: str) -> Any:  # noqa: ANN401 - a response.
        """The options to confirm it with a passkey."""
        return self.client.post(
            f"/auth/passkeys/authorizations/{authorization_id}/options",
            headers=self.headers,
        )

    def answer(self, authorization_id: str, credential: dict[str, Any]) -> Any:  # noqa: ANN401
        """Confirm it with a passkey's ``credential``."""
        return self.client.post(
            f"/auth/passkeys/authorizations/{authorization_id}",
            json={"credential": credential},
            headers=self.headers,
        )

    def rename(self, name: str, authorization: str) -> Any:  # noqa: ANN401
        """Rename oneself ``name``, sending ``authorization``."""
        return self.client.post(
            "/me/name",
            json={"first_name": name},
            headers={**self.headers, "Action-Authorization": authorization},
        )


def rows() -> list[dict[str, Any]]:
    """Every stored authorization, oldest first."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                select(authorizations).order_by(authorizations.c.created_at)
            )
            return [dict(row._mapping) for row in found]

    return run_async(read())


def row_of(authorization_id: str) -> dict[str, Any]:
    """The stored authorization ``authorization_id``."""
    [found] = [row for row in rows() if str(row["id"]) == authorization_id]
    return found


def code_of(response: Any) -> tuple[int, str]:  # noqa: ANN401 - a response.
    """The status and the error code of ``response``."""
    return response.status_code, response.json().get("code")


def with_device(client: TestClient, username: str = "ann") -> tuple[Session, Device]:
    """The app's session of a new user, and the device it bound."""
    app_session = Session.of_new_user(client, username)
    device = Device()
    assert bind(client, app_session.headers, device).status_code == 201
    return app_session, device


def test_the_apps_own_act_is_confirmed_with_its_digest_and_spent_once(
    app: App,
) -> None:
    """The app asks, shows, signs the digest, and acts: once; ``hwk`` and ``phone``."""
    client = app()
    ann, device = with_device(client)
    authorization_id = ann.asked("Al")

    confirmed = ann.confirm(authorization_id, device, ann.digest("Al"))
    authorization = confirmed.json()["authorization"]
    renamed = ann.rename("Al", authorization)
    again = ann.rename("Al", authorization)

    assert confirmed.status_code == 200, confirmed.json()
    assert confirmed.headers["cache-control"] == "no-store"
    assert authorization.split(".")[0] == authorization_id
    assert confirmed.json()["expires_at"]
    assert renamed.status_code == 200, renamed.json()
    assert code_of(again) == (403, "action_not_authorized")
    row = row_of(authorization_id)
    assert row["amr"] == "hwk phone"
    assert str(row["device_id"]) == device.id
    [authorized] = audited("auth.action_authorized")
    assert authorized["details"]["device_id"] == device.id


def test_a_digest_other_than_the_acts_is_refused(app: App) -> None:
    """409 ``authorization_changed``: the app shows another act; it stays to confirm."""
    client = app()
    ann, device = with_device(client)
    authorization_id = ann.asked("Al")

    changed = ann.confirm(authorization_id, device, ann.digest("Bo"))
    malformed = ann.confirm(authorization_id, device, "not a digest")
    confirmed = ann.confirm(authorization_id, device, ann.digest("Al"))

    assert code_of(changed) == (409, "authorization_changed")
    assert malformed.status_code == 422
    assert confirmed.status_code == 200, confirmed.json()


def test_another_users_device_or_authorization_is_refused(app: App) -> None:
    """Bob's device signs nothing of ann's, and his login finds none of hers."""
    client = app()
    ann, _ = with_device(client)
    bob, bobs = with_device(client, "bob")
    authorization_id = ann.asked("Al")
    digest = ann.digest("Al")

    with_her_login = ann.confirm(authorization_id, bobs, digest)
    with_his_login = bob.confirm(authorization_id, bobs, digest)

    assert code_of(with_her_login) == (401, "device_signature_refused")
    assert code_of(with_his_login) == (404, "authorization_not_found")
    assert row_of(authorization_id)["confirmed_at"] is None


def test_an_unsigned_or_replayed_request_is_refused(app: App) -> None:
    """No device signature: 401; the same signed request again: 401."""
    client = app()
    ann, device = with_device(client)
    authorization_id = ann.asked("Al")
    path = f"/auth/devices/authorizations/{authorization_id}"
    body = json.dumps({"digest": ann.digest("Al")}).encode()
    headers = {
        **ann.headers,
        "Content-Type": "application/json",
        **device.headers("POST", path, body),
    }

    unsigned = ann.confirm(authorization_id, None, ann.digest("Al"))
    signed = client.post(path, content=body, headers=headers)
    replayed = client.post(path, content=body, headers=headers)

    assert code_of(unsigned) == (401, "device_signature_refused")
    assert signed.status_code == 200, signed.json()
    assert code_of(replayed) == (401, "device_signature_refused")


def test_only_the_apps_own_login_confirms_by_its_device(app: App) -> None:
    """A page's authorization is not the app's to confirm: 404, unconfirmed.

    The app confirms an act it makes and shows itself, from the login that
    asked and spends it. Another session's goes to the phone, which shows
    who asked.
    """
    client = app()
    ann, device = with_device(client)
    page = ann.again()
    authorization_id = page.asked("Al")

    by_the_app = ann.confirm(authorization_id, device, ann.digest("Al"))

    assert code_of(by_the_app) == (404, "authorization_not_found")
    assert row_of(authorization_id)["confirmed_at"] is None


def test_the_devices_proof_is_kept_checks_again_and_outlives_the_device(
    app: App,
) -> None:
    """The key, what it signed, and its signature stay on auth's row, unbound or not.

    The kept signature holds for the kept key over the kept message, which
    names the authorization and covers the body sent: compact JSON of the
    row's digest.
    """
    client = app()
    ann, device = with_device(client)
    authorization_id = ann.asked("Al")
    assert ann.confirm(authorization_id, device, ann.digest("Al")).status_code == 200
    kept = row_of(authorization_id)
    other = ann.again()

    unbound = client.delete(f"/auth/devices/{device.id}", headers=other.headers)
    after = row_of(authorization_id)

    assert unbound.status_code == 204
    assert client.get("/auth/devices", headers=other.headers).json() == []
    assert after == kept
    assert after["public_key"] == b64decode(device.public())
    assert after["algorithm"] == -7
    assert verify_signature(
        after["algorithm"], after["public_key"], after["signature"], after["signed"]
    )
    version, method, target, moment, body_digest = after["signed"].split(b"\n")
    body = json.dumps({"digest": after["digest"]}, separators=(",", ":")).encode()
    assert (version, method) == (b"domainkit-device-v1", b"POST")
    assert target == f"/auth/devices/authorizations/{authorization_id}".encode()
    assert int(moment) == round(after["signed_at"].timestamp() * 1000)
    assert body_digest == base64.urlsafe_b64encode(
        hashlib.sha256(body).digest()
    ).rstrip(b"=")


def test_a_passkey_confirms_with_a_challenge_made_for_the_authorization(
    app: App,
) -> None:
    """Options, assertion, the code, the act: once; ``hwk`` and ``mfa``."""
    client = app(**SETTINGS)
    ann = Session.of_new_user(client)
    key = Authenticator()
    assert add_passkey(client, ann.headers, key).status_code == 201
    authorization_id = ann.asked("Al")

    options = ann.options(authorization_id)
    linked = row_of(authorization_id)["passkey_challenge"]
    credential = key.get(options.json())
    confirmed = ann.answer(authorization_id, credential)
    authorization = confirmed.json()["authorization"]
    replayed = ann.answer(authorization_id, credential)
    renamed = ann.rename("Al", authorization)
    again = ann.rename("Al", authorization)

    assert options.status_code == 200, options.json()
    assert options.json()["userVerification"] == "required"
    assert len(options.json()["allowCredentials"]) == 1
    assert linked == options.json()["challenge"]
    assert confirmed.status_code == 200, confirmed.json()
    assert confirmed.headers["cache-control"] == "no-store"
    assert code_of(replayed) == (401, "passkey_refused")
    assert renamed.status_code == 200, renamed.json()
    assert code_of(again) == (403, "action_not_authorized")
    row = row_of(authorization_id)
    assert (row["amr"], row["device_id"]) == ("hwk mfa", None)


def test_a_challenge_for_another_authorization_or_purpose_is_refused(
    app: App,
) -> None:
    """Each is spent where it is answered; only the authorization's own confirms it."""
    client = app(**SETTINGS)
    ann = Session.of_new_user(client)
    key = Authenticator()
    add_passkey(client, ann.headers, key)
    first, second, unasked = ann.asked("Al"), ann.asked("Bo"), ann.asked("Cy")
    for_first = ann.options(first).json()
    for_second = ann.options(second).json()
    for_step_up = client.post(
        "/auth/passkeys/step-up/options", headers=ann.headers
    ).json()

    crossed = ann.answer(second, key.get(for_first))
    spent = ann.answer(first, key.get(for_first))
    stepping = ann.answer(second, key.get(for_step_up))
    as_step_up = client.post(
        "/auth/passkeys/step-up",
        json={"credential": key.get(for_second)},
        headers=ann.headers,
    )
    never_made = ann.answer(unasked, key.get(ann.options(second).json()))
    confirmed = ann.answer(second, key.get(ann.options(second).json()))

    for refused in (crossed, spent, stepping, as_step_up, never_made):
        assert code_of(refused) == (401, "passkey_refused")
    assert confirmed.status_code == 200, confirmed.json()
    amr = [row_of(each)["amr"] for each in (first, second, unasked)]
    assert amr == [None, "hwk mfa", None]


def test_a_passkey_never_proves_the_phone_and_the_apps_own_act_does(
    app: App,
) -> None:
    """Every act asks ``phone``: a passkey is refused (409); the app's act is not."""
    client = app(**SETTINGS)
    ann, device = with_device(client)
    key = Authenticator()
    add_passkey(client, ann.headers, key)
    with override_settings(AUTH=PHONE_ONLY):
        authorization_id = ann.asked("Al")
        shown = client.get(
            f"/auth/authorizations/{authorization_id}", headers=ann.headers
        ).json()
        options = ann.options(authorization_id)
        answered = ann.answer(authorization_id, key.get({"challenge": "x" * 43}))
        confirmed = ann.confirm(authorization_id, device, ann.digest("Al"))
        renamed = ann.rename("Al", confirmed.json()["authorization"])

    assert shown["methods"] == ["phone"]
    assert code_of(options) == (409, "authorization_needs_other_factors")
    assert code_of(answered) == (409, "authorization_needs_other_factors")
    assert row_of(authorization_id)["passkey_challenge"] is None
    assert confirmed.status_code == 200, confirmed.json()
    assert renamed.status_code == 200, renamed.json()


def test_another_session_cannot_use_the_passkey_routes(app: App) -> None:
    """Another login of the same user: 404, and the challenge stays for the session."""
    client = app(**SETTINGS)
    ann = Session.of_new_user(client)
    key = Authenticator()
    add_passkey(client, ann.headers, key)
    other = ann.again()
    authorization_id = ann.asked("Al")
    options = ann.options(authorization_id).json()

    their_options = other.options(authorization_id)
    their_answer = other.answer(authorization_id, key.get(options))
    own_answer = ann.answer(authorization_id, key.get(options))

    assert code_of(their_options) == (404, "authorization_not_found")
    assert code_of(their_answer) == (404, "authorization_not_found")
    assert own_answer.status_code == 200, own_answer.json()
    assert row_of(authorization_id)["passkey_challenge"] == options["challenge"]


def test_asking_needs_a_bound_device_or_a_passkey_and_the_phone_a_device(
    app: App,
) -> None:
    """``available`` follows what the user holds now: ``409 no_second_factor`` else.

    A user with nothing to confirm with cannot ask; a bound device or a
    passkey lets them. Where every act asks the phone, a passkey does
    not, and the device does. Without ``RP_ID``, a passkey confirms
    nothing.
    """
    client = app(**SETTINGS)
    ann = Session.of_new_user(client)
    bob = Session.of_new_user(client, "bob")
    add_passkey(client, bob.headers, Authenticator())

    bare = ann.ask()
    bind(client, ann.headers, Device())
    with_device = ann.ask()
    with_passkey = bob.ask()
    with override_settings(AUTH=PHONE_ONLY):
        passkey_for_phone = bob.ask()
        device_for_phone = ann.ask()
    with override_settings(DEVICES={}):
        passkeys_off = run_async(PASSKEY_CONFIRMATION(bob.user_id))

    assert code_of(bare) == (409, "no_second_factor")
    assert with_device.status_code == 201, with_device.json()
    assert with_passkey.status_code == 201, with_passkey.json()
    assert code_of(passkey_for_phone) == (409, "no_second_factor")
    assert device_for_phone.status_code == 201, device_for_phone.json()
    assert passkeys_off is False


def test_devices_registers_both_ways_and_proves_the_phone_for_check(
    app: App,
) -> None:
    """``auth.E108`` passes an act asking the phone alone: the app's act proves it."""
    app()
    register_action(
        Action(
            "accounts.close_by_phone",
            command=acts.Rename,
            subject=lambda command: f"auth.user:{command.user_id}",
            show=acts.show_rename,
            methods=["phone"],
        ),
        domain="shop",
    )

    ways = {hook.call.name: hook.call.methods for hook in on_action_confirmation}
    problems = action_problems(FastAPI())

    assert ways == {
        "code": frozenset({"otp", "pwd"}),
        "device": frozenset({"hwk", "phone"}),
        "passkey": frozenset({"hwk", "mfa"}),
    }
    assert [problem.id for problem in problems] == []

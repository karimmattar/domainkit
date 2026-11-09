"""Passkeys and bound devices, end to end, with a software authenticator."""

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.devices.binding import signed_message
from domainkit.contrib.devices.push import notify_user
from domainkit.contrib.devices.webauthn import AT, BE, BS, UP, UV, b64encode
from domainkit.contrib.messaging.tables import messages
from domainkit.contrib.notifications.delivery import PUSH_TASK
from domainkit.contrib.notifications.events import PushTokenExpired, token_digest
from domainkit.db import connections
from domainkit.ddd import event_bus
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    IDENTITY,
    IDENTITY_DOMAINS,
    ExitStackClients,
    Migrated,
    Shape,
)
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user, login
from domainkit_tests.test_auth_login_origins import origins

DEVICES = Shape(
    "devices",
    "devices",
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [
            *IDENTITY_DOMAINS,
            "domainkit.contrib.notifications",
            "domainkit.contrib.devices",
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_devices_routes.router",
    },
)
RP_ID = "example.com"
ORIGIN = "https://app.example.com"
SETTINGS = {"RP_ID": RP_ID, "ORIGINS": (ORIGIN,)}
type App = Callable[..., TestClient]


def cbor(value: Any) -> bytes:  # noqa: ANN401 - CBOR holds anything.
    """``value`` in CBOR, definite lengths (what authenticators write)."""

    def head(major: int, argument: int) -> bytes:
        if argument < 24:
            return bytes([major << 5 | argument])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if argument < 1 << (8 * size):
                return bytes([major << 5 | info]) + argument.to_bytes(size, "big")
        raise ValueError(argument)

    if value is None or isinstance(value, bool):
        return bytes([{False: 0xF4, True: 0xF5, None: 0xF6}[value]])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        return head(3, len(value.encode())) + value.encode()
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(each) for each in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(
            cbor(key) + cbor(each) for key, each in value.items()
        )
    raise TypeError(value)


class Authenticator:
    """A passkey in software: a P-256 key, a credential id, a counter."""

    def __init__(self, *, counts: bool = True) -> None:
        """A new credential; ``counts``: its counter moves (else stays 0)."""
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.counts = counts
        self.count = 0

    def cose(self) -> dict[int, Any]:
        """The public key, as COSE writes it."""
        numbers = self.key.public_key().public_numbers()
        return {
            1: 2,
            3: -7,
            -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }

    def auth_data(self, flags: int, rp_id: str = RP_ID, *, attested: bool) -> bytes:
        """Authenticator data, the credential attested or not."""
        if self.counts:
            self.count += 1
        data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + self.count.to_bytes(4, "big")
        )
        if attested:
            data += bytes(16) + len(self.credential_id).to_bytes(2, "big")
            data += self.credential_id + cbor(self.cose())
        return data

    def create(
        self,
        options: dict[str, Any],
        *,
        flags: int = UP | UV | AT | BE | BS,
        origin: str = ORIGIN,
        kind: str = "webauthn.create",
        rp_id: str = RP_ID,
    ) -> dict[str, Any]:
        """``navigator.credentials.create()``, as ``toJSON()`` writes it."""
        client_data = client_json(kind, options["challenge"], origin)
        attestation = cbor(
            {
                "fmt": "none",
                "attStmt": {},
                "authData": self.auth_data(flags, rp_id, attested=True),
            }
        )
        return {
            "id": b64encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64encode(client_data),
                "attestationObject": b64encode(attestation),
                "transports": ["internal"],
            },
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        flags: int = UP | UV,
        origin: str = ORIGIN,
        user_handle: bytes | None = None,
        sign: bytes | None = None,
    ) -> dict[str, Any]:
        """``navigator.credentials.get()``, as ``toJSON()`` writes it."""
        client_data = client_json("webauthn.get", options["challenge"], origin)
        data = self.auth_data(flags, attested=False)
        signature = self.key.sign(
            sign or data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        response = {
            "clientDataJSON": b64encode(client_data),
            "authenticatorData": b64encode(data),
            "signature": b64encode(signature),
        }
        if user_handle is not None:
            response["userHandle"] = b64encode(user_handle)
        return {"id": b64encode(self.credential_id), "response": response}


def client_json(kind: str, challenge: str, origin: str) -> bytes:
    """Client data, as a browser writes it."""
    return json.dumps(
        {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
    ).encode()


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(**DEVICES)``: a client of the project with devices installed."""
    with migrated(DEVICES), ExitStackClients() as clients:

        def make(**devices: Any) -> TestClient:  # noqa: ANN401 - DEVICES keys.
            clients.enter(override_settings(DEVICES=devices))
            return clients.client(("testclient", 50000))

        yield make


def signed_in(client: TestClient, username: str = "ann") -> dict[str, str]:
    """``username`` made and logged in: the headers of their requests."""
    create_user(username)
    return bearer(login(client, username)["access_token"])


def add_passkey(
    client: TestClient,
    headers: dict[str, str],
    key: Authenticator,
    **create: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401 - a response.
    """Register ``key`` for the user: the registration's response."""
    options = client.post(
        "/auth/passkeys/register/options", json={"password": PASSWORD}, headers=headers
    )
    assert options.status_code == 200, options.json()
    return client.post(
        "/auth/passkeys/register",
        json={"credential": key.create(options.json(), **create), "name": "Laptop"},
        headers=headers,
    )


def passkey_login(client: TestClient, key: Authenticator, **get: Any) -> Any:  # noqa: ANN401
    """Log in with ``key``: the response."""
    options = client.post("/auth/passkeys/login/options").json()
    return client.post(
        "/auth/passkeys/login", json={"credential": key.get(options, **get)}
    )


def test_a_passkey_logs_in_with_no_password_and_no_second_factor(app: App) -> None:
    """Registered after the password; the login gives tokens; the list shows it."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    key = Authenticator()

    added = add_passkey(client, headers, key)
    tokens = passkey_login(client, key)

    assert added.status_code == 201, added.json()
    assert added.json()["backed_up"] is True
    assert tokens.status_code == 200, tokens.json()
    assert tokens.headers["cache-control"] == "no-store"
    me = client.get("/auth/me", headers=bearer(tokens.json()["access_token"]))
    assert me.json()["username"] == "ann"
    assert origins()[-1] == ("passkey", None)
    listed = client.get("/auth/passkeys", headers=headers).json()
    assert [each["name"] for each in listed] == ["Laptop"]
    assert listed[0]["last_used_at"] is not None


def test_the_options_follow_webauthn(app: App) -> None:
    """Resident keys, users verified, no attestation; held keys excluded."""
    client = app(**SETTINGS, RP_NAME="Acme")
    headers = signed_in(client)
    add_passkey(client, headers, Authenticator())

    created = client.post(
        "/auth/passkeys/register/options", json={"password": PASSWORD}, headers=headers
    ).json()
    requested = client.post("/auth/passkeys/step-up/options", headers=headers).json()

    assert created["rp"] == {"id": RP_ID, "name": "Acme"}
    assert created["authenticatorSelection"]["userVerification"] == "required"
    assert created["attestation"] == "none"
    assert [p["alg"] for p in created["pubKeyCredParams"]] == [-7, -8, -257]
    assert len(created["excludeCredentials"]) == 1
    assert created["timeout"] == 300_000
    assert requested["rpId"] == RP_ID
    assert len(requested["allowCredentials"]) == 1


def test_adding_a_passkey_needs_the_user_again(app: App) -> None:
    """No password and no step-up: 401; a wrong password: 400."""
    client = app(**SETTINGS)
    headers = signed_in(client)

    bare = client.post("/auth/passkeys/register/options", json={}, headers=headers)
    wrong = client.post(
        "/auth/passkeys/register/options", json={"password": "nope"}, headers=headers
    )

    assert (bare.status_code, bare.json()["code"]) == (401, "reauthentication_required")
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_password")


@pytest.mark.parametrize(
    "create",
    [
        {"origin": "https://evil.example"},
        {"rp_id": "evil.example"},
        {"kind": "webauthn.get"},
        {"flags": UP | AT},
        {"flags": UV | AT},
        {"flags": UP | UV},
        {"flags": UP | UV | AT | BS},
    ],
)
def test_registrations_that_do_not_hold_are_refused(
    app: App, create: dict[str, Any]
) -> None:
    """Another origin, site, or ceremony; the user absent or unverified; no key."""
    client = app(**SETTINGS)
    headers = signed_in(client)

    refused = add_passkey(client, headers, Authenticator(), **create)

    assert (refused.status_code, refused.json()["code"]) == (401, "passkey_refused")
    assert client.get("/auth/passkeys", headers=headers).json() == []


def test_a_challenge_works_once_for_its_purpose_and_user(app: App) -> None:
    """Used, another user's, a login's: refused; the same passkey twice too."""
    client = app(**SETTINGS)
    ann = signed_in(client)
    bob = signed_in(client, "bob")
    key = Authenticator()
    options = client.post(
        "/auth/passkeys/register/options", json={"password": PASSWORD}, headers=ann
    ).json()
    credential = key.create(options)
    login_options = client.post("/auth/passkeys/login/options").json()

    as_bob = client.post(
        "/auth/passkeys/register",
        json={"credential": credential},
        headers=bob,
    )
    again = client.post(
        "/auth/passkeys/register", json={"credential": credential}, headers=ann
    )
    for_login = client.post(
        "/auth/passkeys/register",
        json={"credential": key.create(login_options)},
        headers=ann,
    )
    added = add_passkey(client, ann, key)
    twice = add_passkey(client, ann, key)

    for refused in (as_bob, again, for_login, twice):
        assert refused.json()["code"] == "passkey_refused"
    assert added.status_code == 201


def test_logins_that_do_not_hold_are_refused(app: App) -> None:
    """Unknown key, wrong signature, another origin, another handle, no user."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    key = Authenticator()
    add_passkey(client, headers, key)

    for refused in (
        passkey_login(client, Authenticator()),
        passkey_login(client, key, sign=b"something else"),
        passkey_login(client, key, origin="https://evil.example"),
        passkey_login(client, key, user_handle=os.urandom(16)),
        passkey_login(client, key, flags=UP),
    ):
        assert (refused.status_code, refused.json()["code"]) == (401, "passkey_refused")
    assert passkey_login(client, key).status_code == 200


def test_a_counter_that_goes_back_means_a_copied_key(app: App) -> None:
    """A count no later than the last is refused; a key that never counts works."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    key = Authenticator()
    add_passkey(client, headers, key)
    assert passkey_login(client, key).status_code == 200

    key.count = 0  # The copy counts from where the original was.
    copied = passkey_login(client, key)
    steady = Authenticator(counts=False)
    add_passkey(client, headers, steady)

    assert copied.json()["code"] == "passkey_refused"
    assert passkey_login(client, steady).status_code == 200
    assert passkey_login(client, steady).status_code == 200


def test_an_inactive_users_passkey_logs_no_one_in(app: App) -> None:
    """Deactivated: refused, as a password login would be."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    key = Authenticator()
    add_passkey(client, headers, key, flags=UP | UV | AT)

    async def deactivate() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("ann")
            assert user is not None
            user.set_active(False)
            await uow.commit()

    run_async(deactivate())

    assert passkey_login(client, key).json()["code"] == "passkey_refused"


def test_a_passkey_steps_up_for_whatever_step_up_asks(app: App) -> None:
    """Before: 401 step_up_required; after the passkey: allowed, amr mfa."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    key = Authenticator()
    add_passkey(client, headers, key)
    before = client.post("/payees", headers=headers)

    options = client.post("/auth/passkeys/step-up/options", headers=headers).json()
    stepped = client.post(
        "/auth/passkeys/step-up", json={"credential": key.get(options)}, headers=headers
    )
    after = client.post("/payees", headers=headers)

    assert before.json()["code"] == "step_up_required"
    assert stepped.status_code == 200, stepped.json()
    assert stepped.json()["amr"] == ["hwk", "mfa"]
    assert after.status_code == 200, after.json()


def test_a_step_up_takes_only_the_users_own_passkey(app: App) -> None:
    """Bob's passkey answering ann's challenge is refused."""
    client = app(**SETTINGS)
    ann = signed_in(client)
    bob = signed_in(client, "bob")
    bobs = Authenticator()
    add_passkey(client, bob, bobs)

    options = client.post("/auth/passkeys/step-up/options", headers=ann).json()
    refused = client.post(
        "/auth/passkeys/step-up", json={"credential": bobs.get(options)}, headers=ann
    )

    assert refused.json()["code"] == "passkey_refused"
    assert client.post("/payees", headers=ann).status_code == 401


def test_removing_a_passkey_and_the_limit(app: App) -> None:
    """At MAX_PASSKEYS: 409 too_many_keys; removed: gone; removed twice: 404."""
    client = app(**SETTINGS, MAX_PASSKEYS=1)
    headers = signed_in(client)
    add_passkey(client, headers, Authenticator())

    full = client.post(
        "/auth/passkeys/register/options", json={"password": PASSWORD}, headers=headers
    )
    passkey_id = client.get("/auth/passkeys", headers=headers).json()[0]["id"]
    removed = client.delete(f"/auth/passkeys/{passkey_id}", headers=headers)
    again = client.delete(f"/auth/passkeys/{passkey_id}", headers=headers)

    assert (full.status_code, full.json()["code"]) == (409, "too_many_keys")
    assert removed.status_code == 204
    assert (again.status_code, again.json()["code"]) == (404, "passkey_not_found")


def test_without_a_relying_party_there_are_no_passkeys(app: App) -> None:
    """No RP_ID: 404 passkeys_off."""
    client = app()

    answer = client.post("/auth/passkeys/login/options")

    assert (answer.status_code, answer.json()["code"]) == (404, "passkeys_off")


# Bound devices.


class Device:
    """A phone's key: P-256 or Ed25519, signing requests."""

    def __init__(self, kind: str = "p256") -> None:
        """A new key."""
        self.key: Any = (
            ec.generate_private_key(ec.SECP256R1())
            if kind == "p256"
            else ed25519.Ed25519PrivateKey.generate()
        )
        self.id = ""

    def public(self, *, raw: bool = False) -> str:
        """The public key: SubjectPublicKeyInfo, or the raw point."""
        if raw:
            return b64encode(
                self.key.public_key().public_bytes(
                    serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint,
                )
            )
        return b64encode(
            self.key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    def headers(
        self, method: str, target: str, body: bytes = b"", *, when: int | None = None
    ) -> dict[str, str]:
        """The headers that sign a request."""
        moment = when if when is not None else int(time.time() * 1000)
        message = signed_message(method, target, moment, body)
        signature = (
            self.key.sign(message, ec.ECDSA(hashes.SHA256()))
            if isinstance(self.key, ec.EllipticCurvePrivateKey)
            else self.key.sign(message)
        )
        return {
            "X-Device-Id": self.id,
            "X-Device-Time": str(moment),
            "X-Device-Signature": b64encode(signature),
        }


def bind(
    client: TestClient,
    headers: dict[str, str],
    device: Device,
    **body: Any,  # noqa: ANN401 - the request's fields.
) -> Any:  # noqa: ANN401 - a response.
    """Bind ``device`` to the user: the response."""
    answer = client.post(
        "/auth/devices",
        json={
            "name": "Phone",
            "public_key": device.public(),
            "password": PASSWORD,
            **body,
        },
        headers=headers,
    )
    if answer.status_code == 201:
        device.id = answer.json()["id"]
    return answer


def send(
    client: TestClient,
    headers: dict[str, str],
    device: Device,
    payload: dict[str, Any],
    **sign: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401 - a response.
    """A transfer, signed by ``device``."""
    body = json.dumps(payload).encode()
    return client.post(
        "/transfers",
        content=body,
        headers={
            **headers,
            "Content-Type": "application/json",
            **device.headers("POST", "/transfers", body, **sign),
        },
    )


@pytest.mark.parametrize("kind", ["p256", "ed25519"])
def test_a_bound_device_signs_requests_once_each(app: App, kind: str) -> None:
    """Signed: allowed, the body read again; the same signature twice: refused."""
    client = app()
    headers = signed_in(client)
    device = Device(kind)
    assert bind(client, headers, device).status_code == 201

    body = json.dumps({"amount": "10.00"}).encode()
    signing = device.headers("POST", "/transfers", body)
    first = client.post(
        "/transfers",
        content=body,
        headers={**headers, **signing, "Content-Type": "application/json"},
    )
    replayed = client.post(
        "/transfers",
        content=body,
        headers={**headers, **signing, "Content-Type": "application/json"},
    )

    assert first.status_code == 200, first.json()
    assert first.json() == {"sent": {"amount": "10.00"}}
    assert (replayed.status_code, replayed.json()["code"]) == (
        401,
        "device_signature_refused",
    )


def test_signatures_that_do_not_hold_are_refused(app: App) -> None:
    """Another body, an old time, no headers, garbage, another user's device."""
    client = app()
    ann = signed_in(client)
    bob = signed_in(client, "bob")
    device = Device()
    bind(client, ann, device)
    body = json.dumps({"amount": "10.00"}).encode()
    tampered = device.headers("POST", "/transfers", body)

    refusals = [
        client.post(
            "/transfers",
            content=b'{"amount": "9999.00"}',
            headers={**ann, **tampered, "Content-Type": "application/json"},
        ),
        send(client, ann, device, {"a": 1}, when=int(time.time() * 1000) - 600_000),
        client.post("/transfers", json={"a": 1}, headers=ann),
        client.post(
            "/transfers",
            json={"a": 1},
            headers={
                **ann,
                "X-Device-Id": device.id,
                "X-Device-Time": "soon",
                "X-Device-Signature": "x",
            },
        ),
        client.post(
            "/transfers",
            json={"a": 1},
            headers={
                **ann,
                **device.headers("POST", "/transfers"),
                "X-Device-Signature": "A" * 300,
            },
        ),
        send(client, bob, device, {"a": 1}),
    ]

    for refused in refusals:
        assert refused.json()["code"] == "device_signature_refused", refused.json()


def test_a_device_steps_up_its_user(app: App) -> None:
    """A signed POST /devices/step-up: amr hwk, which stands for the code."""
    client = app()
    headers = signed_in(client)
    device = Device()
    bind(client, headers, device, public_key=device.public(raw=True))

    stepped = client.post(
        "/auth/devices/step-up",
        headers={**headers, **device.headers("POST", "/auth/devices/step-up")},
    )
    unsigned = client.post("/auth/devices/step-up", headers=headers)

    assert stepped.status_code == 200, stepped.json()
    assert stepped.json()["amr"] == ["hwk"]
    assert unsigned.json()["code"] == "device_signature_refused"
    assert client.post("/payees", headers=headers).status_code == 200


def test_binding_needs_the_user_again_a_known_key_and_room(app: App) -> None:
    """No password: 401; an RSA key: 422; past MAX_DEVICES: 409."""
    client = app(MAX_DEVICES=1)
    headers = signed_in(client)
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = rsa_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    bare = bind(client, headers, Device(), password=None)
    rsa_bound = bind(client, headers, Device(), public_key=b64encode(spki))
    garbage = bind(client, headers, Device(), public_key="!!")
    p384 = (
        ec.generate_private_key(ec.SECP384R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    wrong_curve = bind(client, headers, Device(), public_key=b64encode(p384))
    first = bind(client, headers, Device())
    second = bind(client, headers, Device())

    assert bare.json()["code"] == "reauthentication_required"
    for refused in (rsa_bound, garbage, wrong_curve):
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "unsupported_device_key",
        )
    assert first.status_code == 201
    assert (second.status_code, second.json()["code"]) == (409, "too_many_keys")


def test_devices_are_listed_and_unbound(app: App) -> None:
    """Listed; unbound, they sign nothing and their logins end; twice: 404."""
    client = app()
    headers = signed_in(client)
    device = Device()
    bind(client, headers, device)
    send(client, headers, device, {"a": 1})
    other = bearer(login(client, "ann")["access_token"])

    listed = client.get("/auth/devices", headers=headers).json()
    removed = client.delete(f"/auth/devices/{device.id}", headers=other)
    again = client.delete(f"/auth/devices/{device.id}", headers=other)

    assert listed[0]["name"] == "Phone"
    assert listed[0]["last_used_at"] is not None
    assert removed.status_code == 204
    assert again.json()["code"] == "device_not_found"
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.get("/auth/me", headers=other).status_code == 200
    assert send(client, other, device, {"a": 2}).status_code == 401


def test_a_recent_step_up_stands_in_for_the_password(app: App) -> None:
    """Stepped up with a device: a second device binds with no password."""
    client = app()
    headers = signed_in(client)
    device = Device()
    bind(client, headers, device)
    client.post(
        "/auth/devices/step-up",
        headers={**headers, **device.headers("POST", "/auth/devices/step-up")},
    )

    second = bind(client, headers, Device(), password=None)

    assert second.status_code == 201, second.json()


# Push tokens.


def queued_pushes() -> list[dict[str, Any]]:
    """The push notifications queued in the outbox, as their payloads."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(messages.c.payload).where(messages.c.target == PUSH_TASK)
            )
            return [row.payload for row in rows]

    return run_async(read())


def test_a_user_is_notified_on_each_app_that_gave_a_token(app: App) -> None:
    """Registered per platform; one notification each; listed without the token."""
    client = app()
    headers = signed_in(client)
    for token, platform in (("fcm-token", "fcm"), ("a" * 64, "apns")):
        answer = client.post(
            "/auth/devices/push-tokens",
            json={"token": token, "platform": platform},
            headers=headers,
        )
        assert answer.status_code == 201, answer.json()
    user_id = UUID(client.get("/auth/me", headers=headers).json()["id"])

    sent = run_async(notify_user(user_id, "Shipped", "On its way", data={"o": "7"}))

    assert sent == 2
    queued = queued_pushes()
    platforms = {p["message"]["platform"] for p in queued}
    assert platforms == {"fcm", "apns"}
    listed = client.get("/auth/devices/push-tokens", headers=headers).json()
    assert {each["platform"] for each in listed} == {"fcm", "apns"}
    assert all("token" not in each for each in listed)


def test_a_token_moves_to_whoever_registers_it_and_the_oldest_end(app: App) -> None:
    """Bob's phone was ann's: the token is his now; past the limit, oldest out."""
    client = app(MAX_PUSH_TOKENS=2)
    ann = signed_in(client)
    bob = signed_in(client, "bob")

    def register(headers: dict[str, str], token: str) -> Any:  # noqa: ANN401
        return client.post(
            "/auth/devices/push-tokens",
            json={"token": token, "platform": "fcm"},
            headers=headers,
        )

    register(ann, "shared")
    register(bob, "shared")
    for token in ("one", "two", "three"):
        register(ann, token)

    assert client.get("/auth/devices/push-tokens", headers=bob).json() != []
    assert len(client.get("/auth/devices/push-tokens", headers=ann).json()) == 2
    assert register(ann, "has space").status_code == 422


def test_tokens_are_removed_by_the_user_or_when_their_service_forgets(
    app: App,
) -> None:
    """DELETE by id (twice: 404); PushTokenExpired removes it by digest."""
    client = app()
    headers = signed_in(client)
    first = client.post(
        "/auth/devices/push-tokens",
        json={"token": "first", "platform": "huawei"},
        headers=headers,
    ).json()
    client.post(
        "/auth/devices/push-tokens",
        json={"token": "second", "platform": "huawei"},
        headers=headers,
    )

    removed = client.delete(f"/auth/devices/push-tokens/{first['id']}", headers=headers)
    again = client.delete(f"/auth/devices/push-tokens/{first['id']}", headers=headers)
    run_async(event_bus.publish(PushTokenExpired(token_digest=token_digest("second"))))

    assert removed.status_code == 204
    assert again.json()["code"] == "device_not_found"
    assert client.get("/auth/devices/push-tokens", headers=headers).json() == []

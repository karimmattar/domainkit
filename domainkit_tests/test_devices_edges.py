"""The refusals and edges of passkeys and devices: each part says no, and why."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from pydantic import ValidationError
from starlette.requests import HTTPConnection, Request

from domainkit.contrib.auth import Credentials
from domainkit.contrib.auth.events import UserDeleted
from domainkit.contrib.devices import ReauthenticationRequired
from domainkit.contrib.devices.apps import forget_devices
from domainkit.contrib.devices.binding import SIGNING_DEVICE, DeviceSigned
from domainkit.contrib.devices.passkeys import reauthenticate
from domainkit.contrib.devices.settings import DevicesSettings
from domainkit.contrib.devices.stores import DevicesUnitOfWork, PasskeyStore, PushToken
from domainkit.contrib.devices.webauthn import (
    AT,
    ED,
    EDDSA,
    ES256,
    RS256,
    UP,
    UV,
    WebAuthnError,
    b64decode,
    b64encode,
    cbor_decode,
    client_challenge,
    cose_key,
    parse_authenticator_data,
    verify_assertion,
    verify_registration,
    verify_signature,
)
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit_tests.test_auth_http import PASSWORD
from domainkit_tests.test_devices import (
    ORIGIN,
    RP_ID,
    SETTINGS,
    App,
    Authenticator,
    Device,
    add_passkey,
    app,  # noqa: F401 - the fixture.
    bind,
    cbor,
    client_json,
    signed_in,
)

NOW = datetime(2027, 1, 4, tzinfo=UTC)


def test_base64url_from_the_browser() -> None:
    """Padded or not; other text refused."""
    assert b64decode("YWI") == b64decode("YWI=") == b"ab"
    for bad in ("é", "a", "a+b/", "!!", 7):
        with pytest.raises(WebAuthnError):
            b64decode(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("data", "value"),
    [
        (b"\x00", 0),
        (b"\x20", -1),
        (b"\x1b" + (2**40).to_bytes(8, "big"), 2**40),
        (b"\xf4", False),
        (b"\xf5", True),
        (b"\xf6", None),
        (cbor([1, "a", b"b", {"k": [None]}]), [1, "a", b"b", {"k": [None]}]),
    ],
)
def test_cbor_reads_what_authenticators_write(data: bytes, value: Any) -> None:  # noqa: ANN401
    """Integers, strings, bytes, arrays, maps, and the simple values."""
    assert cbor_decode(data) == value


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\x00",
        b"\x18",
        b"\x42a",
        b"\x82\x01",
        b"\x9b" + (2**64 - 1).to_bytes(8, "big") + b"\x00",
        b"\xa1\x01",
        b"\x9f",
        b"\x62\xff\xfe",
        b"\xa1\x41a\x01",
        b"\xa2\x01\x01\x01\x01",
        b"\x81" * 10 + b"\x00",
        b"\xc0\x00",
        b"\xf9\x00\x00",
        b"\x1c",
    ],
)
def test_cbor_refuses_what_it_does_not_read(data: bytes) -> None:
    """Empty, trailing, cut short, indefinite, bad text or keys, deep, tags, floats."""
    with pytest.raises(WebAuthnError):
        cbor_decode(data)


def p256_key(key: ec.EllipticCurvePrivateKey) -> dict[int, Any]:
    """A P-256 key as COSE writes it."""
    numbers = key.public_key().public_numbers()
    return {
        1: 2,
        3: ES256,
        -1: 1,
        -2: numbers.x.to_bytes(32, "big"),
        -3: numbers.y.to_bytes(32, "big"),
    }


def test_cose_keys_of_each_algorithm() -> None:
    """ES256, EdDSA, and RS256 of 2048 bits are read."""
    ed = (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    numbers = rsa.generate_private_key(65537, 2048).public_key().public_numbers()
    n = numbers.n.to_bytes(256, "big")

    assert cose_key(p256_key(ec.generate_private_key(ec.SECP256R1())))[0] == ES256
    assert cose_key({1: 1, 3: EDDSA, -1: 6, -2: ed})[0] == EDDSA
    assert cose_key({1: 3, 3: RS256, -1: n, -2: b"\x01\x00\x01"})[0] == RS256


@pytest.mark.parametrize(
    "key",
    [
        {1: 2, 3: ES256, -1: 1, -2: b"x", -3: b"y"},
        {1: 2, 3: ES256, -1: 1, -2: bytes(32), -3: bytes(32)},
        {1: 1, 3: EDDSA, -1: 6, -2: "x"},
        {1: 1, 3: EDDSA, -1: 6, -2: b"short"},
        {1: 3, 3: RS256, -1: "n", -2: b"\x01"},
        {1: 3, 3: RS256, -1: (2**1023 + 1).to_bytes(128, "big"), -2: b"\x01\x00\x01"},
        {1: 2, 3: -35, -1: 2},
    ],
)
def test_cose_keys_that_are_not_offered_or_not_keys(key: dict[int, Any]) -> None:
    """Short, off the curve, of the wrong type, too weak, or not offered."""
    with pytest.raises(WebAuthnError):
        cose_key(key)


def test_signatures_of_each_algorithm() -> None:
    """Each holds for its key; a key of another type or garbage never does."""
    data = b"signed"
    p256 = ec.generate_private_key(ec.SECP256R1())
    ed = ed25519.Ed25519PrivateKey.generate()
    rs = rsa.generate_private_key(65537, 2048)

    def spki(key: Any) -> bytes:  # noqa: ANN401
        public: bytes = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return public

    rs_signature = rs.sign(data, padding.PKCS1v15(), hashes.SHA256())
    assert verify_signature(
        ES256, spki(p256), p256.sign(data, ec.ECDSA(hashes.SHA256())), data
    )
    assert verify_signature(EDDSA, spki(ed), ed.sign(data), data)
    assert verify_signature(RS256, spki(rs), rs_signature, data)
    assert not verify_signature(ES256, spki(rs), rs_signature, data)
    assert not verify_signature(RS256, spki(rs), b"bad", data)
    assert not verify_signature(ES256, b"not a key", b"", data)


@pytest.mark.parametrize(
    "data",
    [
        b"\x00" * 36,
        b"\x00" * 32 + bytes([AT]) + bytes(4) + bytes(10),
        b"\x00" * 32 + bytes([AT]) + bytes(4) + bytes(16) + b"\x00\x00",
        b"\x00" * 32 + bytes([AT]) + bytes(4) + bytes(16) + b"\x00\x01a" + cbor(1),
        b"\x00" * 32 + bytes([UP]) + bytes(4) + b"\x00",
    ],
)
def test_authenticator_data_that_does_not_parse(data: bytes) -> None:
    """Short, an empty or non-map credential, or trailing bytes."""
    with pytest.raises(WebAuthnError):
        parse_authenticator_data(data)


def test_authenticator_data_with_extensions() -> None:
    """Extensions are read past, not into."""
    data = b"\x00" * 32 + bytes([UP | ED]) + bytes(4) + cbor({"credProtect": 2})

    assert parse_authenticator_data(data).flags == UP | ED


@pytest.mark.parametrize(
    "client_data",
    [
        b"x" * 5000,
        b"not json",
        b"[1]",
        b"{}",
        json.dumps({"challenge": "x" * 200}).encode(),
    ],
)
def test_client_data_that_names_no_challenge(client_data: bytes) -> None:
    """Too long, not JSON, not an object, no challenge, an absurd one."""
    with pytest.raises(WebAuthnError):
        client_challenge(client_data)


def registration(**client: Any) -> dict[str, Any]:  # noqa: ANN401
    """What ``verify_registration`` checks, made by a software authenticator."""
    key = Authenticator()
    created = key.create({"challenge": "c"})
    data = json.loads(b64decode(created["response"]["clientDataJSON"]))
    return {
        "client_data_json": json.dumps({**data, **client}).encode(),
        "attestation_object": b64decode(created["response"]["attestationObject"]),
        "challenge": "c",
        "rp_id": RP_ID,
        "origins": (ORIGIN,),
    }


def test_registrations_framed_or_without_authenticator_data() -> None:
    """Made in another origin's frame; an attestation that is not a map."""
    with pytest.raises(WebAuthnError, match="frame"):
        verify_registration(**registration(crossOrigin=True))
    with pytest.raises(WebAuthnError, match="authenticator data"):
        verify_registration(**{**registration(), "attestation_object": cbor([1])})
    unattested = cbor(
        {
            "fmt": "none",
            "attStmt": {},
            "authData": hashlib.sha256(RP_ID.encode()).digest()
            + bytes([UP | UV])
            + bytes(4),
        }
    )
    with pytest.raises(WebAuthnError, match="no attested credential"):
        verify_registration(**{**registration(), "attestation_object": unattested})
    with pytest.raises(WebAuthnError, match="another challenge"):
        verify_registration(**registration(challenge=7))


def test_an_assertion_must_attest_nothing_and_count_on() -> None:
    """A stored count with no new one: a copied key."""
    key = ec.generate_private_key(ec.SECP256R1())
    client = client_json("webauthn.get", "c", ORIGIN)
    data = hashlib.sha256(RP_ID.encode()).digest() + bytes([UP | UV]) + bytes(4)
    signature = key.sign(
        data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
    )
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    with pytest.raises(WebAuthnError, match="went back"):
        verify_assertion(
            client_data_json=client,
            authenticator_data=data,
            signature=signature,
            challenge="c",
            rp_id=RP_ID,
            origins=(ORIGIN,),
            algorithm=ES256,
            public_key=spki,
            sign_count=5,
        )


def test_rp_id_needs_origins() -> None:
    """Passkeys with nowhere to be used: refused."""
    with pytest.raises(ValidationError, match="ORIGINS"):
        DevicesSettings(RP_ID=RP_ID)


def http(**scope: Any) -> Request:  # noqa: ANN401
    """A request of ``scope``."""
    return Request({"type": "http", "headers": [], **scope})


def test_device_signed_reads_its_answer_once_and_only_of_requests() -> None:
    """Cached on the request; a WebSocket or no user: refused."""

    class Someone:
        is_authenticated = True
        id = uuid4()

    rule = DeviceSigned()
    cached = http(**{SIGNING_DEVICE: None})
    socket = HTTPConnection({"type": "websocket", "headers": []})

    assert run_async(rule.has_object_permission(cached, Someone(), object())) is False
    assert run_async(rule.has_permission(socket, Someone())) is False
    assert run_async(rule.has_permission(http(), object())) is False


def test_staff_acting_as_a_user_add_no_keys() -> None:
    """An impersonation never adds a passkey or a device, password or not."""
    user: Any = object()
    request = http(auth=Credentials(user, "token", impersonator=uuid4()))

    with pytest.raises(ReauthenticationRequired):
        run_async(reauthenticate(request, user, PASSWORD, "devices.add_passkey"))


def test_registrations_cut_short(app: App) -> None:  # noqa: F811 - the fixture.
    """Garbage fields, no challenge, and a limit reached between the steps."""
    client = app(**SETTINGS, MAX_PASSKEYS=1)
    headers = signed_in(client)
    first, second, third = (
        client.post(
            "/auth/passkeys/register/options",
            json={"password": PASSWORD},
            headers=headers,
        ).json()
        for _ in range(3)
    )
    created = Authenticator().create(first)
    spent = Authenticator().create(third)

    garbage = client.post(
        "/auth/passkeys/register",
        json={
            "credential": {"response": {**spent["response"], "attestationObject": "!!"}}
        },
        headers=headers,
    )
    no_challenge = client.post(
        "/auth/passkeys/register",
        json={
            "credential": {
                "response": {**created["response"], "clientDataJSON": b64encode(b"{}")}
            }
        },
        headers=headers,
    )
    added = client.post(
        "/auth/passkeys/register",
        json={"credential": Authenticator().create(second)},
        headers=headers,
    )
    full = client.post(
        "/auth/passkeys/register", json={"credential": created}, headers=headers
    )

    assert garbage.json()["code"] == "passkey_refused"
    assert no_challenge.json()["code"] == "passkey_refused"
    assert added.status_code == 201
    assert full.json()["code"] == "too_many_keys"


def test_logins_cut_short(app: App, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """A garbage credential id; a use another request counted past."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    key = Authenticator()
    add_passkey(client, headers, key)

    options = client.post("/auth/passkeys/login/options").json()
    garbage = client.post(
        "/auth/passkeys/login",
        json={"credential": {**key.get(options), "id": "!!"}},
    )

    async def counted_past(self: PasskeyStore, passkey: Any, count: int) -> bool:  # noqa: ANN401
        return False

    monkeypatch.setattr(PasskeyStore, "used", counted_past)
    options = client.post("/auth/passkeys/login/options").json()
    raced = client.post("/auth/passkeys/login", json={"credential": key.get(options)})

    assert garbage.json()["code"] == "passkey_refused"
    assert raced.json()["code"] == "passkey_refused"


def test_a_signed_request_with_a_query(app: App) -> None:  # noqa: F811 - the fixture.
    """The query is signed with the path."""
    client = app()
    headers = signed_in(client)
    device = Device()
    bind(client, headers, device)
    body = b'{"a": 1}'

    answer = client.post(
        "/transfers?draft=1",
        content=body,
        headers={
            **headers,
            **device.headers("POST", "/transfers?draft=1", body),
            "Content-Type": "application/json",
        },
    )

    assert answer.status_code == 200, answer.json()


def test_a_deleted_user_loses_their_keys(app: App) -> None:  # noqa: F811 - the fixture.
    """UserDeleted removes every passkey and device of the user."""
    client = app(**SETTINGS)
    headers = signed_in(client)
    add_passkey(client, headers, Authenticator())
    bind(client, headers, Device())
    user_id = client.get("/auth/me", headers=headers).json()["id"]

    async def remaining() -> int:
        async with DevicesUnitOfWork() as uow:
            ann = UUID(user_id)
            await forget_devices(UserDeleted(user_id=ann, username="ann"))
            return await uow.passkeys.count(ann) + await uow.devices.count(ann)

    assert run_async(remaining()) == 0


def test_devices_must_share_auths_database(
    app: App,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routed elsewhere, the app refuses to start."""
    from domainkit.apps import registry

    app()
    config = registry.get_domain_config("devices")
    monkeypatch.setattr(router, "target_for", lambda label: f"database-of-{label}")

    with pytest.raises(ImproperlyConfigured, match="routed with 'auth'"):
        config.ready()


def test_without_notifications_no_token_is_listened_for(
    app: App,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notifications not installed: ready() subscribes to deletions only."""
    from domainkit.apps import registry

    app()
    config = registry.get_domain_config("devices")
    monkeypatch.setattr(registry, "is_installed", lambda name: False)

    config.ready()

    token = PushToken(uuid4(), uuid4(), "d", "secret-token", "fcm", NOW, NOW)
    assert "secret-token" not in repr(token)

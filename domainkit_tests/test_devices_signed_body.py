"""A signed request's body is read once its device is the user's (U5, ADR 0175).

``DeviceSigned`` read the whole body before it looked the device up: a
stolen session without the phone's key made the service read every body
it sent, up to the route's limit. The device is now looked up from the
headers first. A body's SHA-256 declared in ``Content-Digest`` lets the
signature be checked before the body is read, and
``DEVICES.CONTENT_DIGEST = "required"`` refuses a request that declares
none, its body unread.
"""

import base64
import hashlib
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.requests import Request
from starlette.types import Message

from domainkit.contrib.devices import binding
from domainkit.contrib.devices.binding import device_proof, signing_device
from domainkit.contrib.devices.stores import BoundDevice, DevicesUnitOfWork
from domainkit.contrib.devices.tables import devices
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.test_devices import App, Device, app, bind, signed_in

__all__ = ["app"]  # The fixture the tests take.

BODY: Final = b'{"amount": "10.00"}'


def digest_of(body: bytes) -> str:
    """``Content-Digest`` for ``body``: its SHA-256, base64 between colons."""
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


class Sent:
    """A request to ``/transfers`` as the rule reads it, counting reads of its body."""

    def __init__(
        self,
        headers: dict[str, str],
        body: bytes = BODY,
        meanwhile: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """``headers`` and ``body``; ``meanwhile`` runs as the body arrives."""
        self.reads = 0
        self.body = body
        self.meanwhile = meanwhile
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/transfers",
            "raw_path": b"/transfers",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
        }
        self.request = Request(scope, self.receive)

    async def receive(self) -> Message:
        """The body, whole, counted."""
        self.reads += 1
        if self.meanwhile is not None:
            await self.meanwhile()
        return {"type": "http.request", "body": self.body, "more_body": False}


def owner_of(device: Device) -> Any:  # noqa: ANN401 - a principal.
    """A principal for the user ``device`` is bound to."""

    async def found() -> BoundDevice | None:
        async with DevicesUnitOfWork() as uow:
            return await uow.devices.get(UUID(device.id))

    bound = run_async(found())
    assert bound is not None
    return SimpleNamespace(id=bound.user_id, is_authenticated=True)


def signed(sent: Sent, user: Any) -> UUID | None:  # noqa: ANN401 - a principal.
    """The id of the device that signed ``sent`` for ``user``, if one did."""
    device = run_async(signing_device(sent.request, user))
    return None if device is None else device.id


@pytest.fixture
def ann(app: App) -> tuple[TestClient, Device, Any]:
    """Ann's client, a device bound to her, and her principal."""
    client = app()
    device = Device()
    assert bind(client, signed_in(client), device).status_code == 201
    return client, device, owner_of(device)


def test_a_device_not_the_users_is_refused_before_the_body_is_read(
    ann: tuple[TestClient, Device, Any],
) -> None:
    """Unknown, another user's, or malformed: refused, not a byte read."""
    _, device, user = ann
    stranger = Device()
    stranger.id = str(uuid4())
    bob = SimpleNamespace(id=uuid4(), is_authenticated=True)
    malformed = {**device.headers("POST", "/transfers", BODY), "X-Device-Time": "x"}
    refused = [
        Sent(stranger.headers("POST", "/transfers", BODY)),
        Sent(device.headers("POST", "/transfers", BODY)),
        Sent(malformed),
    ]

    assert signed(refused[0], user) is None
    assert signed(refused[1], bob) is None
    assert signed(refused[2], user) is None
    assert [each.reads for each in refused] == [0, 0, 0]
    passed = Sent(device.headers("POST", "/transfers", BODY))
    assert signed(passed, user) == UUID(device.id)
    assert passed.reads == 1
    assert run_async(device_proof(passed.request, user)) is not None


def test_a_declared_digest_is_signed_before_the_body_is_read(
    ann: tuple[TestClient, Device, Any],
) -> None:
    """The signature holds for the digest first; then the body must be it."""
    _, device, user = ann
    declared = {"Content-Digest": digest_of(BODY)}
    good = Sent({**device.headers("POST", "/transfers", BODY), **declared})
    swapped = Sent(
        {**device.headers("POST", "/transfers", BODY), **declared},
        body=b'{"amount": "9999.00"}',
    )

    assert signed(good, user) == UUID(device.id)
    assert good.reads == 1
    assert signed(swapped, user) is None
    assert swapped.reads == 1


@pytest.mark.parametrize("mode", ["optional", "required"])
def test_a_required_digest_refuses_unread_what_does_not_hold(
    app: App,
    mode: str,
) -> None:
    """Required: no digest, or a signature that does not hold for it, read nothing.

    Optional: such a request is checked as before, once its body is read;
    a digest the signature does not hold for is set aside.
    """
    client = app(CONTENT_DIGEST=mode)
    device = Device()
    assert bind(client, signed_in(client), device).status_code == 201
    user = owner_of(device)
    thief = Device()
    thief.id = device.id
    other_digest = {"Content-Digest": digest_of(b"another body")}
    moment = int(time.time() * 1000)

    def headers(signer: Device, at: int) -> dict[str, str]:
        return signer.headers("POST", "/transfers", BODY, when=moment + at)

    sent: dict[str, Sent] = {
        "none": Sent(headers(device, 0)),
        "malformed": Sent({**headers(device, 1), "Content-Digest": "x"}),
        "thief": Sent({**headers(thief, 2), "Content-Digest": digest_of(BODY)}),
        "other": Sent({**headers(device, 3), **other_digest}),
    }
    found = {name: signed(each, user) for name, each in sent.items()}
    reads = {name: each.reads for name, each in sent.items()}

    if mode == "required":
        assert found == dict.fromkeys(sent)
        assert reads == dict.fromkeys(sent, 0)
    else:
        mine = UUID(device.id)
        assert found == {"none": mine, "malformed": mine, "thief": None, "other": mine}
        assert reads == dict.fromkeys(sent, 1)


@pytest.mark.parametrize("store", ["database", "cache"])
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("change", ["unbound", "rekeyed"])
def test_a_device_unbound_while_the_body_arrives_is_refused(
    app: App, store: str, declared: bool, change: str
) -> None:
    """Read before the body and again after it: one gone, or changed, refuses.

    The device is looked up with no unit of work open while the body
    arrives; the one that records the request reads it again.
    """
    client = app(SIGNATURE_STORE=store)
    device = Device()
    assert bind(client, signed_in(client), device).status_code == 201
    user = owner_of(device)
    headers = device.headers("POST", "/transfers", BODY)
    if declared:
        headers["Content-Digest"] = digest_of(BODY)

    async def unbind() -> None:
        async with DevicesUnitOfWork() as uow:
            assert await uow.devices.remove(user.id, UUID(device.id))
            await uow.commit()

    async def rekey() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(devices)
                .where(devices.c.id == UUID(device.id))
                .values(public_key=b"another key")
            )

    sent = Sent(headers, meanwhile=unbind if change == "unbound" else rekey)

    assert signed(sent, user) is None
    assert sent.reads == 1
    kept = Sent(
        device.headers("POST", "/transfers", BODY, when=int(time.time() * 1000) + 1)
    )
    assert signed(kept, user) is None


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("", None),
        (digest_of(BODY), hashlib.sha256(BODY).digest()),
        (
            (
                f"sha-512=:{base64.b64encode(b'x' * 64).decode()}:, "
                f"{digest_of(BODY)};note=1"
            ),
            hashlib.sha256(BODY).digest(),
        ),
        ("sha-256=abc", None),
        ("sha-256=:", None),
        ("sha-256=:!!:", None),
        (f"sha-256=:{base64.b64encode(b'short').decode()}:", None),
        (f"sha-512=:{base64.b64encode(b'x' * 64).decode()}:", None),
    ],
)
def test_content_digest_is_read_as_rfc_9530_writes_it(
    field: str, expected: bytes | None
) -> None:
    """A dictionary of algorithms; SHA-256 alone is read, 32 bytes, or nothing."""
    assert binding._declared(field) == expected


def test_the_signed_body_still_reaches_the_route(app: App) -> None:
    """Through the application: declared, signed, and read again by the route."""
    client: TestClient = app(CONTENT_DIGEST="required")
    headers = signed_in(client)
    device = Device()
    assert bind(client, headers, device).status_code == 201
    send: Callable[..., Any] = client.post

    answer = send(
        "/transfers",
        content=BODY,
        headers={
            **headers,
            **device.headers("POST", "/transfers", BODY),
            "Content-Digest": digest_of(BODY),
            "Content-Type": "application/json",
        },
    )
    unsaid = send(
        "/transfers",
        content=BODY,
        headers={
            **headers,
            **device.headers("POST", "/transfers", BODY),
            "Content-Type": "application/json",
        },
    )

    assert answer.status_code == 200, answer.json()
    assert answer.json() == {"sent": {"amount": "10.00"}}
    assert (unsaid.status_code, unsaid.json()["code"]) == (
        401,
        "device_signature_refused",
    )

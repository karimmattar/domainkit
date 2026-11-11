"""A signed request works once, in whatever order requests arrive (ADR 0135).

Before 2.3 each signed request had to be later than the device's last: an
app that sent two at once had the one that arrived second refused.
"""

import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import utils
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from domainkit import clock
from domainkit.cache import CacheUnavailable, caches
from domainkit.conf import override_settings
from domainkit.contrib.devices.apps import _check_signature_store
from domainkit.contrib.devices.stores import DevicesUnitOfWork
from domainkit.contrib.devices.tables import signatures
from domainkit.contrib.devices.tasks import purge_signatures
from domainkit.contrib.devices.webauthn import b64decode, b64encode
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit_tests.test_devices import App, Device, app, bind, send, signed_in

__all__ = ["app"]  # The fixture the tests take.

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
REFUSED = (401, "device_signature_refused")


def bound(client: TestClient) -> tuple[dict[str, str], Device]:
    """Ann, logged in, and a device of hers."""
    headers = signed_in(client)
    device = Device()
    assert bind(client, headers, device).status_code == 201
    return headers, device


def kept() -> int:
    """How many signed requests the database remembers."""

    async def count() -> int:
        async with connections.engine().connect() as connection:
            found = await connection.scalar(
                select(func.count()).select_from(signatures)
            )
            return int(found or 0)

    return run_async(count())


def answer(response: Any) -> tuple[int, str | None]:  # noqa: ANN401 - a response.
    """The status and the problem's code."""
    return response.status_code, response.json().get("code")


def test_requests_pass_in_whatever_order_they_arrive(app: App) -> None:
    """The audit's finding: the earlier of two parallel requests got 401."""
    client = app()
    headers, device = bound(client)
    now = int(time.time() * 1000)

    later = send(client, headers, device, {"n": 2}, when=now + 5)
    earlier = send(client, headers, device, {"n": 1}, when=now)

    assert later.status_code == 200, later.json()
    assert earlier.status_code == 200, earlier.json()


def test_two_requests_of_one_millisecond_both_pass(app: App) -> None:
    """They differ: only the same request, sent again, is a replay."""
    client = app()
    headers, device = bound(client)
    now = int(time.time() * 1000)

    first = send(client, headers, device, {"n": 1}, when=now)
    second = send(client, headers, device, {"n": 2}, when=now)
    again = send(client, headers, device, {"n": 2}, when=now)

    assert (first.status_code, second.status_code) == (200, 200)
    assert answer(again) == REFUSED


def test_another_signature_of_the_same_request_is_still_a_replay(app: App) -> None:
    """ECDSA: (r, n - s) signs what (r, s) signs. The request is kept, not its bytes."""
    client = app()
    headers, device = bound(client)
    body = json.dumps({"amount": "10.00"}).encode()
    signing = device.headers("POST", "/transfers", body)
    r, s = utils.decode_dss_signature(b64decode(signing["X-Device-Signature"]))
    twin = {
        **signing,
        "X-Device-Signature": b64encode(utils.encode_dss_signature(r, P256_ORDER - s)),
    }
    json_headers = {**headers, "Content-Type": "application/json"}

    first = client.post("/transfers", content=body, headers={**json_headers, **signing})
    replayed = client.post("/transfers", content=body, headers={**json_headers, **twin})

    assert first.status_code == 200, first.json()
    assert answer(replayed) == REFUSED
    assert kept() == 1


def test_a_device_forgets_its_requests_once_they_are_too_old(app: App) -> None:
    """Its last use is written once a minute, and the old requests go then."""
    client = app()
    headers, device = bound(client)
    with clock.freeze_time("2026-11-10T10:00:00Z") as frozen:
        start = int(clock.now().timestamp() * 1000)
        for n in range(3):
            assert (
                send(client, headers, device, {"n": n}, when=start + n).status_code
                == 200
            )
        assert kept() == 3
        [listed] = client.get("/auth/devices", headers=headers).json()
        first_use = listed["last_used_at"]

        frozen.tick(30)
        soon = int(clock.now().timestamp() * 1000)
        assert send(client, headers, device, {"n": 4}, when=soon).status_code == 200
        [listed] = client.get("/auth/devices", headers=headers).json()
        assert listed["last_used_at"] == first_use  # Not written with every request.
        assert kept() == 4

        frozen.tick(151)  # Past the window (two minutes) since the first three.
        late = int(clock.now().timestamp() * 1000)
        assert send(client, headers, device, {"n": 5}, when=late).status_code == 200
        [listed] = client.get("/auth/devices", headers=headers).json()
        assert listed["last_used_at"] != first_use
        assert kept() == 1  # Only the one just sent.


def test_the_purge_forgets_the_requests_of_devices_that_stopped(app: App) -> None:
    """Hourly, with messaging: whatever is outside the window."""
    client = app()
    headers, device = bound(client)
    with clock.freeze_time("2026-11-10T10:00:00Z") as frozen:
        now = int(clock.now().timestamp() * 1000)
        assert send(client, headers, device, {"n": 1}, when=now).status_code == 200
        assert run_async(purge_signatures()) == 0
        frozen.tick(121)
        assert run_async(purge_signatures()) == 1
    assert kept() == 0


def test_unbinding_a_device_and_deleting_a_user_forget_their_requests(
    app: App,
) -> None:
    """Nothing is kept of a device that is gone."""
    client = app()
    headers, device = bound(client)
    other = Device()
    assert bind(client, headers, other).status_code == 201
    assert send(client, headers, device, {"n": 1}).status_code == 200
    assert send(client, headers, other, {"n": 2}).status_code == 200

    assert (
        client.delete(f"/auth/devices/{device.id}", headers=headers).status_code == 204
    )
    assert kept() == 1

    async def forget() -> None:
        async with DevicesUnitOfWork() as uow:
            [left] = await uow.devices.of_user(
                (await uow.users.find_by_identifier("ann")).id  # type: ignore[union-attr]
            )
            await uow.devices.forget_user(left.user_id)
            await uow.commit()

    run_async(forget())
    assert kept() == 0


def test_with_the_cache_a_signed_request_writes_to_no_table(app: App) -> None:
    """``SIGNATURE_STORE = "cache"``: kept in the default cache, once."""
    client = app(SIGNATURE_STORE="cache")
    headers, device = bound(client)
    body = json.dumps({"n": 1}).encode()
    signing = device.headers("POST", "/transfers", body)
    json_headers = {**headers, "Content-Type": "application/json"}

    first = client.post("/transfers", content=body, headers={**json_headers, **signing})
    again = client.post("/transfers", content=body, headers={**json_headers, **signing})
    other = send(client, headers, device, {"n": 2})

    assert first.status_code == 200, first.json()
    assert answer(again) == REFUSED
    assert other.status_code == 200
    assert kept() == 0
    [listed] = client.get("/auth/devices", headers=headers).json()
    assert listed["last_used_at"] is not None  # Still noted, once a minute.


def test_with_the_cache_down_no_signed_request_passes(
    app: App, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A request nothing can remember is refused, and the outage logged."""
    client = app(SIGNATURE_STORE="cache")
    headers, device = bound(client)

    async def down(*args: object, **named: object) -> bool:
        raise CacheUnavailable("The cache is down.")

    monkeypatch.setattr(type(run_async(_default_cache())), "add", down)
    with caplog.at_level("ERROR", logger="domainkit.contrib.devices.binding"):
        refused = send(client, headers, device, {"n": 1})

    assert answer(refused) == REFUSED
    assert "is down" in caplog.text


async def _default_cache() -> object:
    """The default cache, which needs a running loop to look up."""
    return caches["default"]


@pytest.mark.parametrize(
    ("url", "environment", "refused"),
    [
        ("dummy://", "test", True),
        ("memory://", "test", False),
        ("memory://", "production", True),
        ("rediss://:s3cret@cache.internal/0", "production", False),
    ],
)
def test_the_cache_must_be_one_every_process_shares(
    url: str, environment: str, refused: bool
) -> None:
    """Else another process, or none, remembers: a request could work twice."""
    cache = {"URL": url, **({"KEY_PREFIX": "bank"} if url.startswith("redis") else {})}
    with override_settings(CACHES={"default": cache}, ENVIRONMENT=environment):
        _check_signature_store("database")
        if refused:
            with pytest.raises(ImproperlyConfigured, match="SIGNATURE_STORE"):
                _check_signature_store("cache")
        else:
            _check_signature_store("cache")

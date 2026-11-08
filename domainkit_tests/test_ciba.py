"""Sign-ins approved from the phone: push and QR, the phone's answers, polls."""

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import clock
from domainkit.conf import override_settings
from domainkit.contrib.ciba.services import PUSH_KIND
from domainkit.contrib.ciba.tables import sign_in_requests
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    IDENTITY,
    IDENTITY_DOMAINS,
    ExitStackClients,
    Migrated,
    Shape,
)
from domainkit_tests.test_auth_cookies import TRUSTED
from domainkit_tests.test_auth_http import PASSWORD, audited, bearer, create_user
from domainkit_tests.test_devices import Device, bind, queued_pushes

CIBA = Shape(
    "ciba",
    "ciba",
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [
            *IDENTITY_DOMAINS,
            "domainkit.contrib.notifications",
            "domainkit.contrib.devices",
            "domainkit.contrib.ciba",
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_ciba_routes.router",
    },
)
SITE = "https://testserver"
LAPTOP = ("203.0.113.7", 50000)
PHONE = ("198.51.100.9", 50001)
AGENT = "Mozilla/5.0 (Laptop)"
type App = Callable[..., TestClient]


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(address, **CIBA)``: a client of the project, from ``address``."""
    with migrated(CIBA), ExitStackClients() as clients:

        def make(address: tuple[str, int] = LAPTOP, **ciba: Any) -> TestClient:  # noqa: ANN401
            if ciba:  # Settings stay as the last client set them.
                clients.enter(override_settings(CIBA=ciba))
            client = clients.client(address)
            return TestClient(client.app, base_url=SITE, client=address)

        yield make


class Phone:
    """A user's phone: logged in, a bound device, a push token; signs requests."""

    def __init__(self, client: TestClient, username: str = "alice") -> None:
        """Log ``username`` in from ``client``, bind a key, register a token."""
        self.client = client
        self.user_id = create_user(username)
        answer = client.post(
            "/auth/login", json={"identifier": username, "password": PASSWORD}
        )
        assert answer.status_code == 200, answer.json()
        self.headers = bearer(answer.json()["access_token"])
        self.device = Device()
        self.last_moment = 0
        assert bind(client, self.headers, self.device).status_code == 201
        registered = client.post(
            "/auth/devices/push-tokens",
            json={"token": f"token-of-{username}", "platform": "fcm"},
            headers=self.headers,
        )
        assert registered.status_code == 201, registered.json()

    def send(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:  # noqa: ANN401 - a response.
        """``method path`` with ``payload``, signed by the device, at clock time.

        Each signature is later than the last, as a real app's must be: two
        requests signed in one millisecond would be refused as a replay.
        """
        body = b"" if payload is None else json.dumps(payload).encode()
        moment = max(int(clock.now().timestamp() * 1000), self.last_moment + 1)
        self.last_moment = moment
        return self.client.request(
            method,
            path,
            content=body or None,
            headers={
                **self.headers,
                **({"Content-Type": "application/json"} if body else {}),
                **self.device.headers(method, path, body, when=moment),
            },
        )

    def waiting(self) -> list[dict[str, Any]]:
        """The sign-ins the phone lists."""
        answer = self.send("GET", "/auth/app-sign-in/requests")
        assert answer.status_code == 200, answer.json()
        listed: list[dict[str, Any]] = answer.json()
        return listed

    def approve(self, request_id: str, **body: Any) -> Any:  # noqa: ANN401
        """Approve ``request_id``: the response."""
        return self.send(
            "POST", f"/auth/app-sign-in/requests/{request_id}/approve", body
        )

    def deny(self, request_id: str, **body: Any) -> Any:  # noqa: ANN401
        """Deny ``request_id``: the response."""
        return self.send("POST", f"/auth/app-sign-in/requests/{request_id}/deny", body)


def start_push(web: TestClient, identifier: str = "alice") -> dict[str, Any]:
    """Start a push sign-in from the web page: what it answers."""
    answer = web.post(
        "/auth/app-sign-in/push",
        json={"identifier": identifier},
        headers={"User-Agent": AGENT},
    )
    assert answer.status_code == 200, answer.json()
    assert answer.headers["Cache-Control"] == "no-store"
    started: dict[str, Any] = answer.json()
    return started


def redeem(web: TestClient, handle: str, route: str = "token") -> Any:  # noqa: ANN401
    """Poll the request ``handle`` names: the response."""
    return web.post(
        f"/auth/app-sign-in/{route}", json={"request": handle}, headers=TRUSTED
    )


def rows() -> list[dict[str, Any]]:
    """Every stored sign-in request, oldest first."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                select(sign_in_requests).order_by(sign_in_requests.c.created_at)
            )
            return [dict(row._mapping) for row in found]

    return run_async(read())


def sign_in_pushes() -> list[dict[str, Any]]:
    """The sign-in pushes queued, as their messages."""
    return [
        each["message"]
        for each in queued_pushes()
        if each["message"]["data"].get("type") == PUSH_KIND
    ]


# Push.


def test_a_push_sign_in_is_approved_from_the_phone_with_the_number(app: App) -> None:
    """Start, push, list, approve with the page's number, redeem once."""
    web = app()
    phone = Phone(app(PHONE))

    started = start_push(web)
    assert set(started) == {"request", "expires_in", "interval", "match_code"}
    assert started["expires_in"] == 120
    assert started["interval"] == 5
    assert len(started["match_code"]) == 2
    assert started["match_code"].isdigit()
    assert len(started["request"]) >= 43

    pushes = sign_in_pushes()
    assert len(pushes) == 1
    assert pushes[0]["token"] == "token-of-alice"
    assert pushes[0]["title"] == "Approve sign-in?"
    request_id = pushes[0]["data"]["request_id"]
    assert started["request"] not in json.dumps(pushes)  # The handle stays home.

    [waiting] = phone.waiting()
    assert waiting["id"] == request_id
    assert waiting["channel"] == "push"
    assert waiting["client"] is None
    assert waiting["address"] == LAPTOP[0]
    assert waiting["browser"] == AGENT
    assert waiting["same_network"] is False
    assert waiting["match_required"] is True
    assert started["match_code"] not in json.dumps(waiting.get("match_code", ""))
    assert "match_code" not in waiting

    assert redeem(web, started["request"]).json()["code"] == "authorization_pending"
    approved = phone.approve(request_id, match_code=started["match_code"])
    assert approved.status_code == 204, approved.json()
    assert phone.waiting() == []

    with clock.freeze_time(clock.now() + timedelta(seconds=6)):
        tokens = redeem(web, started["request"])
        assert tokens.status_code == 200, tokens.json()
        assert tokens.headers["Cache-Control"] == "no-store"
        assert set(tokens.json()) >= {"access_token", "token_type"}
        me = web.get("/auth/me", headers=bearer(tokens.json()["access_token"]))
        assert me.json()["username"] == "alice"
    with clock.freeze_time(clock.now() + timedelta(seconds=12)):
        again = redeem(web, started["request"])
    assert again.status_code == 400
    assert again.json()["code"] == "sign_in_unknown"
    [event] = audited("ciba.sign_in_approved")
    assert event["actor"] is not None


def test_a_poll_too_soon_slows_down_and_the_interval_grows(app: App) -> None:
    """The first poll waits; the next within 5 s slows down; then 10 s apply."""
    web = app()
    Phone(app(PHONE))
    with clock.freeze_time("2026-11-05T10:00:00Z") as frozen:
        handle = start_push(web)["request"]
        assert redeem(web, handle).json()["code"] == "authorization_pending"
        frozen.tick(2)
        assert redeem(web, handle).json()["code"] == "slow_down"
        assert rows()[0]["interval"] == 10
        frozen.tick(6)
        assert redeem(web, handle).json()["code"] == "slow_down"  # 10 s from then.
        assert rows()[0]["interval"] == 15  # Each slow_down adds 5 s more.
        frozen.tick(15)
        assert redeem(web, handle).json()["code"] == "authorization_pending"
        frozen.tick(121)
        assert redeem(web, handle).json()["code"] == "expired_token"


def test_a_wrong_number_denies_the_sign_in(app: App) -> None:
    """One try: the request is denied, the page is told so."""
    web = app()
    phone = Phone(app(PHONE))
    started = start_push(web)
    [waiting] = phone.waiting()
    wrong = "00" if started["match_code"] != "00" else "01"

    answer = phone.approve(waiting["id"], match_code=wrong)
    assert answer.status_code == 422
    assert answer.json()["code"] == "wrong_match_code"
    assert phone.waiting() == []
    assert redeem(web, started["request"]).json()["code"] == "access_denied"
    assert phone.approve(waiting["id"], match_code=started["match_code"]).json()[
        "code"
    ] == ("sign_in_not_found")
    assert len(audited("ciba.sign_in_denied")) == 1


def test_without_number_matching_the_phone_approves_alone(app: App) -> None:
    """``NUMBER_MATCHING = False``: no number on the page, none asked."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    started = start_push(web)
    assert "match_code" not in started
    [waiting] = phone.waiting()
    assert waiting["match_required"] is False
    assert phone.approve(waiting["id"]).status_code == 204
    assert redeem(web, started["request"]).status_code == 200


@pytest.mark.parametrize(
    "who", ["nobody", "unbound", "inactive"], ids=["unknown", "unbound", "inactive"]
)
def test_users_who_cannot_be_asked_get_a_decoy(app: App, who: str) -> None:
    """The same answer; no push; pending until it expires; nobody can approve."""
    web = app()
    if who == "unbound":
        create_user("bob")
    if who == "inactive":
        Phone(app(PHONE), "bob")
        run_async(_deactivate("bob"))
    with clock.freeze_time("2026-11-05T10:00:00Z") as frozen:
        started = start_push(web, "bob")
        assert set(started) == {"request", "expires_in", "interval", "match_code"}
        assert sign_in_pushes() == []
        assert redeem(web, started["request"]).json()["code"] == (
            "authorization_pending"
        )
        frozen.tick(121)
        assert redeem(web, started["request"]).json()["code"] == "expired_token"
    [row] = rows()
    assert row["decoy"] is True
    assert row["user_id"] is None


async def _deactivate(username: str) -> None:
    """Turn ``username``'s account off."""
    from domainkit.contrib.auth import AuthUnitOfWork

    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        user.is_active = False
        await uow.commit()


def test_past_max_pending_no_push_reaches_the_user(app: App) -> None:
    """Three wait; the fourth is a decoy; answering one makes room."""
    web = app()
    phone = Phone(app(PHONE))
    for _ in range(4):
        start_push(web)
    assert len(sign_in_pushes()) == 3
    assert len(phone.waiting()) == 3
    phone.deny(phone.waiting()[0]["id"])
    start_push(web)
    assert len(sign_in_pushes()) == 4


def test_a_report_denies_every_waiting_sign_in_and_pauses_pushes(app: App) -> None:
    """Reported: all denied, audited; pushes become decoys for the cooldown."""
    web = app()
    phone = Phone(app(PHONE))
    first, second = start_push(web), start_push(web)
    answer = phone.deny(phone.waiting()[0]["id"], report=True)
    assert answer.status_code == 204
    assert phone.waiting() == []
    for started in (first, second):
        assert redeem(web, started["request"]).json()["code"] == "access_denied"
    assert len(audited("ciba.sign_in_reported")) == 1

    start_push(web)
    assert len(sign_in_pushes()) == 2  # The two before the report.
    with clock.freeze_time(clock.now() + timedelta(minutes=16)):
        start_push(web)
    assert len(sign_in_pushes()) == 3


def test_the_phone_answers_only_its_users_waiting_sign_ins(app: App) -> None:
    """Another user's request, an expired one, or without the device: refused."""
    web = app()
    alice = Phone(app(PHONE))
    bob = Phone(app(PHONE), "bob")
    start_push(web, "alice")
    [hers] = alice.waiting()
    assert bob.waiting() == []
    refused = bob.approve(hers["id"], match_code="00")
    assert refused.status_code == 404
    assert refused.json()["code"] == "sign_in_not_found"
    unsigned = alice.client.get("/auth/app-sign-in/requests", headers=alice.headers)
    assert unsigned.status_code == 401
    assert unsigned.json()["code"] == "device_signature_refused"
    with clock.freeze_time(clock.now() + timedelta(minutes=3)):
        assert alice.deny(hers["id"]).status_code == 404


def test_starts_are_limited_per_address(app: App) -> None:
    """``START_RATE`` per address: the eleventh start in a minute is refused."""
    web = app(START_RATE="2/minute")
    for _ in range(2):
        start_push(web, "nobody")
    refused = web.post("/auth/app-sign-in/qr")
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"


# QR.


def test_a_qr_sign_in_is_claimed_approved_and_becomes_a_session(app: App) -> None:
    """The phone scans, sees the page's address, approves; the browser gets cookies."""
    web = app()
    phone = Phone(app(LAPTOP))  # Same network as the page.
    started = web.post("/auth/app-sign-in/qr", headers={"User-Agent": AGENT}).json()
    assert set(started) == {"request", "expires_in", "interval", "qr", "qr_expires_in"}
    assert started["qr_expires_in"] == 60  # To scan it,
    assert started["expires_in"] == 180  # then REQUEST_TTL to answer.
    assert started["request"] not in started["qr"]
    assert sign_in_pushes() == []

    claimed = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    )
    assert claimed.status_code == 200, claimed.json()
    shown = claimed.json()
    assert shown["channel"] == "qr"
    assert shown["same_network"] is True
    assert shown["browser"] == AGENT
    assert shown["match_required"] is False
    again = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    )
    assert again.status_code == 422
    assert again.json()["code"] == "qr_code_expired"

    assert phone.approve(shown["id"]).status_code == 204
    session = redeem(web, started["request"], "session")
    assert session.status_code == 200, session.json()
    assert "csrf_token" in session.json()
    assert any(
        cookie.startswith("__Host-dk_session=")
        for cookie in session.headers.get_list("set-cookie")
    )


def test_a_qr_code_holds_the_format_and_expires(app: App) -> None:
    """``QR_FORMAT`` wraps the code; the phone may send either; it expires."""
    web = app(QR_FORMAT="https://id.example.com/qr#{code}")
    phone = Phone(app(PHONE))
    with clock.freeze_time("2026-11-05T10:00:00Z") as frozen:
        first = web.post("/auth/app-sign-in/qr").json()
        assert first["qr"].startswith("https://id.example.com/qr#")
        code = first["qr"].removeprefix("https://id.example.com/qr#")
        frozen.tick(61)
        late = phone.send("POST", "/auth/app-sign-in/requests/claim", {"code": code})
        assert late.json()["code"] == "qr_code_expired"
        assert redeem(web, first["request"]).json()["code"] == "expired_token"
        second = web.post("/auth/app-sign-in/qr").json()
        bare = second["qr"].removeprefix("https://id.example.com/qr#")
        frozen.tick(1)  # Each signed request is later than the last.
        claimed = phone.send("POST", "/auth/app-sign-in/requests/claim", {"code": bare})
        assert claimed.status_code == 200
        assert claimed.json()["same_network"] is False


def test_a_qr_code_from_another_network_can_be_refused(app: App) -> None:
    """``QR_SAME_NETWORK = "require"``: the phone elsewhere cannot claim it."""
    web = app(QR_SAME_NETWORK="require")
    phone = Phone(app(PHONE, QR_SAME_NETWORK="require"))
    started = web.post("/auth/app-sign-in/qr").json()
    refused = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "different_network"
    near = Phone(app(LAPTOP, QR_SAME_NETWORK="require"), "bob")
    claimed = near.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    )
    assert claimed.status_code == 200


def test_a_handle_works_only_for_what_it_started(app: App) -> None:
    """Unknown handles are refused; a denied QR request answers access_denied."""
    web = app()
    phone = Phone(app(PHONE))
    assert redeem(web, "x" * 43).json()["code"] == "sign_in_unknown"
    started = web.post("/auth/app-sign-in/qr").json()
    shown = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    ).json()
    assert phone.deny(shown["id"]).status_code == 204
    assert redeem(web, started["request"], "session").json()["code"] == (
        "access_denied"
    )


def test_an_approved_user_turned_off_meanwhile_gets_no_login(app: App) -> None:
    """Approved, then deactivated before the poll: the handle is spent, no tokens."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    started = start_push(web)
    phone.approve(phone.waiting()[0]["id"])
    run_async(_deactivate("alice"))
    refused = redeem(web, started["request"])
    assert refused.status_code == 400
    assert refused.json()["code"] == "sign_in_unknown"


# The mobile app as identity.


def test_with_approve_logins_the_phone_is_the_only_way_in(app: App) -> None:
    """Bound: the web is refused, the phone's signed login works; unbound: as before."""
    web = app(APPROVE_LOGINS=True)
    create_user("bob")
    assert (
        web.post(
            "/auth/login", json={"identifier": "bob", "password": PASSWORD}
        ).status_code
        == 200
    )  # No bound device: the app's first login binds one.

    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    refused = web.post(
        "/auth/login", json={"identifier": "alice", "password": PASSWORD}
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "phone_approval_required"
    session = web.post(
        "/auth/session",
        json={"identifier": "alice", "password": PASSWORD},
        headers=TRUSTED,
    )
    assert session.json()["code"] == "phone_approval_required"

    signed = phone.send(
        "POST", "/auth/login", {"identifier": "alice", "password": PASSWORD}
    )
    assert signed.status_code == 200, signed.json()

    started = start_push(web)
    phone.approve(phone.waiting()[0]["id"], match_code=started["match_code"])
    assert redeem(web, started["request"]).status_code == 200


def test_without_approve_logins_bound_users_log_in_anywhere(app: App) -> None:
    """The default: the gate lets every login through."""
    web = app()
    Phone(app(PHONE))
    answer = web.post("/auth/login", json={"identifier": "alice", "password": PASSWORD})
    assert answer.status_code == 200


def test_requests_name_their_user_by_id_only(app: App) -> None:
    """The row keeps the user's id; the page never sees it."""
    web = app()
    phone = Phone(app(PHONE))
    started = start_push(web)
    [row] = rows()
    assert row["user_id"] == phone.user_id
    assert str(phone.user_id) not in json.dumps(started)
    assert isinstance(row["id"], UUID)


# The app's own logins.


def test_a_bound_device_logs_in_by_its_signature(app: App) -> None:
    """``DEVICE_LOGIN``: the signature is the login; past the gate too."""
    from domainkit.conf import override_settings as override

    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    off = phone.send("POST", "/auth/devices/login")
    assert off.status_code == 404
    assert off.json()["code"] == "device_login_off"
    with override(DEVICES={"DEVICE_LOGIN": True}):
        answer = phone.send("POST", "/auth/devices/login")
        assert answer.status_code == 200, answer.json()
        assert answer.headers["Cache-Control"] == "no-store"
        me = phone.client.get("/auth/me", headers=bearer(answer.json()["access_token"]))
        assert me.json()["username"] == "alice"

        replayed = phone.client.post(
            "/auth/devices/login",
            headers=phone.device.headers(
                "POST", "/auth/devices/login", when=int(clock.now().timestamp() * 1000)
            )
            | {"X-Device-Id": "not-a-uuid"},
        )
        assert replayed.status_code == 401
        assert replayed.json()["code"] == "device_signature_refused"
        stranger = Device()
        stranger.id = phone.device.id
        forged = phone.client.post(
            "/auth/devices/login",
            headers=stranger.headers(
                "POST",
                "/auth/devices/login",
                when=int(clock.now().timestamp() * 1000) + 1,
            ),
        )
        assert forged.status_code == 401
        assert len(audited("devices.login")) == 2


def test_unbinddevices_lets_a_user_who_lost_the_phone_back_in(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staff unbind: sessions end; the password works on the web again."""
    import io

    from domainkit.management import call_command
    from domainkit.management.base import CommandError

    monkeypatch.setenv("DOMAINKIT_OPERATOR", "mallory")
    web = app(APPROVE_LOGINS=True)
    phone = Phone(app(PHONE))
    out = io.StringIO()

    call_command("unbinddevices", "alice", stdout=out)
    call_command("unbinddevices", "alice", stdout=out)

    assert out.getvalue() == (
        "Unbound 1 device(s) of 'alice'; they log in again.\n"
        "'alice' had no bound device.\n"
    )
    assert phone.client.get("/auth/me", headers=phone.headers).status_code == 401
    assert (
        web.post(
            "/auth/login", json={"identifier": "alice", "password": PASSWORD}
        ).status_code
        == 200
    )
    assert audited("devices.device_unbound")[0]["actor"] == "operator:mallory"
    with pytest.raises(CommandError, match="does not exist"):
        call_command("unbinddevices", "nobody", stdout=out)


# Housekeeping.


def test_ended_requests_are_purged_and_deleted_users_forgotten(app: App) -> None:
    """A day after they end, requests go; a deleted user's go at once."""
    from domainkit.contrib.ciba.apps import forget_requests
    from domainkit.contrib.ciba.tasks import purge

    web = app()
    phone = Phone(app(PHONE))
    with clock.freeze_time("2026-11-05T10:00:00Z") as frozen:
        start_push(web, "nobody")
        frozen.tick(timedelta(days=1, minutes=3))
        start_push(web)
        run_async(purge())
    [kept] = rows()
    assert kept["user_id"] == phone.user_id

    class Deleted:
        user_id: UUID = phone.user_id

    run_async(forget_requests(Deleted()))
    assert rows() == []


def test_privacy_exports_and_erases_a_users_requests(app: App) -> None:
    """Export lists when, how, from where; erase removes them; others untouched."""
    from domainkit.apps import registry

    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    start_push(web)
    phone.approve(phone.waiting()[0]["id"])
    start_push(web, "nobody")
    config = registry.get_domain_config("ciba")
    subject = str(phone.user_id)

    exported = run_async(config.export_person(subject))  # type: ignore[attr-defined]
    [entry] = exported["sign_in_requests"]
    assert entry["channel"] == "push"
    assert entry["address"] == LAPTOP[0]
    assert entry["browser"] == AGENT
    assert entry["approved_at"] is not None
    assert entry["denied_at"] is None
    assert run_async(config.export_person("not-a-user")) == {}  # type: ignore[attr-defined]
    assert run_async(config.export_person(str(uuid4()))) == {}  # type: ignore[attr-defined]

    erased = run_async(config.erase_person(subject, "request"))  # type: ignore[attr-defined]
    assert erased == {"sign_in_requests": 1}
    assert run_async(config.erase_person(subject, "request")) == {}  # type: ignore[attr-defined]
    assert run_async(config.erase_person("x", "request")) == {}  # type: ignore[attr-defined]
    assert len(rows()) == 1  # The decoy names nobody.


def test_the_routes_served_are_checked(app: App) -> None:
    """E002: ID tokens without an asymmetric key; E003: identity mode, no routes."""
    from fastapi import FastAPI

    from domainkit.apps import registry
    from domainkit.http import get_asgi_application

    app()
    config = registry.get_domain_config("ciba")
    problems = config.application_problems(get_asgi_application())  # type: ignore[attr-defined]
    assert [problem.id for problem in problems] == ["ciba.E002"]
    assert config.application_problems(FastAPI()) == []  # type: ignore[attr-defined]
    with override_settings(CIBA={"APPROVE_LOGINS": True}):
        bare = config.application_problems(FastAPI())  # type: ignore[attr-defined]
        assert [problem.id for problem in bare] == ["ciba.E003"]


def test_ciba_lives_with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routed to another database than auth's, the app refuses to start."""
    import domainkit.contrib.ciba as ciba_module
    from domainkit.contrib.ciba.apps import CibaConfig
    from domainkit.db.routing import router
    from domainkit.exceptions import ImproperlyConfigured

    monkeypatch.setattr(router, "target_for", lambda label: label)
    config = CibaConfig("domainkit.contrib.ciba", ciba_module)
    with pytest.raises(ImproperlyConfigured, match="routed with 'auth'"):
        config.ready()


def test_settings_are_checked() -> None:
    """The QR format holds the code once; rates read."""
    from pydantic import ValidationError

    from domainkit.contrib.ciba import CibaSettings

    for text in ("no code", "{code}{code}", "{code}{x}"):
        with pytest.raises(ValidationError, match="holds"):
            CibaSettings(QR_FORMAT=text)
    with pytest.raises(ValidationError, match="rate"):
        CibaSettings(START_RATE="lots")
    assert CibaSettings(START_RATE=None, CLIENT_RATE=None).START_RATE is None


def test_reprs_keep_secrets_out(app: App) -> None:
    """Requests, starts and clients show ids and states, never digests or codes."""
    from domainkit.contrib.ciba.clients import register as register_client

    web = app()
    Phone(app(PHONE))
    start_push(web)
    from domainkit.contrib.ciba import CibaUnitOfWork

    request_id = rows()[0]["id"]

    async def stored() -> Any:  # noqa: ANN401 - a request.
        async with CibaUnitOfWork() as uow:
            return await uow.requests.get(request_id)

    request = run_async(stored())
    assert repr(request).startswith("SignInRequest(id=")
    assert "pending" in repr(request)
    assert request.handle_digest not in repr(request)
    secret = "s" * 40
    client = run_async(
        register_client("Acme", method="client_secret_post", secret=secret)
    )
    assert client.secret_digest is not None
    assert client.secret_digest not in repr(client)
    assert "Acme" in repr(client)


# Races: the guarded update that matched no row lost to another answer.


@pytest.mark.parametrize("lost", ["claim", "approve", "deny", "redeem"])
def test_an_answer_that_loses_a_race_is_refused(
    app: App, monkeypatch: pytest.MonkeyPatch, lost: str
) -> None:
    """Each state change is one guarded update: the loser gets the refusal."""
    from domainkit.contrib.ciba.stores import SignInRequestStore

    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    started = web.post("/auth/app-sign-in/qr").json()
    claim_path = "/auth/app-sign-in/requests/claim"
    if lost != "claim":
        shown = phone.send("POST", claim_path, {"code": started["qr"]}).json()
    if lost == "redeem":
        phone.approve(shown["id"])

    async def lose(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(SignInRequestStore, lost, lose)
    if lost == "claim":
        answer = phone.send("POST", claim_path, {"code": started["qr"]})
        assert answer.json()["code"] == "qr_code_expired"
    elif lost == "approve":
        assert phone.approve(shown["id"]).json()["code"] == "sign_in_not_found"
    elif lost == "deny":
        assert phone.deny(shown["id"]).json()["code"] == "sign_in_not_found"
    else:
        assert redeem(web, started["request"]).json()["code"] == "sign_in_unknown"


def test_a_requests_state_follows_its_answers(app: App) -> None:
    """pending, claimed, approved, denied, redeemed: as the row says."""
    from dataclasses import replace

    from domainkit.contrib.ciba import CibaUnitOfWork
    from domainkit.contrib.ciba.services import Started

    web = app()
    Phone(app(PHONE))
    start_push(web)
    request_id = rows()[0]["id"]

    async def stored() -> Any:  # noqa: ANN401 - a request.
        async with CibaUnitOfWork() as uow:
            return await uow.requests.get(request_id)

    request = run_async(stored())
    now = clock.now()
    assert request.state == "pending"
    assert replace(request, claimed_at=now).state == "claimed"
    assert replace(request, denied_at=now).state == "denied"
    assert replace(request, approved_at=now).state == "approved"
    assert replace(request, approved_at=now, redeemed_at=now).state == "redeemed"
    started = Started(handle="h" * 43, request_id=request_id, expires_in=1, interval=5)
    assert repr(started) == f"Started(request_id={request_id!r})"


def test_a_turned_off_users_device_logs_no_one_in(app: App) -> None:
    """The device signs, but its user is inactive: refused like a bad signature."""
    phone = Phone(app(PHONE))
    with override_settings(DEVICES={"DEVICE_LOGIN": True}):
        run_async(_deactivate("alice"))
        moment = int(clock.now().timestamp() * 1000)
        answer = phone.client.post(  # No bearer: the device alone logs in.
            "/auth/devices/login",
            headers=phone.device.headers("POST", "/auth/devices/login", when=moment),
        )
    assert answer.status_code == 401
    assert answer.json()["code"] == "device_signature_refused"


def test_a_qr_code_scanned_late_still_leaves_time_to_answer(app: App) -> None:
    """Scanned at 55 s: the phone has REQUEST_TTL from then, not 5 s."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    with clock.freeze_time("2026-11-08T10:00:00Z") as frozen:
        started = web.post("/auth/app-sign-in/qr").json()
        frozen.tick(55)
        shown = phone.send(
            "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
        )
        assert shown.status_code == 200, shown.json()
        frozen.tick(30)
        assert redeem(web, started["request"]).json()["code"] == (
            "authorization_pending"
        )
        assert phone.approve(shown.json()["id"]).status_code == 204
        frozen.tick(6)
        assert redeem(web, started["request"]).status_code == 200
    with clock.freeze_time("2026-11-08T11:00:00Z") as frozen:
        unanswered = web.post("/auth/app-sign-in/qr").json()
        frozen.tick(30)
        claimed = phone.send(
            "POST", "/auth/app-sign-in/requests/claim", {"code": unanswered["qr"]}
        )
        frozen.tick(151)  # Past QR_TTL + REQUEST_TTL: nobody answered.
        assert phone.approve(claimed.json()["id"]).status_code == 404
        assert redeem(web, unanswered["request"]).json()["code"] == "expired_token"


def test_other_domains_gates_judge_approved_sign_ins_too(app: App) -> None:
    """The phone's approval passes ciba's gate, not every gate."""
    from fastapi import status as http_status
    from starlette.requests import Request

    from domainkit.contrib.auth import AbstractBaseUser
    from domainkit.contrib.auth.gates import on_login_gate
    from domainkit.http.errors import APIError

    web = app(NUMBER_MATCHING=False, APPROVE_LOGINS=True)
    phone = Phone(app(PHONE))
    judged: list[str] = []

    async def office_hours(request: Request, user: AbstractBaseUser) -> None:
        judged.append(request.url.path)
        raise APIError(http_status.HTTP_403_FORBIDDEN, "Closed.", code="closed")

    on_login_gate(office_hours, domain="ciba")
    for route in ("token", "session"):
        started = start_push(web)
        phone.approve(phone.waiting()[0]["id"])
        refused = redeem(web, started["request"], route)
        assert refused.status_code == 403
        assert refused.json()["code"] == "closed"
    assert judged == ["/auth/app-sign-in/token", "/auth/app-sign-in/session"]


def test_a_session_route_needs_the_cookie_class(app: App) -> None:
    """E004: /app-sign-in/session served without CookieAuthentication."""
    from domainkit.apps import registry
    from domainkit.http import get_asgi_application
    from domainkit_tests.conftest import IDENTITY

    app()
    config = registry.get_domain_config("ciba")
    auth = {
        **IDENTITY.settings["AUTH"],
        "AUTHENTICATION_CLASSES": [
            "domainkit.contrib.auth.authentication.TokenAuthentication"
        ],
    }
    with override_settings(AUTH=auth):
        problems = config.application_problems(get_asgi_application())  # type: ignore[attr-defined]
    assert "ciba.E004" in [problem.id for problem in problems]


def test_concurrent_polls_redeem_once(app: App) -> None:
    """Eight polls race on an approved request: exactly one redeems it."""
    import asyncio

    from domainkit.contrib.ciba import (
        CibaUnitOfWork,
        PollRefused,
        Requester,
        approve,
        poll,
    )
    from domainkit.contrib.ciba import (
        start_push as start,
    )

    app(NUMBER_MATCHING=False)
    if not connections.config().URL.get_secret_value().startswith("postgresql"):
        pytest.skip("concurrent writes need PostgreSQL: SQLite runs one at a time")
    Phone(app(PHONE))

    async def scenario() -> list[str]:
        async with CibaUnitOfWork() as uow:
            user = await uow.users.find_by_identifier("alice")
            assert user is not None
            started = await start(uow, user, requester=Requester(None, None))
            await uow.commit()
        async with CibaUnitOfWork() as uow:
            device = (await uow.devices.of_user(user.id))[0]
            await approve(uow, user.id, started.request_id, device.id)
            await uow.commit()

        async def one() -> str:
            try:
                await poll(started.handle, client_id=None)
            except PollRefused as exc:
                return exc.code
            return "redeemed"

        return list(await asyncio.gather(*(one() for _ in range(8))))

    results = run_async(scenario())
    assert results.count("redeemed") == 1
    assert set(results) <= {"redeemed", "sign_in_unknown", "slow_down"}


def test_a_concurrent_approve_and_deny_have_one_winner(app: App) -> None:
    """Approvals and denials race: one answer wins, the others are refused."""
    import asyncio

    from domainkit.contrib.ciba import (
        CibaUnitOfWork,
        Requester,
        SignInNotFound,
        approve,
        deny,
    )
    from domainkit.contrib.ciba import (
        start_push as start,
    )

    app(NUMBER_MATCHING=False)
    if not connections.config().URL.get_secret_value().startswith("postgresql"):
        pytest.skip("concurrent writes need PostgreSQL: SQLite runs one at a time")
    Phone(app(PHONE))

    async def scenario() -> list[str]:
        async with CibaUnitOfWork() as uow:
            user = await uow.users.find_by_identifier("alice")
            assert user is not None
            started = await start(uow, user, requester=Requester(None, None))
            await uow.commit()
            device = (await uow.devices.of_user(user.id))[0]

        async def answer(yes: bool) -> str:
            try:
                async with CibaUnitOfWork() as uow:
                    if yes:
                        await approve(uow, user.id, started.request_id, device.id)
                    else:
                        await deny(uow, user.id, started.request_id)
                    await uow.commit()
            except SignInNotFound:
                return "refused"
            return "approved" if yes else "denied"

        return list(await asyncio.gather(*(answer(i % 2 == 0) for i in range(6))))

    results = run_async(scenario())
    assert results.count("refused") == 5
    [row] = rows()
    assert (row["approved_at"] is None) != (row["denied_at"] is None)

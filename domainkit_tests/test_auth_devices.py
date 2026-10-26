"""Device proofs: strangers' wrong passwords no longer keep a user out (SEC-r1-2).

Before 0.10, ``LOGIN_RATE`` wrong passwords for alice, from any addresses,
got her right password refused (429) for as long as they were sent, and
as many from her office's address kept everyone there out. A client that
logged in to alice's account now holds a signed proof, and its logins are
counted on its own: the attacker's count fills, hers does not. Guessing
her password stays bounded as before: without a proof, every attempt
counts per account and per address.
"""

from datetime import timedelta
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthUnitOfWork, UserId, devices
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    stored_tokens,
)
from domainkit_tests.test_auth_mfa import clock as clock
from domainkit_tests.test_auth_mfa import code_at, enrol

SITE: Final = "https://testserver"
RIGHT: Final = {"username": "alice", "password": PASSWORD}


def browser(app: Any, address: str) -> TestClient:  # noqa: ANN401 - an application.
    """A client at ``address`` that keeps the cookies it is given (over HTTPS)."""
    return TestClient(app, base_url=SITE, client=(address, 1))


def guess(app: Any, address: str, username: str = "alice") -> int:  # noqa: ANN401 - an application.
    """A wrong password for ``username`` from ``address``, without a proof."""
    response = browser(app, address).post(
        "/auth/login", json={"username": username, "password": f"x-{address}"}
    )
    return response.status_code


def test_strangers_wrong_passwords_keep_a_proven_client_in(api: Api) -> None:
    """The PoC of SEC-r1-2, both attacks, with device proofs.

    The attacker fills alice's count from three addresses: their next guess
    is refused, as before, so the guessing bound holds. Alice's browser,
    which logged in last week, gets in; a new browser of hers is held, as
    before 0.10.
    """
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    alice = browser(client.app, "203.0.113.50")
    assert alice.post("/auth/login", json=RIGHT).status_code == 200
    assert devices.cookie_name() in alice.cookies

    guesses = [guess(client.app, f"198.51.100.{n}") for n in range(1, 5)]
    proven = [alice.post("/auth/login", json=RIGHT).status_code for _ in range(3)]
    new_browser = browser(client.app, "203.0.113.50").post("/auth/login", json=RIGHT)

    assert guesses == [401, 401, 429, 429]
    assert proven == [200, 200, 200]
    assert new_browser.status_code == 429


def test_an_address_full_of_failures_keeps_a_proven_client_in(api: Api) -> None:
    """The office: one person there fills the address's count, for others' names.

    Alice's browser gets in; a colleague's new browser is held, as before.
    """
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    office = "192.0.2.10"
    alice = browser(client.app, office)
    assert alice.post("/auth/login", json=RIGHT).status_code == 200

    spray = [guess(client.app, office, name) for name in ("bob", "carol", "dan")]

    assert spray == [401, 401, 429]
    assert alice.post("/auth/login", json=RIGHT).status_code == 200
    assert browser(client.app, office).post("/auth/login", json=RIGHT).status_code == (
        429
    )


def test_a_proven_client_has_a_count_of_its_own(api: Api) -> None:
    """Wrong passwords with a proof count per device: it buys no more guesses.

    A proof made for another account counts as none; neither does a forged
    or an expired one: they are counted on the account, full here.
    """
    create_user("alice")
    create_user("mallory")
    client = api(LOGIN_RATE="2/minute")
    alice = browser(client.app, "203.0.113.50")
    mallory = browser(client.app, "198.51.100.66")
    wrong = {"username": "alice", "password": "wrong"}

    def fill_the_account() -> list[int]:
        return [guess(client.app, f"198.51.100.{n}") for n in (1, 2)]

    with freeze_time("2026-10-26T10:00:00Z") as clock:
        assert alice.post("/auth/login", json=RIGHT).status_code == 200
        proof = alice.cookies[devices.cookie_name()]
        assert fill_the_account() == [401, 401]
        own = [alice.post("/auth/login", json=wrong).status_code for _ in range(3)]
        assert own == [401, 401, 429]  # The device's own count, as strict.

        mallory.post("/auth/login", json={"username": "mallory", "password": PASSWORD})
        assert mallory.post("/auth/login", json=wrong).status_code == 429
        forged = browser(client.app, "198.51.100.67")
        forged.cookies.set(devices.cookie_name(), proof[:-2] + "xx")
        assert forged.post("/auth/login", json=RIGHT).status_code == 429

        clock.tick(timedelta(days=29))
        assert fill_the_account() == [401, 401]
        assert alice.post("/auth/login", json=RIGHT).status_code == 200
        stale = browser(client.app, "203.0.113.50")
        stale.cookies.set(devices.cookie_name(), proof)
        clock.tick(timedelta(days=1, seconds=1))  # The first proof, 30 days old.
        assert fill_the_account() == [401, 401]
        assert stale.post("/auth/login", json=RIGHT).status_code == 429
        assert alice.post("/auth/login", json=RIGHT).status_code == 200  # Renewed.


def test_proofs_are_secure_http_only_cookies_for_one_account(api: Api) -> None:
    """``__Host-dk_device``: Secure, HttpOnly, ``SameSite=Lax``, for ``DEVICE_TTL``.

    Browser sessions hand one out too, at the password step, second factor
    or not. ``DEVICE_TTL = None`` hands none out, as before 0.10.
    """
    create_user("alice")
    client = api(
        AUTHENTICATION_CLASSES=[
            "domainkit.contrib.auth.authentication.TokenAuthentication",
            "domainkit.contrib.auth.cookies.CookieAuthentication",
        ]
    )
    response = browser(client.app, "203.0.113.50").post("/auth/login", json=RIGHT)

    header = response.headers["set-cookie"]
    assert header.startswith("__Host-dk_device=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute in header
    assert f"Max-Age={30 * 24 * 3600}" in header
    assert "alice" not in header

    with override_settings(AUTH={"DEVICE_TTL": None}):
        quiet = browser(client.app, "203.0.113.51").post("/auth/login", json=RIGHT)
    assert "dk_device" not in quiet.headers.get("set-cookie", "")


def test_the_cookie_takes_no_prefix_where_it_cannot_be_secure(api: Api) -> None:
    """Over plain HTTP (``COOKIE.SECURE = False``) it is ``dk_device``."""
    create_user("alice")
    client = api(COOKIE={"SECURE": False})
    response = client.post("/auth/login", json=RIGHT)

    assert devices.cookie_name() == "dk_device"
    assert response.headers["set-cookie"].startswith("dk_device=")
    assert "Secure" not in response.headers["set-cookie"]


def test_proofs_are_read_only_when_they_could_be_one(api: Api) -> None:
    """Long cookies are not checked; without ``DEVICE_TTL`` none is."""
    api()
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"cookie", b"__Host-dk_device=" + b"a" * 300)],
    }
    assert run_async(devices.proven_device(Request(scope), "alice")) is None
    with override_settings(AUTH={"DEVICE_TTL": None}):
        assert run_async(devices.proven_device(Request(scope), "alice")) is None
    assert audited("auth.login") == []


@pytest.mark.parametrize("ttl", [timedelta(minutes=59), timedelta(days=401)])
def test_the_device_lifetime_is_bounded(api: Api, ttl: timedelta) -> None:
    """From an hour to 400 days, the longest browsers keep a cookie."""
    with pytest.raises(Exception, match="DEVICE_TTL"):
        api(DEVICE_TTL=ttl)


def mint(app: Any, count: int) -> list[str]:  # noqa: ANN401 - an application.
    """Proofs of ``count`` fresh clients that logged in as alice."""
    proofs = []
    for _ in range(count):
        fresh = browser(app, "198.51.100.7")
        assert fresh.post("/auth/login", json=RIGHT).status_code == 200
        proofs.append(fresh.cookies[devices.cookie_name()])
    return proofs


def guesses_with(app: Any, proofs: list[str], each: int = 2) -> list[int]:  # noqa: ANN401 - an application.
    """``each`` wrong passwords sent with every proof, from one address."""
    answers = []
    for n, proof in enumerate(proofs):
        holder = browser(app, "198.51.100.7")
        holder.cookies.set(devices.cookie_name(), proof)
        for guess in range(each):
            wrong = {"username": "alice", "password": f"guess-{n}-{guess}"}
            answers.append(holder.post("/auth/login", json=wrong).status_code)
    return answers


def test_a_password_change_ends_every_proof(api: Api) -> None:
    """SEC-10a-3: proofs minted while the password was known used to outlive it.

    Six of them checked 12 wrong guesses after the change, at a
    ``LOGIN_RATE`` of 2; now they count as none, on the account's count.
    """
    alice = create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    proofs = mint(client.app, 6)
    assert len(set(proofs)) == 6
    assert len(stored_tokens(alice, devices=True)) - len(stored_tokens(alice)) == 6

    async def change() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(alice))
            await uow.users.set_password(user, "a-brand-new-Passw0rd!")
            await uow.tokens.revoke_user(user.id)
            await uow.commit()

    run_async(change())

    answers = guesses_with(client.app, proofs)
    assert answers.count(401) == 2
    assert stored_tokens(alice, devices=True) == []


def test_logging_out_everywhere_ends_every_proof(api: Api) -> None:
    """A device belongs to the user's tokens: ending them all ends it."""
    alice = create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    alice_browser = browser(client.app, "203.0.113.50")
    token = alice_browser.post("/auth/login", json=RIGHT).json()["access_token"]
    assert (
        alice_browser.post(
            "/auth/logout", json={"everywhere": True}, headers=bearer(token)
        ).status_code
        == 204
    )

    assert stored_tokens(alice, devices=True) == []
    assert [guess(client.app, f"198.51.100.{n}") for n in (1, 2)] == [401, 401]
    assert alice_browser.post("/auth/login", json=RIGHT).status_code == 429


def test_proven_attempts_of_an_account_have_a_ceiling(api: Api) -> None:
    """However many proofs one holds, they check two ``LOGIN_RATE`` s in all.

    The account's own count stays apart, so strangers still cannot fill
    the ceiling, and one copied proof cannot fill it alone either.
    """
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    proofs = mint(client.app, 4)

    answers = guesses_with(client.app, proofs)

    assert answers.count(401) == 2 * devices.PROVEN_CEILING
    assert set(answers) == {401, 429}
    assert guess(client.app, "198.51.100.9") == 401  # The account's count.


def test_a_copied_proof_alone_cannot_keep_the_user_out(api: Api) -> None:
    """The ceiling is twice a device's count: one proof fills only its own."""
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    stolen, own = mint(client.app, 2)
    assert guesses_with(client.app, [stolen], each=3) == [401, 401, 429]

    alice = browser(client.app, "203.0.113.50")
    alice.cookies.set(devices.cookie_name(), own)
    assert alice.post("/auth/login", json=RIGHT).status_code == 200


def test_a_user_keeps_at_most_max_devices(api: Api) -> None:
    """SEC-10a-3: proofs were minted without limit; past the cap the oldest ends.

    The oldest proof then counts as none: its wrong passwords take the
    account's count, which ``LOGIN_RATE`` fills.
    """
    alice = create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    oldest, *rest = mint(client.app, devices.MAX_DEVICES + 1)

    kept = len(stored_tokens(alice, devices=True)) - len(stored_tokens(alice))
    assert kept == devices.MAX_DEVICES
    assert guesses_with(client.app, [oldest]) == [401, 401]
    assert guess(client.app, "198.51.100.9") == 429  # The account's count.
    assert guesses_with(client.app, rest[-1:]) == [401, 401]  # Its own.


def test_without_a_login_rate_a_proof_counts_without_limit(api: Api) -> None:
    """``LOGIN_RATE = None`` limits nothing, proven or not (``auth.W002``)."""
    create_user("alice")
    client = api(LOGIN_RATE=None)
    proofs = mint(client.app, 1)

    assert devices.proven_limits("d", "alice", None) == [("login:device:d", None)]
    assert guesses_with(client.app, proofs, each=4) == [401] * 4


def test_proofs_wait_for_the_second_factor(identity: Api, clock: list[float]) -> None:
    """SEC-10a-3: a password without its second factor used to mint a proof."""
    alice = create_user("alice")
    client = identity()
    secret, _ = enrol(client, bearer(login_token(client)), clock)
    before = len(stored_tokens(alice, devices=True)) - len(stored_tokens(alice))
    caller = browser(client.app, "198.51.100.7")

    pending = caller.post("/auth/login", json=RIGHT)
    assert pending.status_code == 202
    assert devices.cookie_name() not in caller.cookies
    clock[0] += 30  # The enrolment used this step's code.
    finished = caller.post(
        "/auth/mfa/login",
        json={
            "mfa_token": pending.json()["mfa_token"],
            "code": code_at(secret, clock[0]),
        },
    )

    assert finished.status_code == 200
    assert devices.cookie_name() in caller.cookies
    after = len(stored_tokens(alice, devices=True)) - len(stored_tokens(alice))
    assert after == before + 1


def login_token(client: TestClient) -> str:
    """Alice's access token, before she has a second factor."""
    token: str = client.post("/auth/login", json=RIGHT).json()["access_token"]
    return token

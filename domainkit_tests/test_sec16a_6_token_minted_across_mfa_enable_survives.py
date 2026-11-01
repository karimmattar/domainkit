"""SEC-16a-6: a token minted while MFA is turned on outlives "every login ends".

The fix wave makes ``POST /mfa/totp/confirm`` end every other login and
token of the user (``revoke_user(keep=current)``): what logged in with the
password alone, before the factor, "may not be the user's". The revocation
is a ``DELETE`` of the rows that exist when it runs. ``POST /auth/tokens``
decides how to re-authenticate (``uow.mfa.enabled``: no factor, so the
password) in one unit of work, checks the password (a hash: the slowest
step), and inserts the token in a later one, without looking again at the
factor or at whether its own credentials still live. A token inserted
after the confirmation's commit is not deleted.

So the intruder the owner turns MFA on against (they hold the password and
a session) keeps a personal token, unscoped, for 90 days: they only need a
token request in flight while the owner confirms, which repeated requests
make likely. ``/auth/login`` has the same gap between ``challenge()`` and
``issue_login``, narrower.

Here the owner's confirmation runs, as a concurrent request, right after
the intruder's password check. The token the intruder got must not work.

The fix (PLAN's choice between a stamp and a lock): the insert holds the
user's credentials lock, which the revocation holds too, and checks
there that the request's own credentials still live. So the intruder's
request is refused (401) and no token is stored, where this test first
asserted a token made (201) and then refused: no token is stronger.
"""

from typing import Any

import httpx2
import pytest

from domainkit.contrib.auth import personal_tokens
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user, login
from domainkit_tests.test_auth_mfa import clock as clock
from domainkit_tests.test_auth_mfa import code_at, secret_of


def test_a_token_minted_while_mfa_is_turned_on_does_not_survive_it(
    identity: Api, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intruder's token, made across the owner's confirmation, is refused."""
    create_user("alice")
    client = identity()
    owner = bearer(login(client, "alice")["access_token"])
    intruder = bearer(login(client, "alice")["access_token"])
    started = client.post("/auth/mfa/totp", json={"password": PASSWORD}, headers=owner)
    assert started.status_code == 200, started.json()
    secret = secret_of(started.json())
    confirmed: list[int] = []
    original = personal_tokens.check_password_again  # type: ignore[attr-defined]

    async def owner_confirms_meanwhile(*args: Any) -> None:  # noqa: ANN401 - as given.
        await original(*args)
        transport = httpx2.ASGITransport(app=client.app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as other:
            answer = await other.post(
                "/auth/mfa/totp/confirm",
                json={"code": code_at(secret, clock[0])},
                headers=owner,
            )
        confirmed.append(answer.status_code)

    monkeypatch.setattr(
        personal_tokens, "check_password_again", owner_confirms_meanwhile
    )
    made = client.post(
        "/auth/tokens", json={"name": "ci", "password": PASSWORD}, headers=intruder
    )
    monkeypatch.undo()

    assert confirmed == [200]
    assert client.get("/auth/tokens", headers=intruder).status_code == 401
    assert made.status_code == 401, "the intruder's token outlived turning MFA on"
    assert "token" not in made.json()
    listed = client.get("/auth/tokens", headers=owner).json()["items"]
    assert [item["name"] for item in listed] == ["login"]

"""Browser sessions in HttpOnly cookies, and the CSRF checks they need."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth.cookies import CookieAuthentication, csrf_token
from domainkit.contrib.auth.settings import AuthSettings, CookieSettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.application import application_problems
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
    stored_tokens,
)

SITE = "https://testserver"
TRUSTED = {"Origin": SITE}
SESSION = "__Host-dk_session"
CSRF = "__Host-dk_csrf"
"""The cookies' names over HTTPS: only this host may set them."""


def browser(identity: Api, **auth: Any) -> TestClient:  # noqa: ANN401 - AUTH keys.
    """A client of the identity API over HTTPS, as a browser would be."""
    client = identity(**auth)
    return TestClient(client.app, base_url=SITE)


def start(client: TestClient, username: str = "alice") -> str:
    """Log in with cookies; return the CSRF token."""
    response = client.post(
        "/auth/session",
        json={"username": username, "password": PASSWORD},
        headers=TRUSTED,
    )
    assert response.status_code == 200, response.json()
    token: str = response.json()["csrf_token"]
    return token


def set_cookies(response: Any) -> dict[str, str]:  # noqa: ANN401 - a response.
    """Each ``Set-Cookie`` header, by cookie name."""
    return {
        header.split("=", 1)[0]: header
        for header in response.headers.get_list("set-cookie")
    }


def test_a_session_lives_in_an_http_only_cookie(identity: Api) -> None:
    """The session is HttpOnly and Secure; scripts read only the CSRF token."""
    alice = create_user("alice")
    client = browser(identity)

    response = client.post(
        "/auth/session",
        json={"username": "alice", "password": PASSWORD},
        headers=TRUSTED,
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    csrf = response.json()["csrf_token"]
    cookies = set_cookies(response)
    session = cookies[SESSION]
    assert "HttpOnly" in session
    assert "Secure" in session
    assert "SameSite=lax" in session
    assert "Path=/" in session
    assert f"Max-Age={14 * 24 * 3600}" in session
    assert f"{CSRF}={csrf};" in cookies[CSRF]
    assert "HttpOnly" not in cookies[CSRF]
    assert "Secure" in cookies[CSRF]
    assert client.cookies[SESSION].startswith("dkt_")
    (token,) = stored_tokens(alice)
    assert (token["kind"], token["name"]) == ("session", "browser")
    assert client.get("/whoami").json() == {"user": "alice", "scheme": "cookie"}
    (logged_in,) = audited("auth.user_logged_in")
    assert logged_in["actor"] == str(alice)


def test_cookies_tossed_by_another_host_are_ignored(identity: Api) -> None:
    """A sibling subdomain can set ``dk_session`` for the site, not ``__Host-``.

    An attacker logged in themselves, then planted their session in the
    victim's browser under the unprefixed name: the victim's requests ran
    as the attacker (login CSRF), and so would their CSRF token.
    """
    create_user("mallory")
    attacker = browser(identity)
    csrf = start(attacker, "mallory")
    tossed = {"dk_session": attacker.cookies[SESSION], "dk_csrf": csrf}

    victim = TestClient(attacker.app, base_url=SITE, cookies=tossed)

    assert victim.get("/whoami").json() == {"user": "", "scheme": None}
    response = victim.post("/notes", headers={**TRUSTED, "X-CSRF-Token": csrf})
    assert response.status_code == 401


def test_names_a_project_sets_are_kept(identity: Api) -> None:
    """``COOKIE.NAME`` and ``CSRF_NAME`` win, prefixed or not."""
    create_user("alice")
    client = browser(identity, COOKIE={"NAME": "sid", "CSRF_NAME": "__Host-csrf"})

    csrf = start(client)

    assert set(client.cookies.keys()) == {"sid", "__Host-csrf"}
    assert client.post("/notes", headers={**TRUSTED, "X-CSRF-Token": csrf}).json() == {
        "user": "alice",
        "scheme": "cookie",
    }


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_warn_of_names_without_the_prefix(
    use_auth: UseAuth, environment: str
) -> None:
    """Where ``__Host-`` could hold, a name without it is a warning."""
    deployed = {
        "ENVIRONMENT": environment,
        "ALLOWED_HOSTS": ["api.example.com"],
        "RATE_LIMIT_BACKEND": "domainkit.http.ratelimit.RedisBackend",
    }
    hashers = ["domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"]
    classes = [
        "domainkit.contrib.auth.authentication.TokenAuthentication",
        "domainkit.contrib.auth.cookies.CookieAuthentication",
    ]

    def warnings(**cookie: str) -> list[str]:
        use_auth(
            settings=deployed,
            PASSWORD_HASHERS=hashers,
            AUTHENTICATION_CLASSES=classes,
            COOKIE=cookie,
        )
        return registry.get_domain_config("auth").deployment_warnings()

    assert warnings(NAME="sid", CSRF_NAME="csrftoken") == [
        (
            "AUTH.COOKIE.NAME 'sid' lacks the __Host- prefix, so another host of "
            "the site (a subdomain) can set it in browsers; name it __Host-sid, or "
            "leave NAME out."
        ),
        (
            "AUTH.COOKIE.CSRF_NAME 'csrftoken' lacks the __Host- prefix, so another "
            "host of the site (a subdomain) can set it in browsers; name it "
            "__Host-csrftoken, or leave CSRF_NAME out."
        ),
    ]
    assert warnings() == []
    assert warnings(NAME="__Host-sid") == []
    assert warnings(NAME="sid", DOMAIN="example.com") == []  # It cannot hold.
    assert warnings(NAME="sid", PATH="/api") == []
    use_auth(settings=deployed, PASSWORD_HASHERS=hashers, COOKIE={"NAME": "sid"})
    assert registry.get_domain_config("auth").deployment_warnings() == []
    use_auth(settings={"ENVIRONMENT": "test"}, COOKIE={"NAME": "sid"})
    assert registry.get_domain_config("auth").deployment_warnings() == []


def test_both_cookies_take_the_cookie_settings(identity: Api) -> None:
    """Domain, path, SameSite, and lifetime apply to the session and the token."""
    create_user("alice")
    client = browser(
        identity,
        COOKIE={
            "DOMAIN": "example.test",
            "PATH": "/api",
            "SAMESITE": "strict",
            "TTL": 3600,
        },
    )

    response = client.post(
        "/auth/session",
        json={"username": "alice", "password": PASSWORD},
        headers=TRUSTED,
    )

    assert set(set_cookies(response)) == {"dk_session", "dk_csrf"}  # No __Host-.
    for cookie in set_cookies(response).values():
        attributes = {part.strip() for part in cookie.split(";")[1:]}
        assert {
            "Domain=example.test",
            "Path=/api",
            "SameSite=strict",
            "Max-Age=3600",
            "Secure",
        } <= attributes


def test_unsafe_methods_need_a_trusted_origin_and_the_token(identity: Api) -> None:
    """Without both, a cookie request is refused with 403 csrf_failed."""
    create_user("alice")
    client = browser(identity)
    csrf = start(client)
    token = {"X-CSRF-Token": csrf}

    wrong_token = "The CSRF token is missing or wrong."
    cases = [
        ("The request names no origin (Origin or Referer).", {**token}),
        (
            "The request comes from an origin that is not trusted.",
            {**token, "Origin": "https://evil.example"},
        ),
        (wrong_token, {**TRUSTED}),
        (wrong_token, {**TRUSTED, "X-CSRF-Token": csrf + "x"}),
    ]
    for message, headers in cases:
        response = client.post("/notes", headers=headers)
        assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")
        assert response.json()["detail"] == message
        assert "WWW-Authenticate" not in response.headers

    allowed = client.post("/notes", headers={**TRUSTED, **token})
    assert allowed.json() == {"user": "alice", "scheme": "cookie"}
    by_referer = client.post("/notes", headers={"Referer": f"{SITE}/page?x=1", **token})
    assert by_referer.status_code == 200
    bad_referer = client.post(
        "/notes", headers={"Referer": "https://evil.example/page", **token}
    )
    assert bad_referer.status_code == 403
    # Safe methods need neither.
    assert client.get("/whoami").json()["scheme"] == "cookie"


def test_csrf_refusals_never_hold_the_address(identity: Api) -> None:
    """Another site can make a browser fail the CSRF checks at will.

    Those 403s are not counted against ``RATE_LIMIT`` as refused
    credentials (401s) are, so they cannot hold the browser's address: its
    session, and other users behind the same address, are served as before.
    """
    create_user("alice")
    create_user("bob")
    guess = bearer("dkt_" + "A" * 43)
    with override_settings(RATE_LIMIT="10/minute"):
        victim = browser(identity)
        csrf = start(victim)
        forged = [
            victim.post("/notes", headers={"Origin": "https://evil.example"})
            for _ in range(11)
        ]
        after = victim.post("/notes", headers={**TRUSTED, "X-CSRF-Token": csrf})
        read = victim.get("/whoami")
        bob = bearer(login(TestClient(victim.app), "bob")["access_token"])
        other = TestClient(victim.app, base_url=SITE).get("/whoami", headers=bob)
        guesses = [victim.get("/whoami", headers=guess) for _ in range(11)]

    assert {(r.status_code, r.json()["code"]) for r in forged} == {(403, "csrf_failed")}
    assert after.json() == read.json() == {"user": "alice", "scheme": "cookie"}
    assert other.json() == {"user": "bob", "scheme": "token"}
    # Tokens that do not hold still count: the tenth refusal is the last.
    assert [r.status_code for r in guesses] == [401] * 10 + [429]


def test_held_addresses_still_serve_their_browser_sessions(identity: Api) -> None:
    """Bad bearer tokens hold the address's tokens back, never its cookies.

    A session cookie cannot be guessed (one that does not hold leaves the
    request anonymous): browsers behind an address shared with a guesser
    (an office, a carrier, a proxy) stay logged in (AUTH-03).
    """
    create_user("alice")
    create_user("bob")
    with override_settings(RATE_LIMIT="3/minute"):
        victim = browser(identity)
        csrf = start(victim)
        bob = bearer(
            login(TestClient(victim.app, base_url=SITE), "bob")["access_token"]
        )
        guesser = TestClient(victim.app, base_url=SITE)  # The same address.
        guess = bearer("dkt_" + "A" * 43)
        guesses = [guesser.get("/whoami", headers=guess).status_code for _ in range(4)]
        read = victim.get("/whoami")
        wrote = victim.post("/notes", headers={**TRUSTED, "X-CSRF-Token": csrf})
        # TestClient opens sockets over ws://, and sends the cookie if told.
        handshake = {
            "Origin": "http://testserver",
            "Cookie": f"{SESSION}={victim.cookies[SESSION]}",
        }
        with victim.websocket_connect("/live", headers=handshake) as socket:
            greeted = socket.receive_json()
        token = victim.get("/whoami", headers=bob)

    assert guesses == [401, 401, 401, 429]
    assert read.json() == {"user": "alice", "scheme": "cookie"}
    assert wrote.status_code == 200
    assert greeted == {"user": "alice"}
    # A request with a token is still held back, even with a session cookie.
    assert (token.status_code, token.json()["code"]) == (429, "rate_limited")


def test_csrf_tokens_belong_to_one_session(identity: Api) -> None:
    """Another session's token, even in both places, is refused."""
    create_user("alice")
    create_user("bob")
    alice = browser(identity)
    start(alice)
    bob = TestClient(alice.app, base_url=SITE)
    bobs = start(bob, "bob")

    alice.cookies.set(CSRF, bobs)
    response = alice.post("/notes", headers={**TRUSTED, "X-CSRF-Token": bobs})

    assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")


def test_tokens_must_be_signed_by_this_service(identity: Api) -> None:
    """A token in the right form, signed with another key, is refused."""
    create_user("alice")
    client = browser(identity)
    start(client)
    session = stored_tokens_of_cookie(client)
    with override_settings(
        SECRET_KEY="another-0123456789-abcdefghijklmnopqrstuvwxyz-XYZ"
    ):
        forged = csrf_token(session)
    client.cookies.set(CSRF, forged)

    response = client.post("/notes", headers={**TRUSTED, "X-CSRF-Token": forged})

    assert response.status_code == 403
    assert csrf_token(session) != csrf_token(session)  # A new nonce each time.


def stored_tokens_of_cookie(client: TestClient) -> str:
    """The digest of the client's session cookie."""
    from domainkit.contrib.auth.tokens import token_digest

    return token_digest(client.cookies[SESSION])


@pytest.mark.parametrize(
    ("origin", "trusted"),
    [
        ("https://testserver", True),
        ("https://testserver:443", True),
        ("http://testserver", False),
        ("https://app.example.com", True),
        ("https://deep.shop.example.org", True),
        ("https://shop.example.org", False),
        ("http://app.example.com", False),
        ("https://app.example.com:8443", False),
        ("https://evil.com", False),
        ("https://testserver.evil.com", False),
        ("null", False),
        ("file://", False),
        ("https://user@testserver", False),
    ],
)
def test_origins_are_trusted_by_host_and_scheme(
    identity: Api, origin: str, trusted: bool
) -> None:
    """``ALLOWED_HOSTS`` with the request's scheme, or ``CSRF_TRUSTED_ORIGINS``."""
    create_user("alice")
    client = browser(
        identity,
        CSRF_TRUSTED_ORIGINS=[
            "http://plain.example.com",
            "https://app.example.com",
            "https://*.shop.example.org",
        ],
    )
    csrf = start(client)

    response = client.post("/notes", headers={"Origin": origin, "X-CSRF-Token": csrf})

    assert (response.status_code == 200) is trusted


def test_requests_without_a_host_trust_no_origin_of_their_own(identity: Api) -> None:
    """Without ``Host`` (HTTP/1.0), only listed origins are trusted."""
    from starlette.requests import HTTPConnection

    from domainkit.contrib.auth.cookies import trusted_origin

    identity(CSRF_TRUSTED_ORIGINS=["https://app.example.com"])
    connection = HTTPConnection(
        {
            "type": "http",
            "scheme": "https",
            "server": ("10.0.0.1", 443),
            "path": "/",
            "query_string": b"",
            "headers": [],
        }
    )

    assert trusted_origin("https://app.example.com", connection)
    for origin in ("https://", "https://xxxx", "https://10.0.0.1"):
        assert not trusted_origin(origin, connection), origin


def test_csrf_tokens_hold_144_random_bits(identity: Api) -> None:
    """24 URL-safe characters, then this session's signature."""
    identity()

    value, _, signature = csrf_token("d" * 64).partition(":")

    assert len(value) == 24
    assert signature


def test_wildcard_hosts_alone_never_trust_an_origin(identity: Api) -> None:
    """``ALLOWED_HOSTS = ["*"]`` trusts no other origin than the request's own."""
    create_user("alice")
    with override_settings(ALLOWED_HOSTS=["*"]):
        client = browser(identity)
        csrf = start(client)
        response = client.post(
            "/notes", headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf}
        )

    assert response.status_code == 403


def test_plain_http_in_development_needs_only_the_token(identity: Api) -> None:
    """Over HTTP with ``DEBUG``, a request may name no origin; the token stays."""
    create_user("alice")
    with override_settings(DEBUG=True):
        client = identity()
        response = client.post(
            "/auth/session", json={"username": "alice", "password": PASSWORD}
        )
        csrf = response.json()["csrf_token"]
        # Browsers refuse __Host- cookies that are not Secure.
        assert set(set_cookies(response)) == {"dk_session", "dk_csrf"}
        assert "Secure" not in set_cookies(response)["dk_session"]

        assert client.post("/notes", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.post("/notes").status_code == 403


@pytest.mark.parametrize(("debug", "secure"), [(True, True), (False, False)])
def test_secure_overrides_debug(identity: Api, debug: bool, secure: bool) -> None:
    """``COOKIE.SECURE`` set either way wins over what ``DEBUG`` would pick."""
    create_user("alice")
    with override_settings(DEBUG=debug):
        client = identity(COOKIE={"SECURE": secure})
        response = client.post(
            "/auth/session", json={"username": "alice", "password": PASSWORD}
        )

    for cookie in set_cookies(response).values():
        assert ("Secure" in cookie) is secure


def test_header_tokens_ignore_cookies(identity: Api) -> None:
    """With ``Authorization``, the cookie plays no part: no CSRF token needed."""
    create_user("alice")
    create_user("bob")
    client = browser(identity)
    start(client, "bob")
    token = login(TestClient(client.app), "alice")["access_token"]

    response = client.post("/notes", headers=bearer(token))
    other_scheme = client.get("/whoami", headers={"Authorization": "Basic YTpi"})

    assert response.json() == {"user": "alice", "scheme": "token"}
    # A header no class reads still keeps the cookie out: the caller is anonymous.
    assert other_scheme.json() == {"user": "", "scheme": None}


def test_session_tokens_never_work_as_bearer_tokens(identity: Api) -> None:
    """A session token out of its cookie, in a header, is refused."""
    create_user("alice")
    client = browser(identity)
    start(client)
    raw = client.cookies[SESSION]

    response = TestClient(client.app).get("/whoami", headers=bearer(raw))

    assert response.status_code == 401


def test_logging_out_ends_the_session_and_clears_the_cookies(identity: Api) -> None:
    """``DELETE /session`` revokes the token; a stale cookie is anonymous."""
    alice = create_user("alice")
    client = browser(identity)
    csrf = start(client)
    session = client.cookies[SESSION]

    refused = client.delete("/auth/session", headers=TRUSTED)
    response = client.delete("/auth/session", headers={**TRUSTED, "X-CSRF-Token": csrf})

    assert refused.status_code == 403
    assert response.status_code == 204
    cleared = set_cookies(response)
    assert set(cleared) == {SESSION, CSRF}
    assert all("Max-Age=0" in header for header in cleared.values())
    assert stored_tokens(alice) == []
    (logged_out,) = audited("auth.user_logged_out")
    assert logged_out["details"] == {"user_id": str(alice), "everywhere": False}

    stale = TestClient(client.app, base_url=SITE, cookies={SESSION: session})
    assert stale.get("/whoami").json() == {"user": "", "scheme": None}
    assert stale.post("/notes").status_code == 401  # Anonymous: no CSRF check.
    assert stale.delete("/auth/session").status_code == 204
    assert start(stale)  # A stale cookie never stops a new login.


def test_sessions_of_inactive_or_expired_users_are_anonymous(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They are not refused with 401: the browser could not clear the cookie."""
    from domainkit.contrib.auth import tokens

    create_user("alice")
    create_user("off")
    client = browser(identity, COOKIE={"TTL": timedelta(minutes=5)})
    start(client)
    assert client.get("/whoami").json()["user"] == "alice"
    later = tokens.utc_now() + timedelta(minutes=6)
    monkeypatch.setattr(tokens, "utc_now", lambda: later)
    assert client.get("/whoami").json()["user"] == ""
    monkeypatch.undo()

    off = TestClient(client.app, base_url=SITE)
    start(off, "off")
    deactivate("off")
    assert off.get("/whoami").json()["user"] == ""
    for cookie in ("dkt_short", "not-a-token"):
        odd = TestClient(client.app, base_url=SITE, cookies={SESSION: cookie})
        assert odd.get("/whoami").json()["user"] == ""


def deactivate(username: str) -> None:
    """Make ``username`` inactive."""
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.management.base import run_async

    async def change() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            user.set_active(False)
            await uow.commit()

    run_async(change())


def test_the_csrf_token_can_be_asked_for_again(identity: Api) -> None:
    """``GET /session`` hands a new token for the cookie session."""
    create_user("alice")
    client = browser(identity)
    start(client)

    response = client.get("/auth/session")

    fresh = response.json()["csrf_token"]
    assert client.cookies[CSRF] == fresh
    assert (
        client.post("/notes", headers={**TRUSTED, "X-CSRF-Token": fresh}).status_code
        == 200
    )
    no_session = TestClient(client.app).get("/auth/session")
    assert (no_session.status_code, no_session.json()["code"]) == (400, "no_session")
    token = login(TestClient(client.app), "alice")["access_token"]
    by_header = TestClient(client.app).get("/auth/session", headers=bearer(token))
    assert by_header.status_code == 400


def test_logins_are_checked_like_any_other(identity: Api) -> None:
    """Wrong passwords, throttling, and origins, before any password is checked."""
    create_user("alice")
    client = browser(identity, LOGIN_RATE="2/minute")
    wrong = {"username": "alice", "password": "wrong"}

    forged = client.post(
        "/auth/session", json=wrong, headers={"Origin": "https://evil.example"}
    )
    first = client.post("/auth/session", json=wrong, headers=TRUSTED)
    second = client.post("/auth/session", json=wrong, headers=TRUSTED)
    third = client.post("/auth/session", json=wrong, headers=TRUSTED)

    assert (forged.status_code, forged.json()["code"]) == (403, "csrf_failed")
    assert first.status_code == second.status_code == 401
    assert first.json()["code"] == "invalid_credentials"
    assert third.status_code == 429
    assert [row["details"] for row in audited("auth.login")] == [
        {"reason": "invalid_credentials"},
        {"reason": "invalid_credentials"},
        {"reason": "throttled"},
    ]


def test_websockets_with_the_cookie_need_a_trusted_origin(identity: Api) -> None:
    """Cross-site WebSocket hijacking: the handshake's origin is checked."""
    create_user("alice")
    client = browser(identity)
    start(client)

    cookie = {"Cookie": f"{SESSION}={client.cookies[SESSION]}"}

    # TestClient opens sockets over ws://, which pages over http:// may use.
    same_site = {"Origin": "http://testserver"}

    with client.websocket_connect("/live", headers={**same_site, **cookie}) as ws:
        assert ws.receive_json() == {"user": "alice"}
    for headers in ({"Origin": "https://evil.example"}, TRUSTED, {}):
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect("/live", headers={**headers, **cookie}),
        ):
            pass  # pragma: no cover - the handshake is refused.
        assert closed.value.code == 1008
    with TestClient(client.app).websocket_connect("/live") as anonymous:
        assert anonymous.receive_json() == {"user": ""}


def test_cookie_settings_follow_browser_rules() -> None:
    """``SameSite=None`` and ``__Host-`` cookies need what browsers ask of them."""
    with pytest.raises(ValidationError, match="needs SECURE"):
        CookieSettings(SAMESITE="none", SECURE=False)
    for options in (
        {"SECURE": False},
        {"DOMAIN": "example.com"},
        {"PATH": "/api"},
    ):
        with pytest.raises(ValidationError, match="__Host- cookies need"):
            CookieSettings(NAME="__Host-dk", **options)
        with pytest.raises(ValidationError, match="__Host- cookies need"):
            CookieSettings(CSRF_NAME="__Host-csrf", **options)
    assert CookieSettings(NAME="__Host-dk", CSRF_NAME="__Host-csrf", SECURE=True)
    assert CookieSettings(SAMESITE="none")
    for name in ("bad name", "semi;colon", ""):
        with pytest.raises(ValidationError, match="NAME"):
            CookieSettings(NAME=name)


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.com/",
        "https://app.example.com/path",
        "app.example.com",
        "ftp://app.example.com",
        "https://*app.example.com",
        "https://APP.example.com",
    ],
)
def test_trusted_origins_are_a_scheme_and_a_host(origin: str) -> None:
    """Anything else would never match an ``Origin`` header."""
    with pytest.raises(ValidationError, match="scheme and a host"):
        AuthSettings(CSRF_TRUSTED_ORIGINS=[origin])


def test_deployments_refuse_plain_cookies(use_auth: UseAuth) -> None:
    """``COOKIE.SECURE = False`` is refused in staging and production."""
    deployed = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
        "DATABASES": {"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
    }

    hashers = ["domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"]

    use_auth(settings=deployed, PASSWORD_HASHERS=hashers, COOKIE={"SECURE": True})
    with pytest.raises(ImproperlyConfigured, match=r"COOKIE\.SECURE = False"):
        use_auth(settings=deployed, PASSWORD_HASHERS=hashers, COOKIE={"SECURE": False})
    use_auth(settings={"ENVIRONMENT": "test"}, COOKIE={"SECURE": False})


def test_session_routes_need_the_cookie_class(identity: Api) -> None:
    """``check`` reports the router mounted without ``CookieAuthentication``."""
    identity(
        AUTHENTICATION_CLASSES=[
            "domainkit.contrib.auth.authentication.TokenAuthentication"
        ]
    )

    assert application_problems(get_asgi_application()) == [
        (
            "Routes of domainkit.contrib.auth.cookies start browser sessions, but "
            "AUTH.AUTHENTICATION_CLASSES lacks "
            "domainkit.contrib.auth.cookies.CookieAuthentication."
        )
    ]


def test_cookie_authentication_issues_no_login_tokens() -> None:
    """Sessions start at ``/session``; ``/login`` needs another class first."""
    import asyncio

    with pytest.raises(ImproperlyConfigured, match="issues no tokens"):
        asyncio.run(CookieAuthentication().issue(None, None))  # type: ignore[arg-type]

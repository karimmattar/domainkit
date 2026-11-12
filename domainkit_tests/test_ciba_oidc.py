"""The OpenID Provider's CIBA routes: clients, their requests, polls, tokens."""

import base64
import hashlib
import io
import json
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.conf import override_settings
from domainkit.contrib.auth.tables import tokens
from domainkit.contrib.ciba.clients import ASSERTION_TYPE, register
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit_tests.conftest import IDENTITY, Migrated
from domainkit_tests.support.auth import grant
from domainkit_tests.test_auth_jwt_keys import pem
from domainkit_tests.test_ciba import (
    CIBA,
    LAPTOP,
    PHONE,
    SITE,
    Phone,
    redeem,
    rows,
)

ISSUER = "https://id.example.com"
GRANT = "urn:openid:params:grant-type:ciba"
SCOPE = "openid shop.change_product"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class Provider:
    """The project as an OpenID Provider: a client of it, and one phone."""

    def __init__(self, clients: list[Any], **ciba: Any) -> None:  # noqa: ANN401
        """Sign ID tokens with a new P-256 key, as ``ISSUER``."""
        secret, self.public = pem(ec.generate_private_key(ec.SECP256R1()))
        auth = {
            **IDENTITY.settings["AUTH"],
            "JWT": {
                "ALGORITHM": "ES256",
                "SIGNING_KEY": secret,
                "VERIFYING_KEY": self.public,
                "ISSUER": ISSUER,
            },
        }
        self.clients = clients
        clients.append(override_settings(AUTH=auth, CIBA=ciba))
        clients[-1].__enter__()
        from domainkit.http import get_asgi_application

        app = get_asgi_application()
        self.web = TestClient(app, base_url=SITE, client=LAPTOP)
        self.web.__enter__()
        clients.append(self.web)

    def phone(self, username: str = "alice") -> Phone:
        """``username``'s phone, bound and with a push token."""
        app = self.web.app
        return Phone(TestClient(app, base_url=SITE, client=PHONE), username)

    def post(self, path: str, data: dict[str, str], **headers: str) -> Any:  # noqa: ANN401
        """A form post to ``path``: the response."""
        return self.web.post(path, data=data, headers={**FORM, **headers})


type MakeProvider = Callable[..., Provider]


@pytest.fixture
def provider(migrated: Migrated) -> Iterator[MakeProvider]:
    """``provider(**CIBA)``: the project, with an asymmetric ``AUTH.JWT``."""
    opened: list[Any] = []
    with migrated(CIBA):

        def make(**ciba: Any) -> Provider:  # noqa: ANN401 - CIBA keys.
            return Provider(opened, **ciba)

        try:
            yield make
        finally:
            for each in reversed(opened):
                each.__exit__(None, None, None)


def basic(client_id: str, secret: str) -> dict[str, str]:
    """``Authorization: Basic`` for the client."""
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def new_client(
    method: str = "client_secret_basic",
    scopes: tuple[str, ...] = ("shop.change_product",),
    jwks: str | None = None,
) -> tuple[str, str | None]:
    """Register a client: its id, and the secret it was given, if any."""
    secret = None if jwks is not None else secrets.token_urlsafe(32)
    client = run_async(
        register("Acme Bank", scopes=scopes, method=method, jwks=jwks, secret=secret)
    )
    return client.client_id, secret


def authorize(provider: Provider, credentials: dict[str, str], **form: str) -> Any:  # noqa: ANN401
    """``POST /oauth/bc-authorize``: the response."""
    body = {"scope": SCOPE, "login_hint": "alice", "binding_message": "TX-7731"}
    body.update(form)
    body = {name: value for name, value in body.items() if value != "-"}
    return provider.post("/oauth/bc-authorize", body, **credentials)


def poll(provider: Provider, credentials: dict[str, str], handle: str) -> Any:  # noqa: ANN401
    """``POST /oauth/token`` with the CIBA grant: the response."""
    return provider.post(
        "/oauth/token", {"grant_type": GRANT, "auth_req_id": handle}, **credentials
    )


def stored_scopes() -> list[list[str]]:
    """The scopes of every token the clients were given, sorted."""

    async def read() -> list[list[str]]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                select(tokens.c.scopes).where(tokens.c.name.like("ciba:%"))
            )
            return [sorted(row.scopes or ()) for row in found]

    return run_async(read())


def test_discovery_and_keys(provider: MakeProvider) -> None:
    """The metadata names the CIBA endpoints; the keys verify ID tokens."""
    op = provider()
    meta = op.web.get("/.well-known/openid-configuration").json()
    assert meta["issuer"] == ISSUER
    assert meta["backchannel_authentication_endpoint"] == (f"{SITE}/oauth/bc-authorize")
    assert meta["token_endpoint"] == f"{SITE}/oauth/token"
    assert meta["jwks_uri"] == f"{SITE}/.well-known/jwks.json"
    assert meta["grant_types_supported"] == [GRANT]
    assert meta["backchannel_token_delivery_modes_supported"] == ["poll"]
    assert meta["backchannel_user_code_parameter_supported"] is False
    assert "private_key_jwt" in meta["token_endpoint_auth_methods_supported"]
    assert meta["id_token_signing_alg_values_supported"] == ["ES256"]
    [key] = op.web.get("/.well-known/jwks.json").json()["keys"]
    assert key["kty"] == "EC"
    assert key["alg"] == "ES256"
    assert "d" not in key


def test_a_client_asks_the_phone_and_gets_scoped_tokens(provider: MakeProvider) -> None:
    """bc-authorize, the phone sees the client and message, approves; tokens once."""
    op = provider()
    phone = op.phone()
    grant("alice", "shop.change_product")
    client_id, secret = new_client()
    assert secret is not None
    credentials = basic(client_id, secret)

    started = authorize(op, credentials)
    assert started.status_code == 200, started.json()
    assert started.headers["Cache-Control"] == "no-store"
    body = started.json()
    assert set(body) == {"auth_req_id", "expires_in", "interval"}
    [waiting] = phone.waiting()
    assert waiting["client"] == "Acme Bank"
    assert waiting["binding_message"] == "TX-7731"
    assert waiting["match_required"] is False
    assert waiting["address"] is None

    pending = poll(op, credentials, body["auth_req_id"])
    assert pending.status_code == 400
    assert pending.json()["error"] == "authorization_pending"
    assert phone.approve(waiting["id"]).status_code == 204
    from datetime import timedelta

    from domainkit import clock

    with clock.freeze_time(clock.now() + timedelta(seconds=6)):
        answer = poll(op, credentials, body["auth_req_id"])
    assert answer.status_code == 200, answer.json()
    tokens_out = answer.json()
    assert tokens_out["token_type"] == "Bearer"
    assert tokens_out["scope"] == SCOPE
    assert tokens_out["expires_in"] == 600
    claims = jwt.decode(
        tokens_out["id_token"],
        op.public,
        algorithms=["ES256"],
        audience=client_id,
        issuer=ISSUER,
        leeway=30,  # Issued at the frozen time, a few seconds ahead.
    )
    assert claims["sub"] == str(phone.user_id)
    assert claims["amr"] == ["hwk"]
    digest = hashlib.sha256(tokens_out["access_token"].encode()).digest()[:16]
    assert claims["at_hash"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert stored_scopes() == [["shop.change_product"]]

    with clock.freeze_time(clock.now() + timedelta(seconds=12)):
        again = poll(op, credentials, body["auth_req_id"])
    assert again.json()["error"] == "invalid_grant"
    assert redeem(op.web, body["auth_req_id"]).json()["code"] == "sign_in_unknown"


def test_tokens_carry_only_the_users_permissions(provider: MakeProvider) -> None:
    """A scope the user lacks is dropped, not given."""
    op = provider()
    phone = op.phone()
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    handle = authorize(op, credentials).json()["auth_req_id"]
    phone.approve(phone.waiting()[0]["id"])
    answer = poll(op, credentials, handle)
    assert answer.json()["scope"] == "openid"
    assert stored_scopes() == [[]]


@pytest.mark.parametrize(
    ("form", "error"),
    [
        ({"scope": "shop.change_product"}, "invalid_scope"),
        ({"scope": "openid shop.delete_product"}, "invalid_scope"),
        ({"binding_message": "-"}, "invalid_binding_message"),
        ({"binding_message": "x" * 65}, "invalid_binding_message"),
        ({"binding_message": "bad\nline"}, "invalid_binding_message"),
        ({"login_hint": "-"}, "invalid_request"),
        ({"id_token_hint": "a.b.c"}, "invalid_request"),
        ({"login_hint": "-", "login_hint_token": "x"}, "invalid_request"),
        ({"requested_expiry": "soon"}, "invalid_request"),
    ],
)
def test_authentication_requests_are_checked(
    provider: MakeProvider, form: dict[str, str], error: str
) -> None:
    """Each malformed request is refused in OAuth's format, before any push."""
    op = provider()
    client_id, secret = new_client()
    answer = authorize(op, basic(client_id, secret or ""), **form)
    assert answer.status_code == 400
    assert answer.json()["error"] == error
    assert answer.headers["Cache-Control"] == "no-store"
    assert rows() == []


def test_the_binding_message_may_be_optional(provider: MakeProvider) -> None:
    """``REQUIRE_BINDING_MESSAGE = False``: a request without one is taken."""
    op = provider(REQUIRE_BINDING_MESSAGE=False)
    client_id, secret = new_client()
    answer = authorize(
        op, basic(client_id, secret or ""), binding_message="-", requested_expiry="60"
    )
    assert answer.status_code == 200


def test_clients_prove_who_they_are(provider: MakeProvider) -> None:
    """Wrong or missing secrets, mixed methods, and other methods are refused."""
    op = provider()
    client_id, secret = new_client()
    assert secret is not None
    wrong = authorize(op, basic(client_id, "nope"))
    assert wrong.status_code == 401
    assert wrong.json()["error"] == "invalid_client"
    assert wrong.headers["WWW-Authenticate"] == 'Basic realm="ciba"'
    missing = authorize(op, {})
    assert missing.status_code == 401
    as_post = authorize(op, {}, client_id=client_id, client_secret=secret)
    assert as_post.status_code == 401  # Registered for Basic.
    both = authorize(op, basic(client_id, secret), client_secret=secret)
    assert both.json()["error"] == "invalid_request"
    garbled = authorize(op, {"Authorization": "Basic %%%"})
    assert garbled.status_code == 401
    no_colon = authorize(
        op, {"Authorization": "Basic " + base64.b64encode(b"x").decode()}
    )
    assert no_colon.status_code == 401
    other = authorize(op, basic(client_id, secret), client_id="ciba_other")
    assert other.json()["error"] == "invalid_request"

    post_id, post_secret = new_client("client_secret_post")
    taken = authorize(op, {}, client_id=post_id, client_secret=post_secret or "")
    assert taken.status_code == 200, taken.json()


def test_the_endpoints_take_forms_only(provider: MakeProvider) -> None:
    """JSON, or a parameter twice, is an invalid request."""
    op = provider()
    as_json = op.web.post("/oauth/bc-authorize", json={"scope": "openid"})
    assert as_json.status_code == 400
    assert as_json.json()["error"] == "invalid_request"
    twice = op.web.post(
        "/oauth/bc-authorize", content="scope=openid&scope=openid", headers=FORM
    )
    assert twice.json()["error"] == "invalid_request"
    long = op.web.post(
        "/oauth/bc-authorize", content="scope=" + "a" * 70000, headers=FORM
    )
    assert long.json()["error"] == "invalid_request"
    garbage = op.web.post("/oauth/token", content=b"\xff\xfe", headers=FORM)
    assert garbage.json()["error"] == "invalid_request"


def test_the_token_endpoint_offers_the_ciba_grant_only(provider: MakeProvider) -> None:
    """Other grants, a missing handle, and another client's handle are refused."""
    op = provider()
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    other_id, other_secret = new_client()
    handle = authorize(op, credentials).json()["auth_req_id"]

    code = op.post(
        "/oauth/token", {"grant_type": "authorization_code", "code": "x"}, **credentials
    )
    assert code.json()["error"] == "unsupported_grant_type"
    empty = op.post("/oauth/token", {"grant_type": GRANT}, **credentials)
    assert empty.json()["error"] == "invalid_request"
    stolen = poll(op, basic(other_id, other_secret or ""), handle)
    assert stolen.json()["error"] == "invalid_grant"
    web_handle = op.web.post("/auth/app-sign-in/qr").json()["request"]
    assert poll(op, credentials, web_handle).json()["error"] == "invalid_grant"


def test_unknown_users_get_a_decoy_not_unknown_user_id(provider: MakeProvider) -> None:
    """No ``unknown_user_id``: the answer is the same, the poll waits."""
    op = provider()
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    answer = authorize(op, credentials, login_hint="nobody@example.com")
    assert answer.status_code == 200
    pending = poll(op, credentials, answer.json()["auth_req_id"])
    assert pending.json()["error"] == "authorization_pending"


def test_an_id_token_hint_names_the_user_again(provider: MakeProvider) -> None:
    """The ID token of one sign-in names the user of the next; a forged one fails."""
    op = provider()
    phone = op.phone()
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    first = authorize(op, credentials).json()["auth_req_id"]
    phone.approve(phone.waiting()[0]["id"])
    id_token = poll(op, credentials, first).json()["id_token"]

    again = authorize(op, credentials, login_hint="-", id_token_hint=id_token)
    assert again.status_code == 200, again.json()
    [waiting] = phone.waiting()
    assert waiting["client"] == "Acme Bank"

    other_id, other_secret = new_client()
    elsewhere = authorize(
        op, basic(other_id, other_secret or ""), login_hint="-", id_token_hint=id_token
    )
    assert elsewhere.json()["error"] == "invalid_request"  # Not its audience.
    head, payload, signature = id_token.split(".")
    forged = authorize(
        op,
        credentials,
        login_hint="-",
        id_token_hint=f"{head}.{payload}.{signature[::-1]}",
    )
    assert forged.json()["error"] == "invalid_request"
    unnamed = jwt.encode(
        {"sub": "x"}, "k" * 32, algorithm="HS256", headers={"kid": "?"}
    )
    assert (
        authorize(op, credentials, login_hint="-", id_token_hint=unnamed).json()[
            "error"
        ]
        == "invalid_request"
    )


def test_a_client_shows_a_qr_code_for_the_phone(provider: MakeProvider) -> None:
    """qr-authorize: no hint; the phone claims, sees the client, approves.

    ``QR_SAME_NETWORK = "require"`` compares the project's pages only.
    """
    op = provider(QR_SAME_NETWORK="require")
    phone = op.phone()
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    hinted = op.post(
        "/oauth/qr-authorize",
        {"scope": "openid", "binding_message": "KIOSK 4", "login_hint": "alice"},
        **credentials,
    )
    assert hinted.json()["error"] == "invalid_request"
    started = op.post(
        "/oauth/qr-authorize",
        {"scope": "openid", "binding_message": "KIOSK 4"},
        **credentials,
    ).json()
    assert set(started) == {
        "auth_req_id",
        "expires_in",
        "interval",
        "qr",
        "qr_expires_in",
    }
    claimed = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    ).json()
    assert claimed["client"] == "Acme Bank"
    assert claimed["binding_message"] == "KIOSK 4"
    assert claimed["same_network"] is None  # A client's server is not the page.
    phone.approve(claimed["id"])
    answer = poll(op, credentials, started["auth_req_id"])
    assert answer.status_code == 200, answer.json()


def private_key_client() -> tuple[str, ec.EllipticCurvePrivateKey]:
    """A client that signs assertions with a P-256 key: its id and key."""
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    client_id, secret = new_client(
        "private_key_jwt", jwks=json.dumps({"keys": [{**jwk, "kid": "k1"}]})
    )
    assert secret is None
    return client_id, key


def assertion(
    client_id: str,
    key: Any,  # noqa: ANN401 - a private key, or a secret.
    *,
    audience: str = ISSUER,
    lifetime: int = 60,
    algorithm: str = "ES256",
    jti: str | None = None,
    kid: str = "k1",
) -> dict[str, str]:
    """The form fields of a ``private_key_jwt`` assertion."""
    now = int(time.time())
    signed = jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": audience,
            "iat": now,
            "exp": now + lifetime,
            "jti": jti or uuid.uuid4().hex,
        },
        key,
        algorithm=algorithm,
        headers={"kid": kid},
    )
    return {"client_assertion_type": ASSERTION_TYPE, "client_assertion": signed}


def test_private_key_jwt_assertions_work_once(provider: MakeProvider) -> None:
    """Signed by the client's key, for the issuer or the endpoint; never twice."""
    op = provider()
    client_id, key = private_key_client()

    first = assertion(client_id, key, jti="once")
    assert authorize(op, {}, **first).status_code == 200
    replayed = authorize(op, {}, **first)
    assert replayed.status_code == 401
    assert replayed.json()["error"] == "invalid_client"
    endpoint = assertion(client_id, key, audience=f"{SITE}/oauth/bc-authorize")
    assert authorize(op, {}, **endpoint).status_code == 200

    other = ec.generate_private_key(ec.SECP256R1())
    refused = [
        assertion(client_id, other),
        assertion(client_id, key, audience="https://elsewhere"),
        assertion(client_id, key, lifetime=3600),
        assertion(client_id, key, lifetime=10**12),  # Past any date: no 500.
        assertion(client_id, key, lifetime=-120),
        assertion(client_id, "s" * 32, algorithm="HS256"),
        assertion(client_id, key, kid="k2"),
        assertion("ciba_unknown", key),
        {**assertion(client_id, key), "client_assertion_type": "other"},
        {**assertion(client_id, key), "client_id": "ciba_other"},
        {"client_assertion_type": ASSERTION_TYPE, "client_assertion": "x" * 9000},
        {"client_assertion_type": ASSERTION_TYPE, "client_assertion": "not.a.jwt"},
        {
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": "eyJhbGciOiJFUzI1NiJ9.bm90IGpzb24.c2ln",
        },
        {
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": "eyJhbGciOiJFUzI1NiJ9.WzFd.c2ln",
        },
    ]
    for fields in refused:
        answer = authorize(op, {}, **fields)
        assert answer.status_code == 401, fields
        assert answer.json()["error"] == "invalid_client"
    secret_id, _ = new_client()
    assert authorize(op, {}, **assertion(secret_id, key)).status_code == 401


def test_clients_are_rate_limited(provider: MakeProvider) -> None:
    """``CLIENT_RATE`` counts starts, not polls: the interval paces those."""
    from datetime import timedelta

    from domainkit import clock

    op = provider(CLIENT_RATE="1/minute")
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    handle = authorize(op, credentials).json()["auth_req_id"]
    throttled = authorize(op, credentials)
    assert throttled.status_code == 429
    assert throttled.json()["error"] == "rate_limited"
    for seconds in (0, 6, 12):
        with clock.freeze_time(clock.now() + timedelta(seconds=seconds)):
            polled = poll(op, credentials, handle)
        assert polled.json()["error"] == "authorization_pending"


def test_a_shared_key_cannot_sign_id_tokens(provider: MakeProvider) -> None:
    """HS256, or no issuer: the routes refuse to run, as the check says."""
    op = provider()
    with override_settings(
        AUTH={**IDENTITY.settings["AUTH"], "JWT": {"SIGNING_KEY": "k" * 32}}
    ):
        assert op.web.get("/.well-known/jwks.json").status_code == 500


def test_the_ciba_command_manages_clients(
    provider: MakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add, list, rotate, remove; secrets from stdin, never printed; refusals."""
    from domainkit.contrib.ciba.management.commands.ciba import Command

    op = provider()
    given = ["first-secret-" + "x" * 30 + "\n"]
    monkeypatch.setattr(Command, "read_secret", staticmethod(lambda: given[0]))
    out = io.StringIO()
    call_command("ciba", stdout=out)
    assert out.getvalue() == "No CIBA client is registered.\n"
    call_command("ciba", "add", "Acme", "--scope", "shop.change_product", stdout=out)
    lines = out.getvalue().splitlines()
    client_id = lines[1].removeprefix("client_id: ")
    assert len(lines) == 2  # The secret is never printed.
    first = given[0].strip()
    assert authorize(op, basic(client_id, first)).status_code == 200
    key = ec.generate_private_key(ec.SECP256R1())
    jwks = tmp_path / "jwks.json"
    jwks.write_text(
        json.dumps(
            {"keys": [json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))]}
        )
    )
    out = io.StringIO()
    call_command(
        "ciba",
        "add",
        "Kiosk",
        "--method",
        "private_key_jwt",
        "--jwks",
        str(jwks),
        stdout=out,
    )
    assert out.getvalue().startswith("client_id: ")
    out = io.StringIO()
    call_command("ciba", "list", stdout=out)
    assert "'Acme'  client_secret_basic  scopes: shop.change_product" in out.getvalue()
    assert "'Kiosk'  private_key_jwt  scopes: -" in out.getvalue()
    out = io.StringIO()
    given[0] = "second-secret-" + "y" * 30
    call_command("ciba", "rotate", client_id, stdout=out)
    assert out.getvalue() == (
        f"The secret of {client_id} changed; the old one stopped working.\n"
    )
    assert authorize(op, basic(client_id, first)).status_code == 401
    assert authorize(op, basic(client_id, given[0])).status_code == 200
    given[0] = "short"
    with pytest.raises(CommandError, match="at least 32"):
        call_command("ciba", "rotate", client_id, stdout=out)
    with pytest.raises(CommandError, match="at least 32"):
        call_command("ciba", "add", "Short", stdout=out)
    given[0] = "z" * 40
    call_command("ciba", "remove", client_id, stdout=out)
    for args in (
        ("rotate", client_id),
        ("remove", client_id),
        ("add", "X", "--method", "private_key_jwt"),
        ("add", "X", "--jwks", str(jwks)),
        ("add", "X", "--method", "private_key_jwt", "--jwks", str(tmp_path / "no")),
    ):
        with pytest.raises(CommandError):
            call_command("ciba", *args, stdout=out)
    private = tmp_path / "private.json"
    private.write_text(
        json.dumps({"keys": [json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key))]})
    )
    for bad in (private, tmp_path / "jwks.json"):
        if bad.name == "jwks.json":
            bad.write_text("{not json")
        with pytest.raises(CommandError):
            call_command(
                "ciba",
                "add",
                "X",
                "--method",
                "private_key_jwt",
                "--jwks",
                str(bad),
                stdout=out,
            )


def test_a_user_turned_off_after_approving_gets_no_tokens(
    provider: MakeProvider,
) -> None:
    """Approved, then deactivated: the poll is refused, the handle spent."""
    from domainkit_tests.test_ciba import _deactivate

    op = provider()
    phone = op.phone()
    client_id, secret = new_client()
    credentials = basic(client_id, secret or "")
    handle = authorize(op, credentials).json()["auth_req_id"]
    phone.approve(phone.waiting()[0]["id"])
    run_async(_deactivate("alice"))
    answer = poll(op, credentials, handle)
    assert answer.json()["error"] == "invalid_grant"


def test_an_id_token_hint_must_name_a_user_by_id(provider: MakeProvider) -> None:
    """Signed by the provider for the client, but its sub is no user id."""
    from domainkit.contrib.ciba.oidc import id_token_signer

    op = provider()
    client_id, secret = new_client()
    signer = id_token_signer()
    hint = jwt.encode(
        {"iss": ISSUER, "sub": "not-an-id", "aud": client_id},
        signer.signing_key,
        algorithm="ES256",
        headers={"kid": signer.key_id},
    )
    answer = authorize(
        op, basic(client_id, secret or ""), login_hint="-", id_token_hint=hint
    )
    assert answer.json()["error"] == "invalid_request"


def test_metadata_leaves_out_the_routes_not_served(provider: MakeProvider) -> None:
    """Served without the CIBA routes, the metadata names no endpoint of theirs."""
    from fastapi import FastAPI
    from starlette.requests import Request

    from domainkit.contrib.ciba.oidc import metadata, well_known

    provider()
    alone = FastAPI()
    alone.include_router(well_known)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/.well-known/openid-configuration",
        "headers": [(b"host", b"id.example.com")],
        "scheme": "https",
        "server": ("id.example.com", 443),
        "query_string": b"",
        "router": alone.router,
        "app": alone,
    }
    answer = run_async(metadata(Request(scope)))
    assert "token_endpoint" not in answer
    assert answer["jwks_uri"] == "https://id.example.com/.well-known/jwks.json"


def test_a_client_needs_a_known_method() -> None:
    """``register`` refuses a method it does not know, before the database."""
    from domainkit.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured, match="method is one of"):
        run_async(register("X", method="tls_client_auth"))

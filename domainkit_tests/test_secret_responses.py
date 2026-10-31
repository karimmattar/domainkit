"""Every answer that holds a secret or a token is marked ``no-store``.

Caches must not keep them, and ``IdempotencyMiddleware`` must not either:
it would replay the secret to whoever retries with the key. The routes are
found by their response models, so a new route that returns a token, a
secret, codes, or a URI with a secret in it is checked as soon as it is
mounted here.
"""

import io
import re
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import BaseModel

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import mfa
from domainkit.contrib.auth.admin import router as auth_admin
from domainkit.contrib.webhooks.admin import router as webhooks_admin
from domainkit.http import get_asgi_application
from domainkit.management import call_command
from domainkit_tests import sample_identity_routes
from domainkit_tests.conftest import (
    FAST_HASHER,
    IDENTITY_DOMAINS,
    MEMORY_MAIL,
    MIDDLEWARE,
)
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user
from domainkit_tests.test_auth_mfa import NOW, code_at, secret_of

SITE: Final = "https://testserver"
ORIGIN: Final = {"Origin": SITE}
AUTHENTICATION: Final = "domainkit.contrib.auth"
SECRET_FIELD: Final = re.compile(
    r"(?:^|_)(?:tokens?|secrets?|codes|uri|password|authorization)$"
)
"""Names of response fields that hold what only the caller may see."""

router = APIRouter()
"""Every router of the framework that answers with secrets, as mounted."""
router.include_router(sample_identity_routes.router)
router.include_router(auth_admin, prefix="/admin/auth")
router.include_router(webhooks_admin, prefix="/admin/webhooks")


def models_in(annotation: object) -> Iterator[type[BaseModel]]:
    """The pydantic models an annotation names, in unions and containers too."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation):
        for argument in typing.get_args(annotation):
            yield from models_in(argument)


def secret_fields(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    """Fields of ``model``, or of the models it holds, named like secrets."""
    seen = set() if seen is None else seen
    if model in seen:
        return set()
    seen.add(model)
    found = set()
    for name, field in model.model_fields.items():
        if SECRET_FIELD.search(name):
            found.add(f"{model.__name__}.{name}")
        for inner in models_in(field.annotation):
            found |= secret_fields(inner, seen)
    return found


def secret_routes(app: FastAPI) -> dict[str, set[str]]:
    """``METHOD path`` of every route that may answer with a secret, and why."""
    found: dict[str, set[str]] = {}
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        answers = [context.response_model]
        answers += [entry.get("model") for entry in context.responses.values()]
        fields = {
            field
            for answer in answers
            for model in models_in(answer)
            for field in secret_fields(model)
        }
        for method in context.methods or () if fields else ():
            found[f"{method} {context.path}"] = fields
    return found


def test_secret_fields_are_found_in_unions_lists_and_nested_models() -> None:
    """The scan sees through ``A | B``, ``list[...]``, and models in models."""

    class Inner(BaseModel):
        refresh_token: str
        recovery_codes_left: int

    class Outer(BaseModel):
        items: list[Inner]
        again: "Outer | None" = None

    class Other(BaseModel):
        otpauth_uri: str

    assert list(models_in(Outer | Other | None)) == [Outer, Other]
    assert secret_fields(Outer) == {"Inner.refresh_token"}
    assert secret_fields(Other) == {"Other.otpauth_uri"}


clock = [NOW]
"""The time second factors are checked at."""


@pytest.fixture
def client(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A project with every app that hands out secrets, its routers mounted."""
    clock[0] = NOW
    monkeypatch.setattr(mfa, "_now", lambda: clock[0])
    registry.clear()
    mail.outbox.clear()
    with override_settings(
        INSTALLED_DOMAINS=[*IDENTITY_DOMAINS, "domainkit.contrib.webhooks"],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER=f"{__name__}.router",
        MIDDLEWARE=MIDDLEWARE,
        MAIL=MEMORY_MAIL,
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "AUTHENTICATION_CLASSES": [
                f"{AUTHENTICATION}.authentication.JWTAuthentication",
                f"{AUTHENTICATION}.authentication.TokenAuthentication",
                f"{AUTHENTICATION}.cookies.CookieAuthentication",
            ],
            "JWT": {"SIGNING_KEY": "jwt-key-0123456789abcdefghijklmnopqrstuv"},
        },
    ):
        setup()
        call_command("makemigrations", "-m", "secrets", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with TestClient(get_asgi_application(), base_url=SITE) as test_client:
            yield test_client
    registry.clear()
    mail.outbox.clear()


def answers(client: TestClient) -> dict[str, list[Response]]:
    """Call each route that answers with a secret, as its users would."""
    create_user("root", is_superuser=True, is_staff=True)
    seen: dict[str, list[Response]] = {}

    def call(method: str, path: str, route: str = "", **options: Any) -> Response:  # noqa: ANN401 - request options.
        response = client.request(method, path, **options)
        assert response.status_code < 300, (path, response.json())
        seen.setdefault(f"{method} {route or path}", []).append(response)
        return response

    credentials = {"username": "root", "password": PASSWORD}
    tokens = call("POST", "/auth/login", json=credentials).json()
    root = bearer(tokens["access_token"])
    call("POST", "/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    call("POST", "/auth/tokens", json={"name": "ci"}, headers=root)
    call("POST", "/auth/session", json=credentials, headers=ORIGIN)
    call("GET", "/auth/session")
    client.cookies.clear()

    started = call("POST", "/auth/mfa/totp", json={"password": PASSWORD}, headers=root)
    secret = secret_of(started.json())
    code = {"code": code_at(secret, clock[0])}
    call("POST", "/auth/mfa/totp/confirm", json=code, headers=root)
    clock[0] += 30
    reauthenticate = {"password": PASSWORD, "code": code_at(secret, clock[0])}
    call("POST", "/auth/mfa/recovery-codes", json=reauthenticate, headers=root)
    clock[0] += 30
    challenge = call("POST", "/auth/login", json=credentials).json()
    finish = {"mfa_token": challenge["mfa_token"], "code": code_at(secret, clock[0])}
    call("POST", "/auth/mfa/login", json=finish)
    clock[0] += 30
    challenge = call("POST", "/auth/session", json=credentials, headers=ORIGIN).json()
    finish = {"mfa_token": challenge["mfa_token"], "code": code_at(secret, clock[0])}
    call("POST", "/auth/session/mfa", json=finish, headers=ORIGIN)
    client.cookies.clear()
    clock[0] += 30
    call(
        "POST", "/auth/step-up", json={"code": code_at(secret, clock[0])}, headers=root
    )
    transfer = {"amount": {"amount": "1.00", "currency": "EUR"}, "payee": "DE89"}
    asked = client.post("/transfers/authorize", json=transfer, headers=root).json()
    clock[0] += 30
    call(
        "POST",
        f"/auth/sca/{asked['challenge']}/confirm",
        "/auth/sca/{challenge_id}/confirm",
        json={"code": code_at(secret, clock[0])},
        headers=root,
    )

    hook = {"url": "https://hooks.example.com/in", "event_types": ["*"]}
    made = call("POST", "/admin/webhooks/subscriptions", json=hook, headers=root)
    call(
        "POST",
        f"/admin/webhooks/subscriptions/{made.json()['id']}/rotate-secret",
        "/admin/webhooks/subscriptions/{subscription_id}/rotate-secret",
        headers=root,
    )
    return seen


def test_answers_with_secrets_are_never_stored(client: TestClient) -> None:
    """Each route whose answer can hold a secret marks it ``no-store``."""
    routes = secret_routes(cast(FastAPI, client.app))

    seen = answers(client)

    assert set(routes) == set(seen), "Call new routes with secrets in answers()."
    assert routes["POST /auth/tokens"] == {"NewToken.token"}
    assert routes["POST /auth/login"] == {
        "TokenOut.access_token",
        "TokenOut.refresh_token",
        "MfaChallengeOut.mfa_token",
    }
    assert [r.status_code for r in seen["POST /auth/login"]] == [200, 202]
    stored = {
        (route, response.status_code): response.headers.get("Cache-Control")
        for route, responses in seen.items()
        for response in responses
        if response.headers.get("Cache-Control") != "no-store"
    }
    assert stored == {}

"""Logs show routes' templates, never the paths callers sent (WEB-10)."""

import dataclasses
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Final

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.routing import Host

from domainkit.conf import override_settings
from domainkit.context import (
    UNMATCHED_PATH,
    RequestContext,
    RoutedRequestContext,
    current_request,
    route_template,
)
from domainkit.ddd import NotFound
from domainkit.http import get_asgi_application
from domainkit.http import middleware as middleware_module
from domainkit.http.access import Public

TOKEN: Final = "c2VjcmV0LXRva2Vu"
"""What a path may carry that logs must not: a token, an id, an email."""
CONFIRM: Final = "/accounts/{account_id}/confirm/{token}"

router = APIRouter(dependencies=[Public])
seen: list[RequestContext | None] = []


@router.get(CONFIRM)
async def confirm(account_id: int, token: str) -> dict[str, str]:
    """Answer with the context's path, as the handler sees it."""
    context = current_request()
    seen.append(context)
    return {"path": context.path if context else ""}


@router.get("/accounts/{account_id}/fail")
async def fail(account_id: int) -> None:
    """Fail as a bug does."""
    raise RuntimeError("a bug")


@router.get("/accounts/{account_id}/gone")
async def gone(account_id: int) -> None:
    """Answer 404 as a domain error."""
    raise NotFound("No such account.")


nested = APIRouter()
nested.include_router(router, prefix="/v1")

ROUTES: Final = "domainkit_tests.test_access_paths.router"
STACK: Final = ("domainkit.http.middleware.RequestContextMiddleware",)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of these routes, raising nothing, with the access log on."""
    seen.clear()
    with (
        override_settings(ROOT_ROUTER=ROUTES, MIDDLEWARE=STACK),
        TestClient(get_asgi_application(), raise_server_exceptions=False) as client,
    ):
        yield client


def access_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """Each access line's message and its ``access`` field's path."""
    return [
        (record.getMessage().rsplit(" ", 1)[0], record.__dict__["access"]["path"])
        for record in caplog.records
        if record.name == "domainkit.access"
    ]


def test_logs_and_the_context_show_the_template(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Access lines, error lines, reports, and the context: no token, no id."""
    reports: list[dict[str, object]] = []
    monkeypatch.setattr(
        middleware_module,
        "report_error",
        lambda exc, **context: reports.append(context),
    )
    caplog.set_level(logging.INFO)

    confirmed = client.get(f"/accounts/7/confirm/{TOKEN}?email=a@example.com")
    failed = client.get("/accounts/7/fail")
    missing = client.get(f"/missing/{TOKEN}")

    assert confirmed.json() == {"path": CONFIRM}
    assert (failed.status_code, missing.status_code) == (500, 404)
    assert access_lines(caplog) == [
        (f"GET {CONFIRM} 200", CONFIRM),
        ("GET /accounts/{account_id}/fail 500", "/accounts/{account_id}/fail"),
        (f"GET {UNMATCHED_PATH} 404", UNMATCHED_PATH),
    ]
    assert "Unhandled error in GET /accounts/{account_id}/fail" in caplog.messages
    assert [report["path"] for report in reports] == ["/accounts/{account_id}/fail"]
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("domainkit")  # httpx logs the client's URLs.
    )
    assert TOKEN not in ours
    assert "/accounts/7" not in ours


def test_problem_details_name_the_path_the_caller_sent(client: TestClient) -> None:
    """``instance`` answers the caller, so it is the path as sent."""
    with override_settings(ERROR_FORMAT="problem+json"):
        answer = client.get("/accounts/7/gone")

    assert answer.json()["instance"] == "/accounts/7/gone"


def test_the_raw_path_is_a_setting_away(caplog: pytest.LogCaptureFixture) -> None:
    """``ACCESS_LOG_PATH = "raw"`` logs, and binds, the path as sent."""
    caplog.set_level(logging.INFO)
    with (
        override_settings(ROOT_ROUTER=ROUTES, MIDDLEWARE=STACK, ACCESS_LOG_PATH="raw"),
        TestClient(get_asgi_application()) as client,
    ):
        answer = client.get(f"/accounts/7/confirm/{TOKEN}")

    path = f"/accounts/7/confirm/{TOKEN}"
    assert answer.json() == {"path": path}
    assert access_lines(caplog) == [(f"GET {path} 200", path)]


def test_routes_of_nested_routers_show_their_full_template(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not flattened (ADR 0060), the template still carries every prefix."""
    caplog.set_level(logging.INFO)
    with (
        override_settings(
            ROOT_ROUTER="domainkit_tests.test_access_paths.nested",
            MIDDLEWARE=STACK,
            FLATTEN_ROUTERS=False,
        ),
        TestClient(get_asgi_application()) as client,
    ):
        answer = client.get(f"/v1/accounts/7/confirm/{TOKEN}")

    assert answer.json() == {"path": f"/v1{CONFIRM}"}
    assert access_lines(caplog) == [(f"GET /v1{CONFIRM} 200", f"/v1{CONFIRM}")]


def test_a_routed_context_follows_its_scope() -> None:
    """Unmatched until the router adds a route; copies keep following it."""
    scope: dict[str, object] = {"path": f"/accounts/7/confirm/{TOKEN}"}
    context = RoutedRequestContext("req-1", None, "GET", UNMATCHED_PATH, scope=scope)
    acting = dataclasses.replace(context, actor="user:1")

    assert (context.path, acting.path) == (UNMATCHED_PATH, UNMATCHED_PATH)
    scope["route"] = SimpleNamespace(path=CONFIRM)
    assert (context.path, acting.path, acting.actor) == (CONFIRM, CONFIRM, "user:1")
    assert context.request_path == f"/accounts/7/confirm/{TOKEN}"
    assert TOKEN not in repr(context)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({}, UNMATCHED_PATH),
        ({"route": Host("api.example.com", app=router)}, UNMATCHED_PATH),
        ({"route": SimpleNamespace(path="/a")}, "/a"),
        ({"fastapi": {}, "route": SimpleNamespace(path="/a")}, "/a"),
        ({"fastapi": None, "route": SimpleNamespace(path="/a")}, "/a"),
        (
            {
                "fastapi": {"effective_route_context": SimpleNamespace(path="/v1/a")},
                "route": SimpleNamespace(path="/a"),
            },
            "/v1/a",
        ),
    ],
)
def test_route_templates(scope: dict[str, object], expected: str) -> None:
    """The flattened route's path, the nested one's full path, or unmatched."""
    assert route_template(scope) == expected
    context = RoutedRequestContext("r", None, "GET", UNMATCHED_PATH, scope=scope)
    assert context.path == expected

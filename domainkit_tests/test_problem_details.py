"""``ERROR_FORMAT = "problem+json"``: errors as RFC 9457 problem details."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.errors import (
    PROBLEM_MEDIA_TYPE,
    error_response,
    problem_details,
    status_phrase,
)

type MakeClient = Callable[..., TestClient]
PROBLEMS = {"ERROR_FORMAT": "problem+json"}


@pytest.fixture
def make_client() -> Iterator[MakeClient]:
    """Clients of the sample routes, with setting overrides."""
    with ExitStack() as stack:

        def make(**overrides: object) -> TestClient:
            stack.enter_context(override_settings(**overrides))
            return stack.enter_context(
                TestClient(get_asgi_application(), raise_server_exceptions=False)
            )

        yield make


def test_domain_errors_are_problems(make_client: MakeClient) -> None:
    """Type, title, status, detail, and instance; code and request id extend it."""
    client = make_client(**PROBLEMS)

    response = client.get("/errors/not_found", headers={"X-Request-ID": "req-7"})

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": response.json()["detail"],
        "instance": "/errors/not_found",
        "code": "not_found",
        "request_id": "req-7",
    }


def test_each_code_can_be_a_type_of_its_own(make_client: MakeClient) -> None:
    """With a base URL, the type names the code, and the title reads it out."""
    client = make_client(
        **PROBLEMS, ERROR_TYPE_BASE_URL="https://errors.example.com/problems/"
    )

    body = client.get("/errors/custom").json()

    assert body["type"] == "https://errors.example.com/problems/fully_booked"
    assert body["title"] == "Fully booked"
    assert (body["status"], body["code"]) == (409, "fully_booked")


def test_validation_errors_keep_their_fields(make_client: MakeClient) -> None:
    """``errors`` is an extension member; submitted values stay out."""
    client = make_client(**PROBLEMS)

    response = client.post("/typed", json={"count": "lots", "extra": "secret"})

    body = response.json()
    assert (response.status_code, body["code"]) == (422, "validation_error")
    assert body["title"] == "Unprocessable Content"
    assert {tuple(error["loc"]) for error in body["errors"]} == {
        ("body", "count"),
        ("body", "extra"),
    }
    assert "secret" not in response.text
    assert "lots" not in response.text


def test_framework_refusals_are_problems_too(make_client: MakeClient) -> None:
    """Middleware answers (hosts, bodies, 500s) use the same format."""
    client = make_client(**PROBLEMS, MAX_REQUEST_BODY_SIZE=4)

    host = client.get("/ok", headers={"Host": "evil.example"})
    large = client.post("/echo", content=b"0123456789")
    crash = client.get("/boom")

    for response, status, code in (
        (host, 400, "invalid_host"),
        (large, 413, "content_too_large"),
        (crash, 500, "internal_error"),
    ):
        assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
        assert (response.json()["status"], response.json()["code"]) == (status, code)
    assert crash.json()["title"] == "Internal Server Error"


def test_the_default_format_is_unchanged(make_client: MakeClient) -> None:
    """Without the setting, errors are the usual JSON body."""
    response = make_client().get("/errors/not_found")

    assert response.headers["content-type"] == "application/json"
    assert set(response.json()) == {"detail", "code", "request_id"}


def test_instances_are_escaped_paths_and_optional() -> None:
    """The path is percent-encoded; outside a request there is no instance."""
    context = RequestContext("req-1", None, "GET", "/files/a b/ü", None)

    with override_settings(**PROBLEMS), bind_request(context):
        inside = problem_details(404, "gone", code="not_found", request=context)
        response = error_response(404, "gone", code="not_found")

    assert inside.instance == "/files/a%20b/%C3%BC"
    assert response.media_type == PROBLEM_MEDIA_TYPE
    assert problem_details(499, "odd", code="odd").instance is None
    assert problem_details(499, "odd", code="odd").title == "Error"
    assert status_phrase(418) == "I'm a Teapot"


@pytest.mark.parametrize("url", ["errors", "https://ex ample.com/", ":nope"])
def test_type_base_urls_are_absolute(url: str) -> None:
    """A relative URL, or one with spaces, is refused at startup."""
    with (
        pytest.raises(ImproperlyConfigured, match="ERROR_TYPE_BASE_URL"),
        override_settings(ERROR_TYPE_BASE_URL=url),
    ):
        pass

"""``ERROR_FORMAT = "problem+json"``: errors as RFC 9457 problem details."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from domainkit.conf import UNSET, override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.approvals import ApprovalPending
from domainkit.ddd import Conflict
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.errors import (
    PROBLEM_MEDIA_TYPE,
    APIError,
    FieldError,
    error_response,
    problem_details,
    status_phrase,
)

type MakeClient = Callable[..., TestClient]
router = APIRouter()


@router.post("/answers")
async def refuse() -> None:
    """Refuse with field errors of the route's own."""
    raise APIError(
        409,
        "Taken.",
        code="taken",
        errors=[FieldError(loc=["body", "slug"], msg="Pick another.", type="taken")],
    )


@router.get("/answers")
async def list_answers() -> list[str]:
    """Read the answers: a second route on the same path."""
    return []


@router.get("/unchanged")
async def unchanged() -> None:
    """Answer 304 as a conditional GET does, keeping its validator."""
    raise HTTPException(304, headers={"ETag": '"v1"'})


@router.post("/checked")
async def checked() -> None:
    """Refuse with a validation error that names neither a place nor a type."""
    raise RequestValidationError([{"msg": "Pick one."}])


class RoomBooked(Conflict):
    """A room taken: it names the booking, and tries the body's own names."""

    code = "room_booked"

    def extensions(self) -> Mapping[str, object]:
        """The booking, and names that a body's members already have."""
        return {
            "booking_id": 7,
            "request_id": "mine",
            "code": "taken",
            "title": "Mine",
            "errors": [],
        }


@router.post("/bookings")
async def book() -> None:
    """Refuse: the room is booked."""
    raise RoomBooked("The room is booked.")


SUBMITTED: Final = UUID("0192f0aa-5b7e-7c3d-9a41-3f6b1e2d4c5a")
"""The approval request ``/payouts`` says it submitted."""


@router.post("/payouts")
async def pay_out() -> None:
    """Submit the payout for approval instead of making it."""
    raise ApprovalPending(
        "This command needs approval: it was submitted for approval.", SUBMITTED
    )


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

    response = client.get("/errors/not_found")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": response.json()["detail"],
        "instance": "/errors/not_found",
        "code": "not_found",
        "request_id": response.headers["X-Request-ID"],
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


def test_problem_details_are_the_default(make_client: MakeClient) -> None:
    """Without the setting, errors are problem details since 1.0 (DSGN-26)."""
    response = make_client(ERROR_FORMAT=UNSET).get("/errors/not_found")

    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert {"type", "title", "status", "detail", "code", "request_id"} <= set(
        response.json()
    )


def test_the_domainkit_format_keeps_the_old_body(make_client: MakeClient) -> None:
    """``ERROR_FORMAT = "domainkit"`` keeps the body of 0.x."""
    response = make_client(ERROR_FORMAT="domainkit").get("/errors/not_found")

    assert response.headers["content-type"] == "application/json"
    assert set(response.json()) == {"detail", "code", "request_id"}


def test_instances_are_escaped_paths_and_optional() -> None:
    """The path is percent-encoded; outside a request there is no instance."""
    context = RequestContext("req-1", None, "GET", "/files/a b/ü", None)

    with override_settings(**PROBLEMS), bind_request(context):
        inside = problem_details(404, "gone", code="not_found", request=context)
        response = error_response(404, "gone", code="not_found")

    assert inside.instance == "/files/a%20b/%C3%BC"
    kept = RequestContext("req-1", None, "GET", "/users/a@b:c;d=e,f", None)
    assert problem_details(404, "gone", code="x", request=kept).instance == (
        "/users/a@b:c;d=e,f"
    )
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


def test_openapi_documents_problem_details(make_client: MakeClient) -> None:
    """Error responses are documented as problem details, not the usual body."""
    schema = make_client(**PROBLEMS).get("/openapi.json").json()

    responses = schema["paths"]["/typed"]["post"]["responses"]
    assert responses["422"]["content"] == {
        PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}
    }
    assert "application/json" in responses["200"]["content"]
    components = schema["components"]["schemas"]
    assert {"ProblemDetails", "FieldError"} <= set(components)
    assert "ErrorResponse" not in components
    assert components["ProblemDetails"]["required"] == [
        "title",
        "status",
        "detail",
        "code",
    ]


def test_openapi_keeps_the_error_body_another_route_still_uses(
    make_client: MakeClient,
) -> None:
    """A schema still naming ``ErrorResponse`` elsewhere keeps the component."""
    from domainkit.http.openapi import _document_problems

    schema: dict[str, Any] = {
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "404": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Other"}
                                }
                            }
                        }
                    }
                },
                "parameters": [],
            }
        },
        "components": {
            "schemas": {
                "ErrorResponse": {"type": "object"},
                "Other": {"$ref": "#/components/schemas/ErrorResponse"},
            }
        },
    }

    _document_problems(schema)

    assert "ErrorResponse" in schema["components"]["schemas"]


@pytest.mark.parametrize("problems", [False, True])
def test_errors_raised_by_routes_keep_their_fields(
    make_client: MakeClient, problems: bool
) -> None:
    """An ``APIError`` with ``errors`` answers with them, in either format."""
    settings = PROBLEMS if problems else {}
    client = make_client(
        ROOT_ROUTER="domainkit_tests.test_problem_details.router", **settings
    )

    response = client.post("/answers")

    assert response.status_code == 409
    assert response.json()["errors"] == [
        {"loc": ["body", "slug"], "msg": "Pick another.", "type": "taken"}
    ]


ROUTES = "domainkit_tests.test_problem_details.router"


@pytest.mark.parametrize("problems", [False, True])
def test_wrong_methods_list_every_route_of_the_path(
    make_client: MakeClient, problems: bool
) -> None:
    """``Allow`` names the methods of each route on the path, not the first's."""
    settings = PROBLEMS if problems else {}
    client = make_client(ROOT_ROUTER=ROUTES, **settings)

    refused = client.delete("/answers")
    served = client.get("/answers")

    assert (refused.status_code, refused.json()["code"]) == (405, "method_not_allowed")
    assert refused.headers["Allow"] == "GET, POST"
    assert "Allow" not in served.headers


@pytest.mark.parametrize("problems", [False, True])
def test_an_errors_own_members_extend_the_body_and_replace_none_of_it(
    make_client: MakeClient, problems: bool
) -> None:
    """``extensions()`` adds ``booking_id``; a body's own names stay the body's.

    Names of either format are left out, so both formats carry the same
    extension members (user report P3).
    """
    settings = PROBLEMS if problems else {}
    client = make_client(ROOT_ROUTER=ROUTES, **settings)

    response = client.post("/bookings")

    body = response.json()
    assert (response.status_code, body["code"]) == (409, "room_booked")
    assert body["booking_id"] == 7
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert "errors" not in body
    assert body.get("title", "Conflict") == "Conflict"


@pytest.mark.parametrize("problems", [False, True])
def test_a_command_submitted_for_approval_names_the_approval_request(
    make_client: MakeClient, problems: bool
) -> None:
    """``approval_request_id`` is the approval request's (user report P3).

    ``request_id``, in every error, is the HTTP request's: the approval
    request's id was not in the body at all.
    """
    settings = PROBLEMS if problems else {}
    client = make_client(ROOT_ROUTER=ROUTES, **settings)

    response = client.post("/payouts")

    body = response.json()
    assert (response.status_code, body["code"]) == (403, "approval_pending")
    assert body["approval_request_id"] == str(SUBMITTED)
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["request_id"] != body["approval_request_id"]


def test_bodiless_statuses_keep_their_headers(make_client: MakeClient) -> None:
    """A 304 raised by a route has no body, but keeps the headers it was given."""
    response = make_client(ROOT_ROUTER=ROUTES).get("/unchanged")

    assert (response.status_code, response.content) == (304, b"")
    assert response.headers["ETag"] == '"v1"'


def test_validation_errors_may_lack_a_place_and_a_type(
    make_client: MakeClient,
) -> None:
    """An error raised by hand without ``loc`` or ``type`` gets empty ones."""
    response = make_client(ROOT_ROUTER=ROUTES).post("/checked")

    assert (response.status_code, response.json()["code"]) == (422, "validation_error")
    assert response.json()["errors"] == [{"loc": [], "msg": "Pick one.", "type": ""}]

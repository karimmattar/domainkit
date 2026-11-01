"""WEB-19: a browser on another origin cannot read ``ETag`` or ``Location``.

``CORSMiddleware`` exposes ``X-Request-ID`` alone (``expose_headers`` is
fixed, and no setting changes it). Browsers hide every other non-safelisted
response header from a cross-origin script: a single-page app the docs
configure with ``CORS_ALLOWED_ORIGINS`` cannot read the ``ETag`` it must
send back as ``If-Match`` (``require_if_match`` resources answer it 428
otherwise), the ``Location`` of what it created, ``Retry-After`` on a 429
or 503, the ``RateLimit-*`` quota, ``Idempotent-Replayed``, or a
deprecation's ``Sunset``. ``If-Match`` and ``Idempotency-Key`` are not in
the default ``CORS_ALLOWED_HEADERS`` either, so its preflight for a
conditional or keyed write is refused.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from domainkit.conf import override_settings
from domainkit.http.middleware import CORSMiddleware

ORIGIN = "https://app.example.com"


def created(request: object) -> Response:
    """What a resource's create answers: its tag and where it lives."""
    return Response(
        status_code=201,
        headers={"ETag": 'W/"1-abc"', "Location": "/gadgets/1", "Retry-After": "1"},
    )


@pytest.fixture
def client() -> TestClient:
    """An application behind CORS, allowing one origin, as the docs configure it."""
    with override_settings(CORS_ALLOWED_ORIGINS=[ORIGIN]):
        app = CORSMiddleware(Starlette(routes=[Route("/", created, methods=["POST"])]))
    return TestClient(app)


def test_scripts_on_an_allowed_origin_may_read_what_resources_answer(
    client: TestClient,
) -> None:
    """``ETag``, ``Location``, and ``Retry-After`` are exposed."""
    answer = client.post("/", headers={"Origin": ORIGIN})
    exposed = {
        name.strip().lower()
        for name in answer.headers.get("access-control-expose-headers", "").split(",")
    }

    assert answer.headers["access-control-allow-origin"] == ORIGIN
    assert {"etag", "location", "retry-after"} <= exposed


def test_scripts_on_an_allowed_origin_may_send_if_match_and_keys(
    client: TestClient,
) -> None:
    """A preflight for a conditional, keyed write is allowed by default."""
    answer = client.options(
        "/",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "if-match, idempotency-key",
        },
    )

    assert answer.status_code == 200

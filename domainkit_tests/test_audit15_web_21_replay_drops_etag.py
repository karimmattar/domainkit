"""WEB-21: a replayed response loses its ``ETag`` (and every header but three).

``IdempotencyMiddleware`` stores a kept response's ``Content-Type``,
``Location``, and ``Content-Language`` only (``_STORED_HEADERS``). A
``ResourceRouter`` create answers ``201`` with ``Location`` and the
``ETag`` of the version it made; retried with the same ``Idempotency-Key``
(the case the key exists for: the first answer was lost), the replay has
no ``ETag``. A client of a resource that requires ``If-Match`` then has
nothing to send with its next change, and a generic client sees a replay
that differs from the answer it stands for (no ``Deprecation``,
``Sunset``, ``Link``, ``Vary``, or ``Cache-Control`` of the route either).
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from domainkit.contrib.idempotency.middleware import IdempotencyMiddleware
from domainkit.http.middleware import RequestContextMiddleware
from domainkit_tests.test_ratelimit_and_idempotency import CALLER, idempotent_app


async def create(request: Request) -> Response:
    """What a resource's create answers."""
    return Response(
        b'{"id": "1"}',
        status_code=201,
        media_type="application/json",
        headers={"ETag": 'W/"1-5d41402abc4b2a76"', "Location": "/gadgets/1"},
    )


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """The create, behind the idempotency middleware, keys in a database."""
    with idempotent_app({"default": database_url}):
        app = RequestContextMiddleware(
            IdempotencyMiddleware(
                Starlette(routes=[Route("/gadgets", create, methods=["POST"])])
            )
        )
        with TestClient(
            app, headers={CALLER[0].decode(): CALLER[1].decode()}
        ) as served:
            yield served


def test_a_replay_carries_the_etag_of_the_answer_it_replays(
    client: TestClient,
) -> None:
    """The retry gets the first answer's ``ETag`` with its ``Location``."""
    headers = {"Idempotency-Key": "create-1"}

    first = client.post("/gadgets", json={}, headers=headers)
    retry = client.post("/gadgets", json={}, headers=headers)

    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.headers["location"] == first.headers["location"]
    assert retry.headers.get("etag") == first.headers["etag"]

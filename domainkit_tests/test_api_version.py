"""``api_version``: the version of the router serving a request, from its URL."""

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from domainkit.http.versioning import api_version, versioned_router


def test_a_route_knows_the_version_serving_it() -> None:
    """The same route under /v1 and /v2 tells them apart; outside, None."""
    shared = APIRouter()

    @shared.get("/which")
    async def which(request: Request) -> dict[str, str | None]:
        return {"version": api_version(request)}

    app = FastAPI()
    for version in ("v1", "v2"):
        router = versioned_router(version)
        router.include_router(shared)
        app.include_router(router)
    app.include_router(shared, prefix="/plain")

    with TestClient(app) as client:
        answers = [
            client.get(f"/{where}/which").json() for where in ("v1", "v2", "plain")
        ]

    assert answers == [{"version": "v1"}, {"version": "v2"}, {"version": None}]

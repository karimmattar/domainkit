"""The probes and the metrics scrape, named once in ``PROBE_PATHS`` (ADR 0186)."""

import logging
from collections.abc import Iterator

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE
from domainkit.ddd.tenancy import current_tenant
from domainkit.http import get_asgi_application
from domainkit.http.access import Public
from domainkit.http.health import router as health_router
from domainkit.http.metrics import router as metrics_router

router = APIRouter()
router.include_router(health_router)
router.include_router(metrics_router)

ROUTES = "domainkit_tests.test_probe_paths.router"
TENANT_MIDDLEWARE = "domainkit.tenancy.TenantMiddleware"


@router.get("/livez", dependencies=[Public])
async def livez() -> dict[str, str | None]:
    """A probe of the project's own, at a path of its choosing."""
    return {"tenant": current_tenant()}


@router.get("/orders", dependencies=[Public])
async def orders() -> dict[str, str | None]:
    """A route of the project's: it serves a tenant."""
    return {"tenant": current_tenant()}


@pytest.fixture
def probed() -> Iterator[None]:
    """The probe routes, behind the documented middleware and a tenant's header."""
    with override_settings(
        ROOT_ROUTER=ROUTES,
        TENANCY={"HEADER": "X-Tenant"},
        MIDDLEWARE=[*DEFAULT_MIDDLEWARE, TENANT_MIDDLEWARE],
    ):
        yield


@pytest.mark.usefixtures("probed", "database_access")
def test_the_probes_need_no_tenant() -> None:
    """The three answer without one; a route of the project's needs one.

    Before 2.9 only ``/health`` was exempt by default (``TENANCY.EXEMPT_PATHS``),
    so ``/ready`` and the scrape answered ``400 tenant_not_named``.
    """
    with TestClient(get_asgi_application()) as client:
        probes = {path: client.get(path) for path in ("/health", "/ready", "/metrics")}
        unnamed = client.get("/orders")
        named = client.get("/orders", headers={"X-Tenant": "acme"})

    assert {path: answer.status_code for path, answer in probes.items()} == {
        "/health": 200,
        "/ready": 200,
        "/metrics": 200,
    }
    assert (unnamed.status_code, unnamed.json()["code"]) == (400, "tenant_not_named")
    assert named.json() == {"tenant": "acme"}


@pytest.mark.usefixtures("probed")
def test_the_probes_move_together(caplog: pytest.LogCaptureFixture) -> None:
    """One setting moves them out of the rate limit, the tenant, and the access log.

    ``/livez`` takes ``/health``'s place: it is never counted, needs no
    tenant, and is logged at DEBUG; ``/health``, out of ``EXEMPT_PATHS`` too,
    is a route like any other.
    """
    caplog.set_level(logging.DEBUG, logger="domainkit.access")
    with (
        override_settings(
            PROBE_PATHS=("/livez",),
            TENANCY={"HEADER": "X-Tenant", "EXEMPT_PATHS": []},
            RATE_LIMIT="1/minute",
            MIDDLEWARE=[
                "domainkit.http.middleware.RequestContextMiddleware",
                "domainkit.http.ratelimit.RateLimitMiddleware",
                TENANT_MIDDLEWARE,
            ],
        ),
        TestClient(get_asgi_application()) as client,
    ):
        probes = [client.get("/livez").status_code for _ in range(3)]
        health = client.get("/health")
        counted = client.get("/health", headers={"X-Tenant": "acme"})

    assert probes == [200, 200, 200]
    assert (health.status_code, health.json()["code"]) == (400, "tenant_not_named")
    assert (counted.status_code, counted.json()["code"]) == (429, "rate_limited")
    levels = {
        record.levelno
        for record in caplog.records
        if record.name == "domainkit.access" and "/livez" in record.getMessage()
    }
    assert levels == {logging.DEBUG}


@pytest.mark.usefixtures("probed")
def test_each_list_adds_paths_of_the_projects_own() -> None:
    """A list a project sets adds to the probes: it never drops them."""
    with (
        override_settings(
            RATE_LIMIT="1/minute",
            RATE_LIMIT_EXEMPT_PATHS=("/orders",),
            CONCURRENCY_EXEMPT_PATHS=("/orders",),
            ACCESS_LOG_QUIET_PATHS=("/orders",),
            MIDDLEWARE=[
                "domainkit.http.middleware.RequestContextMiddleware",
                "domainkit.http.shedding.ConcurrencyLimitMiddleware",
                "domainkit.http.ratelimit.RateLimitMiddleware",
            ],
        ),
        TestClient(get_asgi_application()) as client,
    ):
        answers = [client.get(path).status_code for path in ["/health", "/orders"] * 3]

    assert answers == [200] * 6

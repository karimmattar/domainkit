"""The application starts, answers its probes, and knows who calls it."""

import pytest
from domainkit.testing import AsyncTestClient, TestClient


def test_health(client: TestClient) -> None:
    """GET /health answers 200."""
    assert client.get("/health").status_code == 200


def test_callers_must_log_in(client: TestClient) -> None:
    """Without a token, the caller is anonymous."""
    response = client.get("/auth/me")
    assert (response.status_code, response.json()["code"]) == (401, "not_authenticated")


@pytest.mark.anyio
async def test_health_in_an_async_test(async_client: AsyncTestClient) -> None:
    """Async tests await the async clients (and ``async_client_for``)."""
    assert (await async_client.get("/health")).status_code == 200

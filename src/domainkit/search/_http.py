"""What the search engines' adapters share: a JSON call, its failures named."""

from collections.abc import Mapping
from typing import Any

from . import SearchError


async def call(
    client: Any,  # noqa: ANN401 - domainkit.http.client's HttpClient.
    engine: str,
    method: str,
    path: str,
    *,
    json: Any = None,  # noqa: ANN401 - any JSON.
    content: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    accept: tuple[int, ...] = (200, 201, 202),
) -> Any:  # noqa: ANN401 - the answer's JSON.
    """Send one call; its JSON answer (None if empty).

    Raises:
        SearchError: The engine answered another status, or could not be reached.
    """
    import httpx2

    try:
        response = await client.request(
            method, path, json=json, content=content, headers=dict(headers or {})
        )
    except httpx2.HTTPError as exc:
        raise SearchError(
            f"{engine} could not be reached ({type(exc).__name__})."
        ) from exc
    if response.status_code not in accept:
        raise SearchError(
            f"{engine} refused {method} {path.split('?')[0]} ({response.status_code})."
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise SearchError(f"{engine} answered without JSON.") from None

"""What the HTTP key services' adapters share: a JSON call, its failures named."""

from collections.abc import Collection, Mapping
from typing import Any

from . import KeyProviderError, UnknownKey


async def call_json(
    client: Any,  # noqa: ANN401 - domainkit.http.client's HttpClient.
    provider: str,
    action: str,
    url: str,
    *,
    payload: Mapping[str, Any] | None = None,
    content: bytes | None = None,
    headers: Mapping[str, str],
    unwrap: bool = False,
    refusals: Collection[int] = (400,),
) -> dict[str, Any]:
    """POST to ``url``; the answer's JSON object, if the status is 200.

    Raises:
        UnknownKey: ``unwrap`` was asked and the service refused the
            ciphertext (a status in ``refusals``).
        KeyProviderError: It refused otherwise, answered no JSON object,
            or could not be reached. The message names the provider and
            the action, never what was sent or answered.
    """
    import httpx2

    try:
        response = await client.post(
            url,
            json=None if payload is None else dict(payload),
            content=content,
            headers=dict(headers),
            retry=True,
        )
    except httpx2.HTTPError as exc:
        raise KeyProviderError(f"{provider} could not be reached ({action}).") from exc
    status = response.status_code
    if unwrap and status in refusals:
        raise UnknownKey(f"{provider} cannot unwrap that key ({status}).")
    if status != 200:
        raise KeyProviderError(f"{provider} refused {action} ({status}).")
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise KeyProviderError(f"{provider} answered {action} without JSON.")
    return answer


def field(answer: Mapping[str, Any], name: str, provider: str) -> str:
    """A text field of an answer.

    Raises:
        KeyProviderError: It is missing.
    """
    value = answer.get(name)
    if not isinstance(value, str) or not value:
        raise KeyProviderError(f"{provider} answered without {name}.")
    return value

"""SEC-15a-3: the container credentials endpoint may be any host, over plain HTTP.

``default_credentials`` calls ``AWS_CONTAINER_CREDENTIALS_FULL_URI`` as
given, and appends ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` to
``http://169.254.170.2`` without checking that the result still names
that host. Either sends the pod's authorization token, in clear, to any
host, and takes the AWS credentials that host answers with. The AWS SDKs
allow a full URI over plain HTTP only to a loopback address or the ECS
and EKS agents' link-local addresses.
"""

import contextlib

import httpx2
import pytest

from domainkit.clock import freeze_time
from domainkit.http.client import stub
from domainkit.keys import KeyProviderError
from domainkit.keys.aws import default_credentials
from domainkit_tests.test_keys_providers import NOW, no_aws  # noqa: F401

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "169.254.170.2", "169.254.170.23"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://credentials.attacker.test/c"),
        ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "@credentials.attacker.test/c"),
    ],
)
async def test_the_container_token_goes_only_to_the_agent(
    no_aws: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    """The pod's token must never be sent to another host over HTTP."""
    asked: list[httpx2.Request] = []

    def anywhere(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(
            200,
            json={
                "AccessKeyId": "ASIAEVIL",
                "SecretAccessKey": "chosen-by-the-attacker",
                "Expiration": "2026-10-31T15:00:00Z",
            },
        )

    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", "pod-identity-token")
    with (
        stub(anywhere),
        freeze_time(NOW),
        contextlib.suppress(KeyProviderError, ValueError),
    ):
        await default_credentials("eu-west-1")()
    sent_to = [request.url.host for request in asked]
    assert all(host in ALLOWED_HOSTS for host in sent_to), (
        f"the authorization token went to {sent_to}"
    )

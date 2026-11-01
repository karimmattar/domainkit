"""AWS KMS as a ``KeyProvider``: data keys under a KMS key, signed with SigV4.

::

    provider = AwsKmsKeyProvider(
        "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-...", region="eu-west-1"
    )

Each call is one HTTPS request to KMS's JSON API (``GenerateDataKey``,
``Decrypt``, ``ReEncrypt``), signed with Signature Version 4 here, through
``domainkit.http.client``: no AWS SDK is needed (ADR 0119). Every call
names the key, and carries the encryption context
``{"domainkit": "data-key"}``, which KMS binds to the wrapped key and
CloudTrail records.

Credentials come from ``credentials``, an async callable, by default
``default_credentials()``: the ``AWS_ACCESS_KEY_ID``,
``AWS_SECRET_ACCESS_KEY`` and ``AWS_SESSION_TOKEN`` variables; else a web
identity (EKS's IAM roles for service accounts: ``AWS_ROLE_ARN`` and
``AWS_WEB_IDENTITY_TOKEN_FILE``); else the container's (ECS, and EKS Pod
Identity: ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` or ``_FULL_URI``).
Temporary credentials are kept until five minutes before they expire.
Pass another callable for anything else (an EC2 instance profile, say).
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit

from domainkit import clock
from domainkit.keys import DataKey, KeyProvider, KeyProviderError, UnknownKey

__all__ = [
    "AwsCredentials",
    "AwsKmsKeyProvider",
    "container_credentials",
    "default_credentials",
    "environment_credentials",
    "sign",
    "web_identity_credentials",
]

NAME: Final = "aws-kms"
ALGORITHM: Final = "AWS4-HMAC-SHA256"
JSON_1_1: Final = "application/x-amz-json-1.1"
CONTEXT: Final = {"domainkit": "data-key"}
"""The encryption context of every data key: KMS binds it to the wrapped key."""
REFRESH_BEFORE: Final = timedelta(minutes=5)
"""How long before temporary credentials expire they are fetched again."""
ECS_HOST: Final = "http://169.254.170.2"
CONTAINER_AGENTS: Final = frozenset({"169.254.170.2", "169.254.170.23", "fd00:ec2::23"})
"""The ECS and EKS Pod Identity agents' addresses, which plain HTTP may reach."""
"""Where ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` points (ECS's agent)."""
UNWRAP_REFUSALS: Final = frozenset(
    {"InvalidCiphertextException", "IncorrectKeyException"}
)
"""KMS errors that mean the wrapped key is not one this key unwraps."""
_ERROR_TYPE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
_STS_FIELD: Final = "<{0}>([^<]{{1,4096}})</{0}>"


@dataclass(frozen=True, slots=True, repr=False)
class AwsCredentials:
    """An access key, its secret, and for temporary ones a session token."""

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    expires_at: datetime | None = None
    """When temporary credentials stop working; None for long-lived ones."""

    def __repr__(self) -> str:
        """The access key id only: it names the credentials, it grants nothing."""
        return f"AwsCredentials(access_key_id={self.access_key_id!r})"

    def fresh(self) -> bool:
        """Whether they still have five minutes or more to run."""
        return self.expires_at is None or clock.now() < self.expires_at - REFRESH_BEFORE


type CredentialSource = Callable[[], Awaitable[AwsCredentials]]


def sign(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    credentials: AwsCredentials,
    *,
    region: str,
    service: str,
    now: datetime,
) -> dict[str, str]:
    """``headers`` with SigV4's: ``X-Amz-Date``, the token, ``Authorization``.

    Every header given is signed, with ``Host`` from ``url``; the query
    string, if any, is signed as AWS canonicalizes it.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    signed = {key.lower(): " ".join(value.split()) for key, value in headers.items()}
    signed["host"] = _host(url)
    signed["x-amz-date"] = stamp
    if credentials.session_token is not None:
        signed["x-amz-security-token"] = credentials.session_token
    names = ";".join(sorted(signed))
    parts = urlsplit(url)
    canonical = "\n".join(
        [
            method.upper(),
            quote(parts.path or "/", safe="/-_.~"),
            _canonical_query(parts.query),
            "".join(f"{name}:{signed[name]}\n" for name in sorted(signed)),
            names,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        [ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = signing_key(credentials.secret_access_key, stamp[:8], region, service)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    result = dict(headers)
    result["X-Amz-Date"] = stamp
    if credentials.session_token is not None:
        result["X-Amz-Security-Token"] = credentials.session_token
    result["Authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={names}, Signature={signature}"
    )
    return result


def signing_key(secret: str, day: str, region: str, service: str) -> bytes:
    """SigV4's key for one day, region, and service."""
    key = f"AWS4{secret}".encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _host(url: str) -> str:
    """The ``Host`` header of ``url``: its host, and its port unless the scheme's."""
    parts = urlsplit(url)
    default = {"https": 443, "http": 80}.get(parts.scheme)
    host = parts.hostname or ""
    return host if parts.port in {None, default} else f"{host}:{parts.port}"


def _canonical_query(query: str) -> str:
    """The query string as SigV4 signs it: each name and value encoded, sorted."""
    pairs = []
    for item in filter(None, query.split("&")):
        name, _, value = item.partition("=")
        pairs.append((quote(name, safe="-_.~"), quote(value, safe="-_.~")))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def environment_credentials() -> CredentialSource:
    """Credentials from the ``AWS_*`` variables (access key, secret, token).

    Read at each call, so a rotated secret is picked up.
    """

    async def read() -> AwsCredentials:
        found = _environment()
        if found is None:
            raise KeyProviderError(
                "AWS credentials: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                "are unset."
            )
        return found

    return read


def _environment() -> AwsCredentials | None:
    """The credentials the environment variables hold, if both parts are set."""
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret:
        return None
    return AwsCredentials(access_key, secret, os.environ.get("AWS_SESSION_TOKEN"))


class _Cached:
    """A source of temporary credentials, fetched again near their expiry."""

    def __init__(self, fetch: CredentialSource) -> None:
        """Keep what ``fetch`` returns until five minutes before it expires."""
        self._fetch = fetch
        self._held: AwsCredentials | None = None

    async def __call__(self) -> AwsCredentials:
        """The credentials held, or new ones."""
        held = self._held
        if held is None or not held.fresh():
            held = self._held = await self._fetch()
        return held


def web_identity_credentials(
    role_arn: str, token_file: Path, *, region: str, session_name: str = "domainkit"
) -> CredentialSource:
    """Credentials for ``role_arn``, assumed with the token in ``token_file``.

    EKS's IAM roles for service accounts mount the token and set
    ``AWS_ROLE_ARN`` and ``AWS_WEB_IDENTITY_TOKEN_FILE``. STS's
    ``AssumeRoleWithWebIdentity`` needs no signature: the token is the proof.
    """

    async def fetch() -> AwsCredentials:
        from domainkit.http.client import HttpClient

        token = (await asyncio.to_thread(token_file.read_text)).strip()
        sts = HttpClient(
            base_url=f"https://sts.{region}.amazonaws.com", propagate=False
        )
        try:
            response = await sts.post(
                "/",
                data={
                    "Action": "AssumeRoleWithWebIdentity",
                    "Version": "2011-06-15",
                    "RoleArn": role_arn,
                    "RoleSessionName": session_name,
                    "WebIdentityToken": token,
                },
                retry=True,
            )
        except _http_errors() as exc:
            raise KeyProviderError("AWS STS could not be reached.") from exc
        finally:
            await sts.aclose()
        if response.status_code != 200:
            raise KeyProviderError(
                f"AWS STS refused AssumeRoleWithWebIdentity ({response.status_code})."
            )
        found = {
            name: re.search(_STS_FIELD.format(name), response.text)
            for name in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")
        }
        if not all(found.values()):
            raise KeyProviderError("AWS STS answered without credentials.")
        values = {name: match[1] for name, match in found.items() if match}
        return AwsCredentials(
            values["AccessKeyId"],
            values["SecretAccessKey"],
            values["SessionToken"],
            _expiry(values["Expiration"]),
        )

    return _Cached(fetch)


def container_credentials(
    url: str, *, authorization: str | None = None
) -> CredentialSource:
    """Credentials from the container's endpoint (ECS, EKS Pod Identity).

    ``authorization`` goes in the ``Authorization`` header, as
    ``AWS_CONTAINER_AUTHORIZATION_TOKEN`` (or the file
    ``AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`` names) gives it.
    """

    async def fetch() -> AwsCredentials:
        from domainkit.http.client import HttpClient

        endpoint = HttpClient(propagate=False, breaker=False)
        headers = {} if authorization is None else {"Authorization": authorization}
        try:
            response = await endpoint.get(url, headers=headers)
        except _http_errors() as exc:
            raise KeyProviderError(
                "The container's credentials endpoint could not be reached."
            ) from exc
        finally:
            await endpoint.aclose()
        try:
            document = response.json()
            return AwsCredentials(
                document["AccessKeyId"],
                document["SecretAccessKey"],
                document.get("Token"),
                _expiry(document["Expiration"]),
            )
        except (ValueError, KeyError, TypeError):
            raise KeyProviderError(
                f"The container's credentials endpoint answered {response.status_code} "
                "without credentials."
            ) from None

    return _Cached(fetch)


def default_credentials(region: str) -> CredentialSource:
    """The first source the environment sets up (see the module).

    Chosen when called, from the environment variables then set.
    """
    if _environment() is not None:
        return environment_credentials()
    role = os.environ.get("AWS_ROLE_ARN")
    token_file = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
    if role and token_file:
        return web_identity_credentials(role, Path(token_file), region=region)
    relative = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    full = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if relative or full:
        url = _container_url(relative, full)
        return container_credentials(url, authorization=_container_token())
    return environment_credentials()  # Says what is missing, at the first call.


def _container_url(relative: str | None, full: str | None) -> str:
    """The container endpoint's URL, if it is one the AWS SDKs would call.

    A relative URI is a path on ECS's agent, ``169.254.170.2``. A full URI
    may use HTTPS to any host, but plain HTTP only to a loopback address
    or an agent's (``169.254.170.2``, ``169.254.170.23``, ``fd00:ec2::23``),
    as the AWS SDKs' container credential provider allows: the pod's
    token goes nowhere else, nor are credentials taken from anywhere else
    (SEC-15a-3).

    Raises:
        KeyProviderError: It is not.
    """
    if relative:
        url = f"{ECS_HOST}{relative}"
        if not relative.startswith("/") or urlsplit(url).hostname != ECS_HOST[7:]:
            raise KeyProviderError(
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI must be a path, such as "
                "/v2/credentials/<id>."
            )
        return url
    url = str(full)
    parts = urlsplit(url)
    if parts.scheme == "https" or (
        parts.scheme == "http" and _local_agent(parts.hostname or "")
    ):
        return url
    raise KeyProviderError(
        "AWS_CONTAINER_CREDENTIALS_FULL_URI must use https, or http to a loopback "
        "address or the container agent's (169.254.170.2, 169.254.170.23)."
    )


def _local_agent(host: str) -> bool:
    """Whether ``host`` is loopback, or a container credentials agent's address."""
    if host in CONTAINER_AGENTS or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _container_token() -> str | None:
    """The container endpoint's authorization token, if the environment gives one."""
    if path := os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
        return Path(path).read_text().strip()
    return os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")


def _expiry(text: str) -> datetime:
    """An ISO 8601 expiry, as STS and the container endpoint write it.

    Raises:
        ValueError: It is not one.
    """
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _http_errors() -> tuple[type[BaseException], ...]:
    """The failures of a call that mean the service could not be reached."""
    import httpx2

    return (httpx2.HTTPError,)


class AwsKmsKeyProvider(KeyProvider):
    """Data keys under one AWS KMS key (see the module)."""

    name = NAME

    def __init__(
        self,
        key_id: str,
        *,
        region: str,
        endpoint: str | None = None,
        credentials: CredentialSource | None = None,
        timeout: float | None = None,
    ) -> None:
        """Use the KMS key ``key_id`` (an ARN, an id, or ``alias/...``) in ``region``.

        ``endpoint`` replaces ``https://kms.<region>.amazonaws.com`` (a VPC
        endpoint, FIPS, or a test double); ``credentials`` replaces
        ``default_credentials(region)``; ``timeout`` bounds each call.
        """
        from domainkit.http.client import HttpClient

        self.key_id = key_id
        self.region = region
        self.endpoint = (endpoint or f"https://kms.{region}.amazonaws.com").rstrip("/")
        self.credentials = credentials or default_credentials(region)
        self.client = HttpClient(
            base_url=self.endpoint, timeout=timeout, propagate=False
        )

    def __repr__(self) -> str:
        """The key and region; no credential."""
        return f"AwsKmsKeyProvider(key_id={self.key_id!r}, region={self.region!r})"

    async def generate_data_key(self) -> DataKey:
        """``GenerateDataKey``: 32 random bytes, and them under the KMS key."""
        answer = await self._call(
            "GenerateDataKey",
            {"KeyId": self.key_id, "KeySpec": "AES_256", "EncryptionContext": CONTEXT},
        )
        return DataKey(
            _bytes(answer, "Plaintext"), f"{NAME}:{_text(answer, 'CiphertextBlob')}"
        )

    async def unwrap(self, wrapped: str) -> bytes:
        """``Decrypt``, naming the key, so KMS refuses a blob of any other."""
        answer = await self._call(
            "Decrypt",
            {
                "CiphertextBlob": self._blob(wrapped),
                "KeyId": self.key_id,
                "EncryptionContext": CONTEXT,
            },
        )
        return _bytes(answer, "Plaintext")

    async def rewrap(self, wrapped: str) -> str:
        """``ReEncrypt`` to the KMS key's current version, inside KMS."""
        answer = await self._call(
            "ReEncrypt",
            {
                "CiphertextBlob": self._blob(wrapped),
                "SourceKeyId": self.key_id,
                "SourceEncryptionContext": CONTEXT,
                "DestinationKeyId": self.key_id,
                "DestinationEncryptionContext": CONTEXT,
            },
        )
        return f"{NAME}:{_text(answer, 'CiphertextBlob')}"

    def _blob(self, wrapped: str) -> str:
        """The KMS ciphertext ``wrapped`` holds, as base64.

        Raises:
            UnknownKey: It is not a key KMS wrapped.
        """
        if not self.owns(wrapped):
            raise UnknownKey("AWS KMS did not wrap that key.")
        blob = wrapped.removeprefix(f"{NAME}:")
        try:
            base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError):
            raise UnknownKey("The wrapped key was changed, or cut short.") from None
        return blob

    async def _call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Send ``operation`` to KMS, signed; its answer.

        Raises:
            UnknownKey: KMS refused the ciphertext.
            KeyProviderError: KMS refused otherwise, or could not be reached.
        """
        body = json.dumps(payload).encode()
        url = f"{self.endpoint}/"
        headers = sign(
            "POST",
            url,
            {"Content-Type": JSON_1_1, "X-Amz-Target": f"TrentService.{operation}"},
            body,
            await self.credentials(),
            region=self.region,
            service="kms",
            now=clock.now(),
        )
        try:
            response = await self.client.post(
                "/", content=body, headers=headers, retry=True
            )
        except _http_errors() as exc:
            raise KeyProviderError(
                f"AWS KMS could not be reached ({operation})."
            ) from exc
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise KeyProviderError(
                f"AWS KMS answered {operation} with {response.status_code} and no JSON."
            )
        if response.status_code == 200:
            return answer
        kind = str(answer.get("__type", "")).rpartition("#")[2]
        kind = kind if _ERROR_TYPE.fullmatch(kind) else "an error"
        if kind in UNWRAP_REFUSALS:
            raise UnknownKey(f"AWS KMS cannot unwrap that key ({kind}).")
        raise KeyProviderError(
            f"AWS KMS refused {operation}: {kind} ({response.status_code})."
        )


def _text(answer: Mapping[str, Any], name: str) -> str:
    """The base64 field ``name`` of an answer, checked.

    Raises:
        KeyProviderError: It is missing, or not base64.
    """
    value = answer.get(name)
    try:
        if not isinstance(value, str):
            raise ValueError
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise KeyProviderError(f"AWS KMS answered without {name}.") from None
    return value


def _bytes(answer: Mapping[str, Any], name: str) -> bytes:
    """The base64 field ``name`` of an answer, decoded."""
    return base64.b64decode(_text(answer, name))

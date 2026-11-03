"""Google Cloud Storage and Azure Blob storage, and their credentials."""

import base64
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from domainkit.clock import freeze_time
from domainkit.cloud import CloudAuthError, TokenSource
from domainkit.cloud.azure import EntraApplication, ManagedIdentity, shared_key
from domainkit.cloud.google import MetadataAccount, ServiceAccount
from domainkit.http.client import stub
from domainkit.storage import FileMissing, StorageError
from domainkit.storage.azure import AzureBlobStorage
from domainkit.storage.gcs import SCOPE, GCSStorage

pytestmark = pytest.mark.anyio

MOMENT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
INFO = {
    "type": "service_account",
    "client_email": "files@acme.iam.gserviceaccount.com",
    "private_key": PEM,
    "private_key_id": "k1",
    "token_uri": "https://oauth2.example/token",
}
ACCOUNT_KEY = base64.b64encode(b"a storage account key, 32 bytes!").decode()


def verify_jwt(assertion: str) -> dict[str, object]:
    """The claims of an RS256 JWT, checked against the test's key."""
    head, body, signature = assertion.split(".")
    KEY.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{head}.{body}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    claims: dict[str, object] = json.loads(base64.urlsafe_b64decode(body + "=="))
    return claims


class FakeGCS:
    """A bucket in memory, its token endpoint, and resumable sessions."""

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, bytes] = {}
        self.sessions: dict[str, bytearray] = {}
        self.tokens = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        url = request.url
        if url.host == "oauth2.example":
            form = dict(parse_qsl(request.content.decode()))
            claims = verify_jwt(form["assertion"])
            assert claims["iss"] == INFO["client_email"]
            self.tokens += 1
            return httpx2.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        assert request.headers["authorization"] == "Bearer tok"
        query = dict(parse_qsl(url.query.decode()))
        if url.path.startswith("/upload/") and query.get("uploadType") == "media":
            self.objects[query["name"]] = request.content
            return httpx2.Response(200, json={"name": query["name"]})
        if url.path.startswith("/upload/"):
            session = (
                f"https://gcs.example/session/{len(self.sessions)}?name={query['name']}"
            )
            self.sessions[session] = bytearray()
            return httpx2.Response(200, headers={"Location": session})
        if url.path.startswith("/session/"):
            session = str(url)
            if request.method == "DELETE":
                del self.sessions[session]
                return httpx2.Response(499)
            span = request.headers["content-range"].removeprefix("bytes ")
            if span.startswith("*/"):
                data = self.sessions.pop(session)
                assert len(data) == int(span[2:])
                self.objects[query["name"]] = bytes(data)
                return httpx2.Response(200, json={})
            self.sessions[session] += request.content
            return httpx2.Response(308)
        key = unquote(url.path.split("/o/", 1)[1])
        if key not in self.objects:
            return httpx2.Response(404)
        if request.method == "DELETE":
            del self.objects[key]
            return httpx2.Response(204)
        if query.get("alt") == "media":
            start, end = map(int, request.headers["range"][6:].split("-"))
            return httpx2.Response(206, content=self.objects[key][start : end + 1])
        return httpx2.Response(200, json={"size": str(len(self.objects[key]))})


@pytest.fixture
def gcs() -> Iterator[tuple[GCSStorage, FakeGCS]]:
    """A storage on the fake, its parts five bytes, files under ``media/``."""
    fake = FakeGCS()
    storage = GCSStorage(
        "acme-files",
        account=ServiceAccount.from_info(INFO),
        endpoint="https://gcs.example",
        prefix="media/",
    )
    storage.part_size = 5
    with stub(fake):
        yield storage, fake


async def test_gcs_keeps_reads_and_deletes_files(
    gcs: tuple[GCSStorage, FakeGCS],
) -> None:
    """A media upload, a resumable one in chunks, ranged reads; one token for all."""
    storage, fake = gcs

    async def content() -> AsyncIterator[bytes]:
        yield b"hello "
        yield b"world!"

    small = await storage.save(b"abc")
    large = await storage.save(content())

    assert fake.objects == {f"media/{small}": b"abc", f"media/{large}": b"hello world!"}
    assert await storage.size(large) == 12
    async with await storage.open(large) as file:
        assert await file.read() == b"hello world!"
    assert await storage.delete(small)
    assert not await storage.delete(small)
    assert not await storage.exists(small)
    assert fake.tokens == 1


async def test_gcs_drops_a_session_whose_content_failed(
    gcs: tuple[GCSStorage, FakeGCS],
) -> None:
    """The resumable session is deleted; nothing appears."""
    storage, fake = gcs

    async def content() -> AsyncIterator[bytes]:
        yield b"0123456789"
        raise OSError("gone")

    with pytest.raises(OSError, match="gone"):
        await storage.save(content())

    assert fake.objects == {}
    assert fake.sessions == {}


async def test_gcs_refusals_and_odd_answers(gcs: tuple[GCSStorage, FakeGCS]) -> None:
    """A 403, a session without a URL, metadata without a size."""
    storage, _ = gcs
    await storage.account.token(SCOPE)  # Held: the refusals below are the bucket's.
    with (
        stub(lambda request: httpx2.Response(403)),
        pytest.raises(StorageError, match="403"),
    ):
        await storage._head("a")
    with (
        stub(lambda request: httpx2.Response(200, json={})),
        pytest.raises(StorageError, match="without the size"),
    ):
        await storage._head("a")
    with (
        stub(lambda request: httpx2.Response(200)),
        pytest.raises(StorageError, match="without its URL"),
    ):
        await storage._start("a")


async def test_gcs_signs_v4_urls_with_the_account_key(
    gcs: tuple[GCSStorage, FakeGCS],
) -> None:
    """GOOG4-RSA-SHA256 over Google's canonical request, verifiable by its key."""
    storage, _ = gcs
    with freeze_time(MOMENT):
        upload = await storage.presigned_upload(size=3, content_type="text/plain")

    parts = urlsplit(upload.url)
    query = dict(parse_qsl(parts.query))
    assert query["X-Goog-Credential"] == (
        f"{INFO['client_email']}/20260304/auto/storage/goog4_request"
    )
    assert query["X-Goog-SignedHeaders"] == "content-length;content-type;host"
    canonical = "\n".join(
        [
            "PUT",
            parts.path,
            "&".join(
                f"{name}={quote(value, safe='-_.~')}"
                for name, value in sorted(parse_qsl(parts.query))
                if name != "X-Goog-Signature"
            ),
            "content-length:3\ncontent-type:text/plain\nhost:gcs.example\n",
            "content-length;content-type;host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    to_sign = "\n".join(
        [
            "GOOG4-RSA-SHA256",
            "20260304T050607Z",
            "20260304/auto/storage/goog4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    KEY.public_key().verify(
        bytes.fromhex(query["X-Goog-Signature"]),
        to_sign.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


async def test_the_metadata_server_gives_tokens_but_cannot_sign() -> None:
    """GKE's or Compute Engine's identity: a token per scope; no presigned URL."""
    asked: list[httpx2.Request] = []

    def server(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(200, json={"access_token": "meta", "expires_in": 60})

    account = MetadataAccount(url="http://metadata.example/sa")
    with stub(server):
        assert await account.token("scope-a") == "meta"
        assert await account.token("scope-a") == "meta"  # Under 5 minutes: asked again.
    storage = GCSStorage("acme-files", account=account)

    assert len(asked) == 2
    assert asked[0].headers["metadata-flavor"] == "Google"
    with pytest.raises(CloudAuthError, match="cannot sign"):
        await storage.presigned_url("a.txt")


async def test_google_credentials_that_do_not_hold(tmp_path: Path) -> None:
    """Unreadable files, other key kinds, refusals, and silence are CloudAuthError."""
    missing = tmp_path / "missing.json"
    not_json = tmp_path / "bad.json"
    not_json.write_text("{")
    ec_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    for path in (missing, not_json):
        with pytest.raises(CloudAuthError, match="could not be read"):
            ServiceAccount.from_file(path)
    with pytest.raises(CloudAuthError, match="not a service account"):
        ServiceAccount.from_info({"type": "authorized_user"})
    with pytest.raises(CloudAuthError, match="could not be read"):
        ServiceAccount("a@b", "not a key")
    with pytest.raises(CloudAuthError, match="an RSA key"):
        ServiceAccount("a@b", ec_key.decode())
    account = ServiceAccount.from_info(INFO)
    with (
        stub(lambda request: httpx2.Response(400)),
        pytest.raises(CloudAuthError, match=r"refused a token \(400\)"),
    ):
        await account.token("s")
    with (
        stub(lambda request: httpx2.Response(200, json={})),
        pytest.raises(CloudAuthError, match="without a token"),
    ):
        await account.token("s")

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    with stub(unreachable), pytest.raises(CloudAuthError, match="could not be reached"):
        await account.token("s")
    with stub(unreachable), pytest.raises(CloudAuthError, match="could not be reached"):
        await MetadataAccount(url="http://metadata.example").token("s")
    key_file = tmp_path / "sa.json"
    key_file.write_text(json.dumps(INFO))
    assert (
        GCSStorage("acme-files", credentials_file=str(key_file)).account.email
        == (INFO["client_email"])
    )
    assert isinstance(GCSStorage("acme-files").account, MetadataAccount)
    with pytest.raises(ValueError, match="bucket name"):
        GCSStorage("A", account=account)


def expected_shared_key(request: httpx2.Request) -> str:
    """Shared Key's signature, assembled as Microsoft's documentation lays it out."""
    headers = {name.lower(): value for name, value in request.headers.items()}
    length = headers.get("content-length", "")
    lines = [
        request.method,
        headers.get("content-encoding", ""),
        headers.get("content-language", ""),
        "" if length == "0" else length,
        headers.get("content-md5", ""),
        headers.get("content-type", ""),
        headers.get("date", ""),
        headers.get("if-modified-since", ""),
        headers.get("if-match", ""),
        headers.get("if-none-match", ""),
        headers.get("if-unmodified-since", ""),
        headers.get("range", ""),
    ]
    ms = "".join(
        f"{name}:{headers[name]}\n"
        for name in sorted(headers)
        if name.startswith("x-ms-")
    )
    resource = f"/acmefiles{request.url.path}"
    for name, value in sorted(parse_qsl(request.url.query.decode())):
        resource += f"\n{name}:{value}"
    to_sign = "\n".join(lines) + "\n" + ms + resource
    digest = hmac.new(base64.b64decode(ACCOUNT_KEY), to_sign.encode(), hashlib.sha256)
    return f"SharedKey acmefiles:{base64.b64encode(digest.digest()).decode()}"


class FakeBlob:
    """A container in memory that checks every Shared Key signature."""

    def __init__(self) -> None:
        """Start empty."""
        self.blobs: dict[str, bytes] = {}
        self.blocks: dict[str, dict[str, bytes]] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        if request.headers["authorization"].startswith("SharedKey"):
            assert request.headers["authorization"] == expected_shared_key(request)
        assert request.headers["x-ms-version"] == "2023-11-03"
        key = unquote(request.url.path.removeprefix("/media/"))
        query = dict(parse_qsl(request.url.query.decode()))
        if query.get("comp") == "block":
            self.blocks.setdefault(key, {})[query["blockid"]] = request.content
            return httpx2.Response(201)
        if query.get("comp") == "blocklist":
            staged = self.blocks.pop(key)
            ids = re.findall(r"<Latest>([^<]+)</Latest>", request.content.decode())
            self.blobs[key] = b"".join(staged[block] for block in ids)
            return httpx2.Response(201)
        if request.method == "PUT":
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            self.blobs[key] = request.content
            return httpx2.Response(201)
        if key not in self.blobs:
            return httpx2.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
        if request.method == "HEAD":
            return httpx2.Response(
                200, headers={"Content-Length": str(len(self.blobs[key]))}
            )
        if request.method == "DELETE":
            del self.blobs[key]
            return httpx2.Response(202)
        start, end = map(int, request.headers["x-ms-range"][6:].split("-"))
        return httpx2.Response(206, content=self.blobs[key][start : end + 1])


@pytest.fixture
def blob() -> Iterator[tuple[AzureBlobStorage, FakeBlob]]:
    """A Shared Key storage on the fake, its parts five bytes."""
    fake = FakeBlob()
    storage = AzureBlobStorage(
        "acmefiles", "media", account_key=ACCOUNT_KEY, endpoint="https://blob.example"
    )
    storage.part_size = 5
    with stub(fake):
        yield storage, fake


async def test_azure_keeps_reads_and_deletes_files(
    blob: tuple[AzureBlobStorage, FakeBlob],
) -> None:
    """Put Blob, blocks and their list, ranged reads; every request signed right."""
    storage, fake = blob

    async def content() -> AsyncIterator[bytes]:
        yield b"hello world!"

    small = await storage.save(b"abc")
    large = await storage.save(content())

    assert fake.blobs == {small: b"abc", large: b"hello world!"}
    assert fake.blocks == {}
    async with await storage.open(large) as file:
        assert await file.read() == b"hello world!"
    assert await storage.delete(large)
    assert not await storage.delete(large)
    with pytest.raises(FileMissing):
        await storage.size(large)


async def test_azure_refusals_name_the_error_code(
    blob: tuple[AzureBlobStorage, FakeBlob],
) -> None:
    """Another status is a StorageError naming Azure's code, and nothing else."""
    storage, _ = blob

    def refuse(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, headers={"x-ms-error-code": "AuthorizationFailure"})

    with (
        stub(refuse),
        pytest.raises(StorageError, match=r"403 to PUT \(AuthorizationFailure\)"),
    ):
        await storage.save(b"abc")


async def test_azure_presigns_a_service_sas(
    blob: tuple[AzureBlobStorage, FakeBlob],
) -> None:
    """Read for GET, create and write for PUT, https only, expiring, key-signed."""
    storage, _ = blob
    with freeze_time(MOMENT):
        url = await storage.presigned_url("a.txt", expires=timedelta(hours=1))
        upload = await storage.presigned_upload(size=3, content_type="text/plain")

    query = dict(parse_qsl(urlsplit(url).query))
    assert (query["sp"], query["sr"], query["spr"]) == ("r", "b", "https")
    assert query["se"] == "2026-03-04T06:06:07Z"
    to_sign = "\n".join(
        [
            "r",
            "",
            query["se"],
            "/blob/acmefiles/media/a.txt",
            "",
            "",
            "https",
            "2022-11-02",
            "b",
            *[""] * 7,
        ]
    )
    digest = hmac.new(base64.b64decode(ACCOUNT_KEY), to_sign.encode(), hashlib.sha256)
    assert query["sig"] == base64.b64encode(digest.digest()).decode()
    assert dict(parse_qsl(urlsplit(upload.url).query))["sp"] == "cw"
    assert upload.headers["x-ms-blob-type"] == "BlockBlob"


async def test_azure_with_an_entra_token() -> None:
    """A bearer token instead of Shared Key; no SAS without the account key."""

    class Fixed(TokenSource):
        async def _fetch(self, scope: str) -> tuple[str, float]:
            assert scope == "https://storage.azure.com/.default"
            return "entra-token", 3600

    seen: list[httpx2.Request] = []

    def upstream(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(201)

    storage = AzureBlobStorage("acmefiles", "media", credential=Fixed())
    with stub(upstream):
        await storage.save(b"x")

    assert seen[0].headers["authorization"] == "Bearer entra-token"
    assert str(seen[0].url).startswith("https://acmefiles.blob.core.windows.net/media/")
    with pytest.raises(CloudAuthError, match="account's key"):
        await storage.presigned_url("a.txt")


def test_azure_options_are_checked() -> None:
    """Names Azure refuses, and neither or both kinds of credential."""
    with pytest.raises(ValueError, match="account name"):
        AzureBlobStorage("Acme_Files", "media", account_key=ACCOUNT_KEY)
    with pytest.raises(ValueError, match="container name"):
        AzureBlobStorage("acmefiles", "Media", account_key=ACCOUNT_KEY)
    with pytest.raises(ValueError, match="one of them"):
        AzureBlobStorage("acmefiles", "media")


def test_a_key_that_is_not_base64_is_refused() -> None:
    """Shared Key decodes the account key first."""
    with pytest.raises(CloudAuthError, match="not base64"):
        shared_key("GET", "https://a/b", {}, account="a", key="not base64!")


async def test_entra_ids_tokens() -> None:
    """The client credentials grant, and the managed identity's metadata service."""
    asked: list[httpx2.Request] = []

    def entra(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(200, json={"access_token": "t", "expires_in": 3599})

    app = EntraApplication(
        "tenant", "client", "s3cret", authority="https://login.example"
    )
    identity = ManagedIdentity(
        client_id="user-assigned", url="http://imds.example/token"
    )
    with stub(entra):
        assert await app.token("https://vault.azure.net/.default") == "t"
        assert await identity.token("https://vault.azure.net/.default") == "t"

    form = dict(parse_qsl(asked[0].content.decode()))
    assert str(asked[0].url) == "https://login.example/tenant/oauth2/v2.0/token"
    assert (form["grant_type"], form["client_secret"]) == (
        "client_credentials",
        "s3cret",
    )
    params = dict(parse_qsl(asked[1].url.query.decode()))
    assert params == {
        "api-version": "2018-02-01",
        "resource": "https://vault.azure.net",
        "client_id": "user-assigned",
    }
    assert asked[1].headers["metadata"] == "true"
    assert "s3cret" not in repr(app)

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    with stub(unreachable):
        with pytest.raises(CloudAuthError, match="Entra ID could not be reached"):
            await app.token("other")
        with pytest.raises(CloudAuthError, match="metadata service could not"):
            await ManagedIdentity(url="http://imds.example").token("other")


def test_an_empty_bodys_length_is_signed_as_empty() -> None:
    """Shared Key signs Content-Length 0 as no length at all (version 2015 on)."""
    common = {
        "x-ms-date": "Wed, 04 Mar 2026 05:06:07 GMT",
        "x-ms-version": "2023-11-03",
    }

    with_zero = shared_key(
        "DELETE",
        "https://a/m/x",
        {**common, "Content-Length": "0"},
        account="a",
        key=ACCOUNT_KEY,
    )

    assert with_zero == shared_key(
        "DELETE", "https://a/m/x", common, account="a", key=ACCOUNT_KEY
    )


def test_a_key_without_its_id_names_none() -> None:
    """The JWT's header has a ``kid`` only when the key file names one."""
    account = ServiceAccount(INFO["client_email"], PEM)

    header = account.assertion("s", 0).split(".")[0]

    assert json.loads(base64.urlsafe_b64decode(header + "==")) == {
        "alg": "RS256",
        "typ": "JWT",
    }

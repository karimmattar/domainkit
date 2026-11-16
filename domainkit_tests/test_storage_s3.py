"""``S3Storage``: SigV4 as AWS documents it, and a store that keeps files."""

import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest

from domainkit.clock import freeze_time
from domainkit.http.client import stub
from domainkit.storage import FileMissing, StorageError
from domainkit.storage.remote import ObjectFile, PresignedUpload
from domainkit.storage.s3 import S3Storage

pytestmark = pytest.mark.anyio

# AWS's documented example credentials and moment (Signature Version 4 for S3).
ACCESS = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
MOMENT = datetime(2013, 5, 24, tzinfo=UTC)


@pytest.fixture
def example() -> Iterator[S3Storage]:
    """``examplebucket`` in us-east-1, its global endpoint, at AWS's moment."""
    with freeze_time(MOMENT):
        yield S3Storage(
            "examplebucket",
            region="us-east-1",
            endpoint="https://s3.amazonaws.com",
            access_key_id=ACCESS,
            secret_access_key=SECRET,
        )


async def test_a_ranged_get_is_signed_as_aws_documents_it(example: S3Storage) -> None:
    """GET /test.txt, Range bytes=0-9: AWS's example signature, to the character."""
    seen: list[httpx2.Request] = []

    def upstream(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(206, content=b"0123456789")

    with stub(upstream):
        data = await example._range("test.txt", 0, 9)

    assert data == b"0123456789"
    (request,) = seen
    assert str(request.url) == "https://examplebucket.s3.amazonaws.com/test.txt"
    assert request.headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/"
        "aws4_request, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


async def test_a_presigned_url_is_signed_as_aws_documents_it(
    example: S3Storage,
) -> None:
    """GET /test.txt for a day: AWS's example presigned URL."""
    url = await example.presigned_url("test.txt", expires=timedelta(days=1))

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    assert (parts.netloc, parts.path) == ("examplebucket.s3.amazonaws.com", "/test.txt")
    assert query["X-Amz-Signature"] == (
        "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )
    assert query["X-Amz-Credential"] == f"{ACCESS}/20130524/us-east-1/s3/aws4_request"
    assert query["X-Amz-Expires"] == "86400"


class FakeS3:
    """A bucket in memory that answers S3's requests, signed or not."""

    def __init__(self, *, write_once: bool = True) -> None:
        """Start empty; ``write_once``: honour ``If-None-Match: *`` on ``PUT``."""
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.requests: list[tuple[str, str]] = []
        self.write_once = write_once

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        key = request.url.path.removeprefix("/files")
        query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        self.requests.append((request.method, key))
        if request.method == "POST" and "uploads" in query:
            upload = f"up-{len(self.uploads)}"
            self.uploads[upload] = {}
            body = f"<Result><UploadId>{upload}</UploadId></Result>"
            return httpx2.Response(200, content=body.encode())
        if request.method == "PUT" and "uploadId" in query:
            parts = self.uploads[query["uploadId"]]
            parts[int(query["partNumber"])] = request.content
            return httpx2.Response(
                200, headers={"ETag": f'"etag-{query["partNumber"]}"'}
            )
        if request.method == "POST" and "uploadId" in query:
            parts = self.uploads.pop(query["uploadId"])
            numbers = [
                int(n)
                for n in re.findall(r"<PartNumber>(\d+)<", request.content.decode())
            ]
            self.objects[key] = b"".join(parts[n] for n in numbers)
            return httpx2.Response(200, content=b"<CompleteMultipartUploadResult/>")
        if request.method == "DELETE" and "uploadId" in query:
            self.uploads.pop(query["uploadId"], None)
            return httpx2.Response(204)
        if request.method == "PUT":
            created = request.headers.get("if-none-match") == "*"
            if created and self.write_once and key in self.objects:
                code = b"<Error><Code>PreconditionFailed</Code></Error>"
                return httpx2.Response(412, content=code)
            self.objects[key] = request.content
            return httpx2.Response(200)
        if key not in self.objects:
            code = b"<Error><Code>NoSuchKey</Code><Key>secret/name</Key></Error>"
            return httpx2.Response(
                404, content=b"" if request.method == "HEAD" else code
            )
        data = self.objects[key]
        if request.method == "HEAD":
            return httpx2.Response(200, headers={"Content-Length": str(len(data))})
        if request.method == "DELETE":
            del self.objects[key]
            return httpx2.Response(204)
        start, end = map(
            int, request.headers["range"].removeprefix("bytes=").split("-")
        )
        return httpx2.Response(206, content=data[start : end + 1])


@pytest.fixture
def bucket() -> Iterator[tuple[S3Storage, FakeS3]]:
    """A MinIO-style storage (path style), its parts five bytes, on a fake bucket."""
    fake = FakeS3()
    storage = S3Storage(
        "files",
        region="us-east-1",
        endpoint="http://minio:9000",
        path_style=True,
        access_key_id="minio",
        secret_access_key="minio-secret",
        prefix="media/",
    )
    storage.part_size = 5
    with stub(fake):
        yield storage, fake


async def test_a_small_file_is_one_put_and_reads_back(
    bucket: tuple[S3Storage, FakeS3],
) -> None:
    """Under a part: one PUT under the prefix; read, sized, and found."""
    storage, fake = bucket

    name = await storage.save(b"abc", extension=".txt")

    assert fake.requests == [("PUT", f"/media/{name}")]
    assert await storage.exists(name)
    assert await storage.size(name) == 3
    async with await storage.open(name) as file:
        assert await file.read() == b"abc"


async def test_a_large_file_is_sent_in_parts_and_read_in_blocks(
    bucket: tuple[S3Storage, FakeS3], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twelve bytes in parts of five; read back four bytes per request."""
    storage, fake = bucket
    monkeypatch.setattr("domainkit.storage.remote.BLOCK_SIZE", 4)

    async def content() -> AsyncIterator[bytes]:
        for piece in (b"hello ", b"wor", b"ld!"):
            yield piece

    name = await storage.save(content())

    assert fake.objects[f"/media/{name}"] == b"hello world!"
    assert [method for method, _ in fake.requests] == [
        "POST",
        "PUT",
        "PUT",
        "PUT",
        "POST",
    ]
    file = await storage.open(name)
    assert isinstance(file, ObjectFile)
    assert await file.read(3) == b"hel"
    assert (
        b"".join([chunk async for chunk in file.chunks(chunk_size=5)]) == b"lo world!"
    )


async def test_a_failing_upload_is_aborted_and_keeps_nothing(
    bucket: tuple[S3Storage, FakeS3],
) -> None:
    """The parts sent are dropped; no object appears."""
    storage, fake = bucket

    async def content() -> AsyncIterator[bytes]:
        yield b"0123456789"
        raise OSError("the client went away")

    with pytest.raises(OSError, match="went away"):
        await storage.save(content())

    assert fake.objects == {}
    assert fake.uploads == {}
    assert fake.requests[-1][0] == "DELETE"


async def test_missing_files_and_refusals(bucket: tuple[S3Storage, FakeS3]) -> None:
    """404 is a missing file; any other status an error naming S3's code only."""
    storage, _ = bucket
    name = await storage.save(b"x")

    assert await storage.delete(name)
    assert not await storage.delete(name)
    assert not await storage.exists(name)
    with pytest.raises(FileMissing):
        await storage.open(name)
    with pytest.raises(FileMissing):
        await storage.size(name)
    with pytest.raises(StorageError, match=r"404 to GET \(NoSuchKey\)") as refused:
        await storage._range(name, 0, 1)
    assert "secret" not in str(refused.value)


async def test_presigned_uploads_sign_their_size_and_type(
    bucket: tuple[S3Storage, FakeS3],
) -> None:
    """A new name; the URL signs Content-Length and Content-Type, and expires.

    It signs If-None-Match too: it creates the name, never replaces it (A5-4).
    """
    storage, _ = bucket

    upload = await storage.presigned_upload(
        size=1024, content_type="image/png", directory="avatars", extension=".png"
    )

    assert isinstance(upload, PresignedUpload)
    assert upload.name.startswith("avatars/")
    assert upload.name.endswith(".png")
    assert upload.method == "PUT"
    assert upload.headers == {
        "Content-Length": "1024",
        "Content-Type": "image/png",
        "If-None-Match": "*",
    }
    query = dict(parse_qsl(urlsplit(upload.url).query))
    assert query["X-Amz-SignedHeaders"] == (
        "content-length;content-type;host;if-none-match"
    )
    assert urlsplit(upload.url).path == f"/files/media/{upload.name}"
    with pytest.raises(ValueError, match="positive size"):
        await storage.presigned_upload(size=0, content_type="image/png")
    with pytest.raises(ValueError, match="seven days"):
        await storage.presigned_url(upload.name, expires=timedelta(days=8))
    with pytest.raises(FileMissing):
        await storage.presigned_url("../etc/passwd")


def test_options_are_checked() -> None:
    """A bad bucket, endpoint, prefix, or half a key pair is refused at once."""
    with pytest.raises(ValueError, match="bucket name"):
        S3Storage("Bad_Bucket", region="eu-west-1")
    with pytest.raises(ValueError, match="endpoint"):
        S3Storage("files", region="auto", endpoint="ftp://r2")
    with pytest.raises(ValueError, match="prefix"):
        S3Storage("files", region="eu-west-1", prefix="media")
    with pytest.raises(ValueError, match="both"):
        S3Storage("files", region="eu-west-1", access_key_id="a")


def test_public_files_have_a_url() -> None:
    """With ``public_url``, the CDN's URL of the key; without, none."""
    cdn = S3Storage(
        "files", region="eu-west-1", prefix="m/", public_url="https://cdn.example.com/"
    )

    assert cdn.url("a.png") == "https://cdn.example.com/m/a.png"
    assert S3Storage("files", region="eu-west-1").url("a.png") is None


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({"POST?uploads": httpx2.Response(200, content=b"<R/>")}, "without its id"),
        ({"PUT?part": httpx2.Response(200)}, "without naming its ETag"),
        (
            {
                "POST?finish": httpx2.Response(
                    200, content=b"<Error><Code>Slow</Code></Error>"
                )
            },
            r"refused to join the parts \(Slow\)",
        ),
    ],
)
async def test_answers_s3_should_not_give_are_refused(
    bucket: tuple[S3Storage, FakeS3],
    answer: dict[str, httpx2.Response],
    message: str,
) -> None:
    """An upload without its id, a part without an ETag, an error in a 200."""
    storage, fake = bucket

    def odd(request: httpx2.Request) -> httpx2.Response:
        query = request.url.query.decode()
        kind = (
            "POST?uploads"
            if "uploads" in query
            else "PUT?part"
            if "partNumber" in query
            else "POST?finish"
            if request.method == "POST"
            else ""
        )
        return answer.get(kind) or fake(request)

    with stub(odd), pytest.raises(StorageError, match=message):
        await storage.save(b"0123456789")


async def test_a_head_refused_and_a_file_that_shrank(
    bucket: tuple[S3Storage, FakeS3],
) -> None:
    """A HEAD answered 403 is an error; a read past the object's end stops."""
    storage, fake = bucket
    name = await storage.save(b"abcd")

    with (
        stub(lambda request: httpx2.Response(403)),
        pytest.raises(StorageError, match="403 to HEAD"),
    ):
        await storage.exists(name)
    file = await storage.open(name)
    fake.objects[f"/media/{name}"] = b""  # Cut short behind the reader's back.
    assert await file.read() == b""


async def test_temporary_credentials_send_their_session_token() -> None:
    """The token is signed in requests, and named in presigned URLs."""
    from domainkit.keys.aws import AwsCredentials

    async def temporary() -> AwsCredentials:
        return AwsCredentials("ASIA", "secret", "the-session-token")

    storage = S3Storage("files", region="eu-west-1", credentials=temporary)
    seen: list[httpx2.Request] = []

    def upstream(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    with stub(upstream):
        await storage.save(b"x")
    url = await storage.presigned_url("a.txt")

    assert seen[0].headers["x-amz-security-token"] == "the-session-token"
    assert "x-amz-security-token" in seen[0].headers["authorization"]
    assert dict(parse_qsl(urlsplit(url).query))["X-Amz-Security-Token"] == (
        "the-session-token"
    )


PROBE = re.compile(r"/media/write-once-probes/[0-9a-f]{32}")
"""Where the write-once probe writes: under the prefix, a new name each time."""


def client_put(fake: FakeS3, name: str, body: bytes) -> httpx2.Response:
    """What the client sends to a presigned upload's URL, as the fake reads it."""
    headers = {"If-None-Match": "*", "Authorization": "AWS4-HMAC-SHA256 client"}
    url = f"http://minio:9000/files/media/{name}"
    return fake(httpx2.Request("PUT", url, headers=headers, content=body))


@pytest.mark.parametrize("write_once", [True, False])
async def test_presigned_uploads_take_the_stores_word_unless_told_to_check(
    bucket: tuple[S3Storage, FakeS3], write_once: bool
) -> None:
    """Without ``check_write_once``, the store is not asked.

    One that ignores ``If-None-Match`` lets the URL replace its file once
    it was checked, and nothing says so (user report U4).
    """
    storage, fake = bucket
    fake.write_once = write_once

    upload = await storage.presigned_upload(size=1, content_type="text/plain")
    client_put(fake, upload.name, b"checked")
    again = client_put(fake, upload.name, b"replaced")

    assert again.status_code == (412 if write_once else 200)
    kept = fake.objects[f"/media/{upload.name}"]
    assert kept == (b"checked" if write_once else b"replaced")
    assert [method for method, _ in fake.requests] == ["PUT", "PUT"]


async def test_the_probe_passes_a_store_that_refuses_a_second_create(
    bucket: tuple[S3Storage, FakeS3],
) -> None:
    """``check_write_once``: one probe per storage, written twice, then deleted."""
    storage, fake = bucket
    storage.check_write_once = True

    first = await storage.presigned_upload(size=1, content_type="text/plain")
    second = await storage.presigned_upload(size=1, content_type="text/plain")

    assert first.name != second.name
    assert [method for method, _ in fake.requests] == ["PUT", "PUT", "DELETE"]
    assert len({key for _, key in fake.requests}) == 1
    assert PROBE.fullmatch(fake.requests[0][1])
    assert fake.objects == {}


async def test_presigned_uploads_are_refused_where_the_store_replaces_files(
    bucket: tuple[S3Storage, FakeS3],
) -> None:
    """A store that took the second write: refused, the probe deleted, asked again.

    Garage and Scality CloudServer read no condition on ``PutObject``: a
    presigned upload's URL could replace the file once it was checked
    (user report U4).
    """
    storage, fake = bucket
    storage.check_write_once = True
    fake.write_once = False

    for _ in range(2):
        with pytest.raises(StorageError) as refused:
            await storage.presigned_upload(size=1, content_type="text/plain")

    assert str(refused.value) == (
        "The S3 store at http://minio:9000 replaced an object written with "
        "If-None-Match: *: it ignores the header, so a presigned upload's URL "
        "could replace the file it made. Use a store that honours it, or "
        "upload through the application."
    )
    assert [method for method, _ in fake.requests] == ["PUT", "PUT", "DELETE"] * 2
    assert fake.objects == {}


@pytest.mark.parametrize(
    ("answers", "outcome"),
    [
        ({"second": httpx2.Response(409)}, None),
        (
            {"first": httpx2.Response(501, content=b"<Code>NotImplemented</Code>")},
            r"^S3 answered 501 to PUT \(NotImplemented\)\.$",
        ),
        (
            {"second": httpx2.Response(500, content=b"<Code>InternalError</Code>")},
            r"^S3 answered 500 to the write-once probe \(InternalError\)\.$",
        ),
    ],
)
async def test_the_probe_reads_what_else_the_store_answers(
    bucket: tuple[S3Storage, FakeS3],
    answers: dict[str, httpx2.Response],
    outcome: str | None,
) -> None:
    """A 409 refuses the second write too.

    A refused first write fails, as does an odd answer to the second.
    """
    storage, fake = bucket
    puts: list[str] = []

    def answering(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT":
            puts.append("first" if not puts else "second")
            if puts[-1] in answers:
                return answers[puts[-1]]
        return fake(request)

    with stub(answering):
        if outcome is None:
            await storage.probe_write_once()
        else:
            with pytest.raises(StorageError, match=outcome):
                await storage.probe_write_once()


async def test_a_probe_the_store_will_not_delete_stays_and_the_answer_stands(
    bucket: tuple[S3Storage, FakeS3], caplog: pytest.LogCaptureFixture
) -> None:
    """Credentials that may not delete: the probe stays, logged; the store passed."""
    storage, fake = bucket

    def keeping(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            return httpx2.Response(403, content=b"<Code>AccessDenied</Code>")
        return fake(request)

    with stub(keeping):
        await storage.probe_write_once()

    (kept,) = fake.objects
    assert PROBE.fullmatch(kept)
    assert caplog.messages == [
        (
            f"The write-once probe {kept.removeprefix('/media/')} stays in the "
            "bucket: S3 answered 403 to DELETE (AccessDenied)."
        )
    ]

# Files and uploads

Uploaded files are a common way to attack a web service: a file name that
climbs out of the upload directory, a "picture" that is really HTML with
a script in it, a file too large for the disk. domainkit stores files
under names that it makes up, decides their type from their content,
stops large files while they arrive, and serves files only from routes
that say who may read them.

On this page you learn how to:

- configure storages: the file system, memory, S3, Google Cloud Storage,
  and Azure Blob storage;
- save, read, and delete files with a `Storage`;
- receive an upload safely with `save_upload`, and serve a file with
  `serve`;
- give an upload route its own body limit with `body_limit`;
- let a client send or fetch a file directly, with presigned URLs;
- scan uploads for viruses, and make thumbnails of images.

The decisions are in
[ADR 0040](adr/0040-store-files-under-random-names.md).

## A first example

A route that accepts a PDF or an image of up to 2 MB, and stores it:

```python
from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel

from domainkit.http.access import Require
from domainkit.http.middleware import body_limit
from domainkit.http.uploads import IMAGES, PDF, save_upload

router = APIRouter()


class DocumentOut(BaseModel):
    name: str
    content_type: str
    size: int
    filename: str


@router.post("/documents", dependencies=[Depends(Require())])
@body_limit(2_100_000)  # the file's 2 MB, and the form around it
async def upload_document(file: UploadFile) -> DocumentOut:
    stored = await save_upload(
        file, types=(*IMAGES, PDF), max_size=2_000_000, directory="documents"
    )
    return DocumentOut(
        name=stored.name,
        content_type=stored.content_type,
        size=stored.size,
        filename=stored.filename,
    )
```

The settings name where files go:

```python
STORAGES = {
    "default": {
        "BACKEND": "domainkit.storage.FileSystemStorage",
        "OPTIONS": {"location": "/srv/shop/files"},
    },
}
```

A client sends `report<1>.pdf`, with the type `text/html`, and the content
`%PDF-1.7 hello`. The answer:

```json
{
  "name": "documents/6f6fde1fe7a9a1024ed58b49399350bd.pdf",
  "content_type": "application/pdf",
  "size": 14,
  "filename": "report1.pdf"
}
```

- `name` is the storage's name for the file. Keep it in your table, to
  find the file again.
- `content_type` comes from the file's first bytes, not from the
  `text/html` that the client claimed.
- `filename` is the client's name, cleaned. Show it to people; never use
  it as a path.

FastAPI reads uploads with `python-multipart`. Install the `uploads`
extra:

```bash
pip install "domainkit[uploads]"
```

## Storages

A storage keeps files under names that it chooses. You configure storages
in `STORAGES`, by alias, as in Django. `storages[alias]` gives you the
`Storage` of an alias:

```python
from domainkit.storage import storages

storage = storages["default"]
name = await storage.save(b"%PDF-1.7 invoice", directory="invoices", extension=".pdf")
print(name)

async with await storage.open(name) as file:
    print(await file.read())  # or: async for chunk in file.chunks(): ...

print(await storage.size(name), await storage.exists(name), storage.url(name))
print(await storage.delete(name), await storage.delete(name))
```

```text
invoices/a343f3c8b917b52e25f256705163ac3c.pdf
b'%PDF-1.7 invoice'
16 True None
True False
```

| Call | What it does |
| --- | --- |
| `await save(content, directory="", extension="", max_size=None)` | Stores bytes, or an async iterable of chunks, under a new name, and returns the name. Past `max_size` bytes, it raises `FileTooLarge` and keeps nothing. |
| `await open(name)` | Returns a `StoredFile` to `read()`, or to read with `chunks()`. Close it, or use `async with`. Raises `FileMissing`. |
| `await size(name)` | The size in bytes. Raises `FileMissing`. |
| `await exists(name)` | `True` if a file has the name. |
| `await delete(name)` | Deletes the file. Returns `True` if it existed. |
| `url(name)` | Where clients can fetch the file themselves, if a web server or a CDN serves the storage; `None` otherwise. |

A `StoredFile` has `name` and `size`. `read(size=-1)` returns up to `size`
more bytes, or the rest, and `b""` at the end. `chunks(chunk_size)`
yields the rest, 64 KiB at a time by default, and closes the file at the
end.

### What a storage promises

- **Names are made, never given.** A name is 32 random hex digits (128
  bits), in the directory you pass, with the extension you pass.
  Directories have up to 8 segments, and segments and extensions are
  plain (letters, digits, `_`, `-`). So no name can climb out of the
  storage, hide a file (`.htaccess`), or overwrite another. A malformed
  directory raises `ValueError: A storage directory is up to 8 segments
  of letters, digits, _ and -.`
- **A name that could not have been made reads as missing.**
  `storage.open("../etc/passwd")` raises `FileMissing: No file has that
  name.`, which is a 404 `file_not_found` over HTTP.
- **Files appear whole.** `FileSystemStorage` writes to a hidden
  temporary file, syncs it, and renames it into place. So readers see a
  whole file or none, even after a crash. A failed or cancelled save
  leaves nothing.
- **No symbolic links.** Every directory of a path is opened from the
  storage's root without following links, and so is the file. So a link
  planted inside the storage cannot lead a read, a write, or a deletion
  elsewhere. Directories and FIFOs under a file's name are not files.
- **Private by default.** Files get mode `0600` and directories `0700`
  (less the umask): only the service's user can read them. Set
  `file_mode` and `directory_mode` when another user (a web server) must
  read them.

`FileSystemStorage` needs `O_NOFOLLOW` and `dir_fd`: Linux, macOS, and
the BSDs. Its blocking calls run in worker threads. Its options are
`location` (an absolute directory), `base_url` (where a web server serves
it, if one does), `file_mode` (`0o600`), and `directory_mode` (`0o700`).

`MemoryStorage` keeps files in the process, for tests. Staging and
production refuse it. `storages.reset()` forgets the storages made, and
their files; the test plugin calls it before every test. `storages` is
the process's `StorageHandler`; an alias that `STORAGES` does not have
raises `ImproperlyConfigured`.

### A storage of your own

To keep files somewhere else (a database, another service), subclass
`domainkit.storage.Storage` and implement five methods. Names reach them
checked:

```python
from collections.abc import AsyncIterator

from domainkit.storage import FileMissing, Storage, StoredFile


class BytesFile(StoredFile):
    def __init__(self, name: str, data: bytes) -> None:
        super().__init__(name, len(data))
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        taken = self._data if size < 0 else self._data[:size]
        self._data = self._data[len(taken) :]
        return taken

    async def close(self) -> None:
        self._data = b""


class DictStorage(Storage):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def _write(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        self.files[name] = b"".join([chunk async for chunk in chunks])

    async def _open(self, name: str) -> StoredFile:
        if name not in self.files:
            raise FileMissing
        return BytesFile(name, self.files[name])

    async def _delete(self, name: str) -> bool:
        return self.files.pop(name, None) is not None

    async def _exists(self, name: str) -> bool:
        return name in self.files

    async def _size(self, name: str) -> int:
        if name not in self.files:
            raise FileMissing
        return len(self.files[name])
```

`_write` must store the file whole or not at all: if `chunks` raises
(the file was too large, or the scanner found something), keep nothing.
For an object store, subclass `domainkit.storage.remote.ObjectStorage`
instead, and speak only its protocol.

## Object storage

Three storages keep files in an object store. They call it through
`domainkit.http.client` (pooled, retried, limited by the request's
deadline), with no cloud SDK. Install the `cloud` extra.

| Backend | Stores | Credentials |
| --- | --- | --- |
| `domainkit.storage.s3.S3Storage` | AWS S3, and with `endpoint` the stores that speak its API: MinIO (`path_style=True`), Cloudflare R2 (`region="auto"`), Huawei OBS, Alibaba OSS | `access_key_id` and `secret_access_key`, else the environment as AWS's tools read it (variables, EKS's web identity, the container's) |
| `domainkit.storage.gcs.GCSStorage` | Google Cloud Storage | `credentials_file` (a service account's key), else the metadata server (GKE, Compute Engine) |
| `domainkit.storage.azure.AzureBlobStorage` | Azure Blob storage | `account_key` (Shared Key), or `credential`: an Entra ID application or the host's managed identity (`domainkit.cloud.azure`) |

```python
STORAGES = {
    "default": {
        "BACKEND": "domainkit.storage.s3.S3Storage",
        "OPTIONS": {"bucket": "acme-files", "region": "eu-west-1", "prefix": "media/"},
    },
}
```

The options of each backend:

| Backend | Options |
| --- | --- |
| `S3Storage` | `bucket`, `region` (both required), `endpoint`, `path_style`, `access_key_id`, `secret_access_key`, `credentials` (an async callable that returns them), `prefix`, `public_url`, `timeout` |
| `GCSStorage` | `bucket` (required), `credentials_file`, `account`, `endpoint`, `prefix`, `public_url`, `timeout` |
| `AzureBlobStorage` | `account`, `container` (both required), `account_key`, `credential`, `endpoint`, `prefix`, `public_url`, `timeout` |

- `prefix` is a folder of the bucket that every name goes under. It ends
  in `/` and cannot contain `..`.
- `public_url` is where a CDN serves the files. `url(name)` then returns
  `<public_url>/<prefix><name>`.
- A file of up to 8 MiB is one request. A larger one is a multipart
  upload (S3), a resumable upload (GCS), or blocks committed by a list
  (Azure). If the content fails, the upload is cancelled: files appear
  whole or not at all, as on the file system.
- A file is read 4 MiB at a time, one ranged request each, so a large
  file never sits in memory.

### Presigned URLs

A presigned URL lets a client fetch or send a file without the file
passing through the application:

```python
from datetime import timedelta

url = await storage.presigned_url(name, expires=timedelta(minutes=5))

upload = await storage.presigned_upload(
    size=1_048_576, content_type="image/png", directory="avatars", extension=".png"
)
# Give the client upload.url, upload.method, and upload.headers; keep upload.name.
```

`presigned_upload` returns a `PresignedUpload`. The client sends
`upload.method` (`PUT`) to `upload.url`, with `upload.headers` and the
file as the body. Keep `upload.name`: the file has that name once the
client is done.

- **Expiry.** A presigned URL works for 1 second to 7 days (15 minutes by
  default). Outside that range, `ValueError`.
- **Size and type.** On S3 and GCS, an upload's URL signs its size and
  its type, so the store refuses anything else. Azure's SAS cannot bind
  them, so check `await storage.size(upload.name)` once the client says
  it is done.
- **Create only.** An upload's URL only creates its name. S3 is sent
  `If-None-Match: *` and GCS `x-goog-if-generation-match: 0`, both signed,
  and Azure's SAS grants create only (`c`). So once the file is there and
  checked, the same URL cannot replace it.
- **Signing keys.** GCS signs with the service account's key (the
  metadata server cannot sign), and Azure with the account key. Without
  one, Azure raises `CloudAuthError: A SAS needs the storage account's
  key.`

!!! warning
    A presigned upload never passes through the application. So nothing
    finds its type from its bytes or scans it, as `save_upload` does.
    Before the application trusts the file, read it back and check it, or
    upload through the application instead.

## Receiving uploads

`save_upload(upload, *, types, max_size, storage="default", directory="")`
stores an `UploadFile` if it is one of `types` and at most `max_size`
bytes. It returns a `StoredUpload` with `name`, `content_type`, `size`,
and `filename`. `storage=` takes an alias or a `Storage`.

These are the refusals, as the client sees them:

```text
415 unsupported_file_type  This type of file is not accepted.
400 empty_file             The file is empty.
413 file_too_large         The file is too large.
422 infected_file          The virus scan refused the file.
503 scanner_unavailable    Uploads cannot be checked now; try again later.
```

- **Types come from the content.** The first bytes of the file decide its
  type, never its name or the `Content-Type` that the client sent. A
  script called `photo.png` is refused, and a PNG called `page.html` is
  stored as a PNG, with `.png`. A type that the route does not list gets
  415 `unsupported_file_type`.
- **The ready types.** `PNG`, `JPEG`, `GIF`, `WEBP`, `PDF`, `ZIP`, and
  `TEXT`; `IMAGES` is `(PNG, JPEG, GIF, WEBP)`. SVG is not offered: it can
  carry scripts. `ZIP` covers Office documents, which are zip files.
  `TEXT` is UTF-8 without control characters (but tabs and line breaks),
  and covers CSV.
- **Your own types.** Describe another type with
  `FileType(content_type, extension, matches)`, where `matches` reads the
  first 512 bytes:

  ```python
  from domainkit.http.uploads import FileType

  MP3 = FileType("audio/mpeg", ".mp3", lambda head: head.startswith(b"ID3"))
  ```

- **Size is checked while the file streams** into the storage. Past
  `max_size`, the upload gets 413 `file_too_large`, and nothing is kept.
  A file whose size is already known to be larger (FastAPI has read the
  form before the route runs) is refused before it is copied. An empty
  file gets 400 `empty_file`.
- **The whole request** is also held to `MAX_REQUEST_BODY_SIZE` (1 MiB by
  default), as it arrives, before the route runs. Give an upload route a
  limit of its own instead of raising that one for every route
  ([Body limits per route](#body-limits-per-route)).
- **The client's file name** is cleaned with `sanitize_filename`, to show
  people and to suggest when downloading. Only its last path segment is
  kept. Control and formatting characters (right-to-left overrides, which
  make `gnp.exe` look like `exe.png`) and characters that Windows refuses
  are dropped. Names that Windows reserves (`CON`, `LPT1`) get a `_` in
  front. The name is cut to 255 bytes, keeping its extension. Never use
  it as a path:

  ```python
  from domainkit.http.uploads import sanitize_filename

  sanitize_filename("../../photo‮gnp.exe")  # 'photognp.exe'
  sanitize_filename("CON.txt")  # '_CON.txt'
  sanitize_filename("a:b?.pdf")  # 'ab.pdf'
  sanitize_filename(None)  # 'file'
  ```

- The messages of these refusals are translated, as every error is
  ([i18n.md](i18n.md)).
- `save_upload` raises `ValueError` when `types` is empty or `directory`
  is malformed: that is a mistake in your code, not the client's.

### Body limits per route

FastAPI reads the whole form, spooling files to disk, before the route
runs. So `max_size` alone acts after the bytes arrived. `body_limit`
gives the route a limit that holds while they arrive, above or below
`MAX_REQUEST_BODY_SIZE`, so the JSON routes keep the global one:

```python
from domainkit.http.middleware import body_limit


@router.post("/avatars", dependencies=[Depends(Require())])
@body_limit(2_100_000)  # the file's 2 MB, and the form around it
async def upload_avatar(file: UploadFile) -> DocumentOut:
    stored = await save_upload(file, types=IMAGES, max_size=2_000_000)
    ...
```

A request of 2,200,179 bytes to this route gets 413
`content_too_large` before a byte of it is read.

- `RequestBodyLimitMiddleware` holds the route's requests to the limit
  once the router has matched them. A `Content-Length` over it is refused
  with 413 before a byte is read. A body sent in chunks is refused as soon
  as it passes the limit, so a 20 MiB upload to a 100 kB route is read
  only to the limit and one chunk more.
- Middleware that reads bodies before routing (`IdempotencyMiddleware`)
  is held to the route's limit too: the route is matched as the router
  will match it, into mounted applications and included routers. Where a
  mount or a router takes the path but no route of it can be told yet,
  the strictest limit below it holds.
- Before routing, a declared length is checked against the largest limit
  of any route, or `MAX_REQUEST_BODY_SIZE` if that is larger. A route that
  never reads its body is not refused for a length that its own limit
  would refuse.
- Limits are 1 byte to 1 GiB (`MAX_ROUTE_BODY_SIZE`); others raise
  `ValueError`. The decorator marks the endpoint, so it may sit above or
  below the route's decorator. A route of a mounted application, or of a
  router nested without flattening (`FLATTEN_ROUTERS = False`), keeps its
  limit, read before routing or after.
- `ResourceRouter` takes `body_limits={"command": 16_384, "batch":
  2_000_000}`, per kind of route that takes a body (`create`, `update`,
  `command`, `batch`). `command_route` and `batch_route` take
  `body_limit=` per route ([resources.md](resources.md)).
- A small limit suits the routes that anonymous callers reach. A login or
  a sign-up takes a few hundred bytes, and does not need the global
  limit.

## Virus scanning

With `UPLOAD_SCANNER` set, `save_upload` streams every upload through
the scanner while the storage writes it:

```python
UPLOAD_SCANNER = {
    "BACKEND": "domainkit.storage.scanning.ClamAVScanner",
    "OPTIONS": {"host": "clamav", "port": 3310},
}
```

- The scanner gives its verdict before the last chunk is handed over. So
  an infected file makes the write fail, and no storage keeps it. The
  client gets 422 `infected_file`, and the log names what was found:
  `An upload was refused by the virus scan: Eicar-Test-Signature`.
- A scanner that cannot be reached, or that refuses the file (over
  clamd's size limit), refuses the upload with 503 `scanner_unavailable`.
  An upload is never kept unscanned.
- `ClamAVScanner` speaks clamd's `INSTREAM`. Its options are `host`
  (`localhost`), `port` (3310), and `timeout` (30 seconds for each step).
- `configured_scanner()` returns the scanner that `UPLOAD_SCANNER` names,
  made once per configuration, or `None`.

Another scanner subclasses `domainkit.storage.scanning.Scanner`. Its
`scan` passes the chunks on as they come, and raises `Infected` (or
`ScannerUnavailable`) before the last one ends. This one, for tests,
refuses any file with `EICAR` in a chunk:

```python
from collections.abc import AsyncIterator

from domainkit.storage.scanning import Infected, Scanner


class EicarScanner(Scanner):
    async def scan(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in chunks:
            if b"EICAR" in chunk:
                raise Infected("Eicar-Test-Signature")
            yield chunk
```

## Thumbnails

`thumbnail` in `domainkit.storage.images` stores a scaled copy of a stored
image. It needs the `images` extra (Pillow):

```python
from domainkit.storage.images import thumbnail

small = await thumbnail(storage, stored.name, size=(256, 256), format="webp")
print(small)
```

```text
thumbnails/ba0aecce691ec7f90c69233153d3d6a1.webp
```

- It fits the image in `size`, keeping its proportions and never
  enlarging it: a 1,200 by 800 image became 256 by 171.
- `format` is `webp`, `png`, or `jpeg`; sides are 1 to 4,096 pixels;
  `directory` is `thumbnails` by default. Other values raise `ValueError`.
- It runs in a thread.
- An image of more than 50 megapixels is refused from its header, before
  it is decoded, so a decompression bomb costs nothing. A stored file
  over 50 MiB is refused too, and so is a file that Pillow cannot read:
  `NotAnImage: That file is not an image that can be read.`
- Metadata (EXIF, with its location) is not copied.

## Serving files

Every route must say who may call it, and so must every way of serving a
file ([ADR 0021](adr/0021-check-mounted-applications.md)). Serve stored
files from a route with a rule, and check that the caller may read the
file that the name points to:

```python
from uuid import UUID

from fastapi.responses import StreamingResponse

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.ddd import PermissionDenied
from domainkit.http.uploads import serve


@router.get("/documents/{document_id}/file", dependencies=[Depends(Require())])
async def download(document_id: UUID, user: AuthenticatedUser) -> StreamingResponse:
    document = await documents.get(document_id)  # yours: who owns which file
    if document.owner_id != user.id:
        raise PermissionDenied("Not yours.")
    return await serve(
        "default",
        document.file_name,
        content_type=document.content_type,  # as save_upload found it
        filename=document.original_name,  # what the browser saves it as
    )
```

For a PDF with `filename="Invoice 7.pdf"`, the answer's headers are:

```text
content-type: application/pdf
content-length: 14
content-disposition: attachment; filename="Invoice 7.pdf"; filename*=UTF-8''Invoice%207.pdf
content-security-policy: default-src 'none'; sandbox
x-content-type-options: nosniff
```

- Files download (`Content-Disposition: attachment`) unless
  `inline=True`, which only images and PDFs may use; another type raises
  `ValueError`. Every answer says `Content-Security-Policy: default-src
  'none'; sandbox` and `X-Content-Type-Options: nosniff`, so a file cannot
  run scripts, load anything, or be read as another type. The name that
  the browser offers is cleaned, in ASCII for old clients and UTF-8 for
  the others.
- Keep the content type that `save_upload` found with the file's name,
  and pass it back to `serve`. Never serve a client's own claim.
- Find files by your own ids, not by names in the URL. A storage name
  cannot be guessed, but anyone who learns one could otherwise fetch it.
- A name that no file has raises `FileMissing`: 404.
- To let a web server or a CDN serve a directory itself, set `base_url`
  (then `storage.url(name)` gives each file's address). Remember that
  everyone who has the address can fetch the file. Mounting the directory
  in the application (`StaticFiles`) is refused by deny by default until
  you wrap it in `mark_public`, on purpose:

  ```python
  from fastapi.staticfiles import StaticFiles

  from domainkit.http.access import mark_public

  app.mount("/media", mark_public(StaticFiles(directory="/srv/public")))
  ```

  Do that only for files meant for everyone, never for uploads.

## Options

The settings; the [backends reference](reference/backends.md#storages)
has the full rows:

| Setting | Default | What it does |
| --- | --- | --- |
| `STORAGES` | `{}` | Storages by alias. Each has `BACKEND` (a `Storage` class, `FileSystemStorage` by default) and `OPTIONS` (its keyword arguments). |
| `UPLOAD_SCANNER` | `None` | The scanner `save_upload` streams every upload through: `BACKEND` (`ClamAVScanner` by default) and `OPTIONS`. `None`: no scan. |
| `MAX_REQUEST_BODY_SIZE` | 1 MiB | The largest request body of a route without `body_limit`. |

The arguments of the functions on this page:

| Name | Arguments |
| --- | --- |
| `save_upload` | `upload`, `types`, `max_size`, `storage="default"`, `directory=""` |
| `serve` | `storage`, `name`, `content_type`, `filename=None`, `inline=False` |
| `body_limit` | `size`: 1 byte to 1 GiB |
| `thumbnail` | `storage`, `name`, `size=(256, 256)`, `format="webp"`, `directory="thumbnails"` |
| `presigned_url` | `name`, `expires=timedelta(minutes=15)` |
| `presigned_upload` | `size`, `content_type`, `directory=""`, `extension=""`, `expires=timedelta(minutes=15)` |

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| 415 `unsupported_file_type` | The file's bytes are not one of `types`. | Send a file of an accepted type. |
| 400 `empty_file` | The file has no bytes. | Send the file. |
| 413 `file_too_large` | The file is over `max_size`. | Send a smaller file. |
| 413 `content_too_large` | The request is over the route's `body_limit`, or `MAX_REQUEST_BODY_SIZE`. | Send less, or raise the route's limit. |
| 422 `infected_file` | The scanner found something. | Nothing is kept. |
| 503 `scanner_unavailable` | The scanner could not scan the file. | Try again later; check clamd. |
| 404 `file_not_found` (`FileMissing`) | No file has the name, or the name is malformed. | Check the name you kept. |
| `FileTooLarge` | `save(..., max_size=...)` went over the limit. | Nothing is kept. |
| `StorageError` | A storage refused an operation; the base of the errors above. | Read its message. |
| `NotAnImage` | `thumbnail` got a file that is not an image, or is too large. | Check the file. |
| `ImproperlyConfigured` | `STORAGES` has no such alias, or its `BACKEND` or `OPTIONS` are wrong. | Fix the settings. |

## Testing

The test plugin forgets every storage before each test, so memory
storages start empty. Point the test settings at `MemoryStorage`:

```python
STORAGES = {"default": {"BACKEND": "domainkit.storage.MemoryStorage"}}
```

A test sends a file with the test client:

```python
from domainkit.testing import TestClient


def test_a_script_named_png_is_refused(client: TestClient) -> None:
    answer = client.post(
        "/documents",
        files={"file": ("photo.png", b"<script>alert(1)</script>", "image/png")},
    )
    assert answer.status_code == 415
    assert answer.json()["code"] == "unsupported_file_type"
```

More in [testing.md](testing.md).

## Common mistakes

- **Using the client's file name as a path.** Store under the name that
  the storage makes, and keep the cleaned `filename` only to show.
- **Trusting the client's `Content-Type`.** Keep the `content_type` that
  `save_upload` found, and pass it to `serve`.
- **Raising `MAX_REQUEST_BODY_SIZE` for one upload route.** Every JSON
  route then takes large bodies. Use `body_limit` on the upload route.
- **Trusting a presigned upload.** Nothing checked it. Read it back and
  check it before you use it.
- **Serving uploads with `StaticFiles`.** Everyone who learns a name can
  fetch the file. Serve uploads from a route that checks the caller.

## See also

- [Backends reference](reference/backends.md#storages): the `STORAGES`
  and `UPLOAD_SCANNER` rows.
- [Resources](resources.md): body limits for the routes of a
  `ResourceRouter`.
- [Security](security.md): deny by default, and what every answer
  carries.
- [Testing](testing.md): the test client, and the resets before each
  test.

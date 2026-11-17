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
- check what a PDF or an Office document holds before it is kept;
- take a file in a resource's route, a body a phone can sign;
- give an upload route its own body limit with `body_limit`;
- let a client send or fetch a file directly, with presigned URLs, and
  check that the store keeps them create only;
- scan uploads for viruses, over TLS where clamd is on another host, and
  make thumbnails of images;
- remove the files a committed change dropped, once no row keeps them.

The decisions are in
[ADR 0040](adr/0040-store-files-under-random-names.md),
[ADR 0167](adr/0167-check-an-upload-whole-and-let-a-resource-take-one.md) and
[ADR 0169](adr/0169-granians-http-versions-failed-starts-fcm-dry-runs-write-once-probes-clamd-over-tls-and-dropped-files.md).

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
| `domainkit.storage.s3.S3Storage` | AWS S3, and with `endpoint` the stores that speak its API: Cloudflare R2 (`region="auto"`), SeaweedFS, RustFS, Versity's gateway (usually `path_style=True`), Huawei OBS, Alibaba OSS. MinIO speaks it too, but its community edition is no longer maintained, and is published as source only | `access_key_id` and `secret_access_key`, else the environment as AWS's tools read it (variables, EKS's web identity, the container's) |
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
| `S3Storage` | `bucket`, `region` (both required), `endpoint`, `path_style`, `access_key_id`, `secret_access_key`, `credentials` (an async callable that returns them), `prefix`, `public_url`, `timeout`, `check_write_once` ([below](#a-store-that-ignores-create-only)) |
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
  checked, the same URL cannot replace it, where the store honours the
  header ([below](#a-store-that-ignores-create-only)).
- **Signing keys.** GCS signs with the service account's key (the
  metadata server cannot sign), and Azure with the account key. Without
  one, Azure raises `CloudAuthError: A SAS needs the storage account's
  key.`

!!! warning
    A presigned upload never passes through the application. So nothing
    finds its type from its bytes or scans it, as `save_upload` does.
    Before the application trusts the file, read it back and check it, or
    upload through the application instead.

### A store that ignores create only

*New in 2.5.* Some stores that speak S3's API ignore `If-None-Match` on
`PutObject`, without an error: a second `PUT` to the URL then replaces
the file the application checked, and nothing says so.

| Store | `If-None-Match: *` on `PutObject` |
| --- | --- |
| AWS S3 | refuses a second write with `412` (since August 2024) |
| Cloudflare R2 | refuses it with `412` |
| SeaweedFS, RustFS, Versity's gateway, MinIO | read the header on `PutObject` |
| Garage 2.5, Scality CloudServer 9.5 | read no condition on `PutObject`: the file is replaced |
| Huawei OBS, Alibaba OSS, GCS's interoperability API | not known: probe them |

`S3Storage(check_write_once=True)` asks the store before the first
presigned upload of each storage, in each process, once however many
uploads ask at once: it writes a probe object under the prefix
(`write-once-probes/`) twice, expects the second write refused (`412`,
or `409`), and deletes it. Its verdict is kept. Where the store took the
second write, `presigned_upload` raises `WriteOnceIgnored`, a
`StorageError` that the HTTP layer answers `503
direct_upload_unavailable`, so the client uploads through the API
instead:

```text
domainkit.storage.s3.WriteOnceIgnored: Write-once probe: the S3 store at http://garage:3900 replaced an object written with If-None-Match: *. It ignores the header, so a presigned upload's URL could replace the file it made: use a store that honours it, or upload through the application.
```

A probe that cannot tell (the store refused the first write, or was
unreachable) raises a `StorageError` that starts `Write-once probe:`,
and is made again at the next upload.

```python
STORAGES = {
    "default": {
        "BACKEND": "domainkit.storage.s3.S3Storage",
        "OPTIONS": {
            "bucket": "files",
            "region": "us-east-1",
            "endpoint": "http://seaweedfs:8333",
            "path_style": True,
            "check_write_once": True,
        },
    },
}
```

The probe needs `PutObject` and `DeleteObject` on the prefix; a probe the
store will not delete stays, logged, and the answer stands. Without
`check_write_once`, as before, `presigned_upload` sends no request. A
deployment job can ask once instead: `await storage.probe_write_once()`
raises the same errors.

## Receiving uploads

`save_upload(upload, *, types, max_size, storage="default", directory="",
filename=None)` stores an upload if it is one of `types` and at most
`max_size` bytes. It returns a `StoredUpload` with `name`,
`content_type`, `size`, `filename`, and `sha256` (the hex SHA-256 of what
was stored, *new in 2.5*). `storage=` takes an alias or a `Storage`.
`StoredUpload` is in `domainkit.storage`, so a command of the application
layer may hold one.

These are the refusals, as the client sees them:

```text
415 unsupported_file_type  This type of file is not accepted.
400 empty_file             The file is empty.
413 file_too_large         The file is too large.
422 unsafe_file            Files with macros are not accepted.
422 unreadable_file        The file could not be read.
422 infected_file          The virus scan refused the file.
503 scanner_unavailable    Uploads cannot be checked now; try again later.
503 service_busy           Uploads cannot be checked now; try again later.
```

- **Types come from the content.** The first bytes of the file decide its
  type, never its name or the `Content-Type` that the client sent. A
  script called `photo.png` is refused, and a PNG called `page.html` is
  stored as a PNG, with `.png`. A type that the route does not list gets
  415 `unsupported_file_type`.
- **The ready types.** `PNG`, `JPEG`, `GIF`, `WEBP`, `PDF`, `ZIP`, and
  `TEXT`; `IMAGES` is `(PNG, JPEG, GIF, WEBP)`. SVG is not offered: it can
  carry scripts. `TEXT` is UTF-8 without control characters (but tabs and
  line breaks), and covers CSV. These read the first bytes only. For
  documents, take the checked types, which read the whole file:
  `CHECKED_PDF`, `DOCX`, `XLSX`, `PPTX`, and `OFFICE` for the three
  ([Checking documents](#checking-documents)). `ZIP` takes any zip file,
  an Office document with macros included.
- **Your own types.** Describe another type with
  `FileType(content_type, extension, matches, inspect=None)`, where
  `matches` reads the first 512 bytes:

  ```python
  from domainkit.http.uploads import FileType

  MP3 = FileType("audio/mpeg", ".mp3", lambda head: head.startswith(b"ID3"))
  ```

  `inspect` reads the whole file ([Checks of your own](#checks-of-your-own)).

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

### Bytes and streams

*New in 2.5.* `save_upload` takes the file as a form's `UploadFile`, as
bytes, or as an async iterable of bytes, such as a request's own body.
`filename=` is the client's name for it; it replaces a form file's own:

```python
from urllib.parse import unquote
from uuid import UUID

from fastapi import Depends, Request

from domainkit.http.access import Require
from domainkit.http.middleware import body_limit
from domainkit.http.uploads import CHECKED_PDF, OFFICE, save_upload


@router.put("/documents/{document_id}/file", dependencies=[Depends(Require())])
@body_limit(10_100_000)
async def put_file(request: Request, document_id: UUID) -> DocumentOut:
    stored = await save_upload(
        request.stream(),
        types=(CHECKED_PDF, *OFFICE),
        max_size=10_000_000,
        filename=unquote(request.headers.get("X-Filename", "")),
    )
    ...
```

A phone that signs the requests it sends (`DeviceSigned`) signs the
body's bytes. It cannot know the bytes of a form before the platform
encodes it, so it sends the file as the body, and its name in a header.
A rule that read the body first leaves it whole for the route. A
`ResourceRouter` does this for you with `upload_route`
([resources.md](resources.md#files)).

`receive_upload(upload, *, types, max_size, filename=None)` receives the
file and checks it as `save_upload` does, but stores nothing. It returns
a `ReceivedUpload`, held in a temporary file: `store(storage,
directory=)` stores it, drops the temporary file, and returns the
`StoredUpload`; `unstored()` says what `store` would return but its
name; and `close()` drops it without storing. Store it, or close it.

A gateway of the infrastructure layer may call `save_upload` with the
bytes its route hands it: the [layer rules](reference/settings.md#layer-rules)
allow `domainkit.http.uploads` there since 2.5. They keep Starlette's
`UploadFile` and `domainkit.http.errors` out, so a gateway raises domain
errors, which the HTTP layer maps. A gateway that takes the route's file
as it is, without reading it into memory first, types it as `Upload`,
from the same module: a form's file, bytes, or a stream, as `save_upload`
and `receive_upload` take them (since 2.7). It may catch `UploadRefused`
from there too.

```python
# billing/infrastructure/files.py
from domainkit.http.uploads import CHECKED_PDF, Upload, save_upload


class ContractFiles:
    async def keep(self, upload: Upload) -> StoredUpload:
        return await save_upload(upload, types=[CHECKED_PDF], max_size=10_000_000)
```

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
  `body_limit=` per route ([resources.md](resources.md)). `upload_route`
  holds its body to its `max_size` and 64 KiB for a form, unless it is
  given a `body_limit`.
- A small limit suits the routes that anonymous callers reach. A login or
  a sign-up takes a few hundred bytes, and does not need the global
  limit.

## Checking documents

*New in 2.5.* A PDF or a Word document starts with the same bytes
whether it holds a script or not. The checked types read the whole file
before it is stored, and refuse what runs, fetches, or hides something
when the file is opened or converted. They need the `documents` extra
(pypdf and lxml):

```bash
pip install "domainkit[documents]"
```

```python
from domainkit.http.uploads import CHECKED_PDF, OFFICE, save_upload

stored = await save_upload(file, types=(CHECKED_PDF, *OFFICE), max_size=10_000_000)
```

A Word document with macros, sent as `contract.docx`:

```json
{"detail": "Files with macros are not accepted.", "code": "unsafe_file", "request_id": "…"}
```

- **When.** The upload is held in a temporary file (in memory up to
  1 MiB, then on disk), the check runs in a process of its own, and only
  a file that passes is stored, through the virus scan if one is set. A
  file a check refuses is never stored: 422 `unsafe_file`, or
  `unreadable_file` for a file its reader cannot read. The detail says
  why, in the caller's language.
- **In a process of its own.** A parser of untrusted files cannot be
  bounded from inside the process that runs it: one call of pypdf's can
  take a minute, or a gigabyte, before it returns. So each upload's check
  runs in a new Python process (`python -I`), handed the file over a
  pipe, with the system's limits (`UPLOAD_CHECKS`): `MEMORY` of address
  space (512 MiB), and `SECONDS` of CPU (15). Past `SECONDS` of wall
  clock it is killed. A check stopped by a limit refuses the file, 422
  `unreadable_file`: "The file took too long to check." or "The file
  needed too much memory to check." A process starts in about a tenth of
  a second, with pypdf or lxml loaded: that is what each checked upload
  costs more. Windows has no such limits: there, the wall clock alone
  stops a check; macOS does not hold a process to `MEMORY`.
- **Not a sandbox.** The process bounds memory, CPU and time, and keeps
  the application's secrets out of a parser's reach: it gets none of
  the environment but `PATH`, the locale and the temporary directory,
  and a session of its own. It still runs as the application's user,
  with the same files and network. On Linux it is the first the
  out-of-memory killer ends.
- **When a process ends badly.** A process ended by another signal (a
  crash, or a `SIGKILL` no one can tell from the out-of-memory killer's)
  refuses the file, 422 `unreadable_file` ("The file could not be
  read."), and is logged at `WARNING` (`domainkit.inspection`) with the
  signal's name. One that exits without an answer (an import that
  failed), or whose check raised anything else, is a 500, logged with
  the last 4 KiB it wrote to standard error.
- **A few at a time.** At most `PROCESSES` (4) checks run at once in a
  process of the application, on any of its event loops. One more upload
  is refused at once, 503 `service_busy` ("Uploads cannot be checked
  now; try again later."), not queued. A slot is freed only once its
  check's process has exited: a request cancelled meanwhile (by its
  deadline, `REQUEST_TIMEOUT`) kills it first. A client that goes away
  does not cancel its request under Granian or uvicorn: the check runs
  on, `SECONDS` at most. A type without a check takes no slot.
- **Memory to give.** `MEMORY` must exceed the largest `max_size` a
  checked type takes: the process holds the file whole, and Python with
  pypdf or lxml takes about 100 MiB of address space before it reads a
  byte. Each worker runs up to `PROCESSES` checks: workers times
  `PROCESSES` times `MEMORY` must fit the host or the container.
- **`CHECKED_PDF`** (`PdfCheck`) reads a PDF as a strict reader does,
  and only as its cross-reference tables say: an object they do not
  declare, or one not at its offset, makes the file `unreadable_file`,
  where pypdf would search the file for it. It reads every object the
  tables declare, follows every reference from the trailer, and parses
  every object written in the file that they do not point at (an earlier
  revision). It refuses an encrypted PDF (an owner password alone
  included), one of more than 1,000 pages, JavaScript anywhere (a 3D
  annotation's included), an action run on opening or on an event
  (`/OpenAction` with an action, `/AA`; a destination is kept), an action
  that launches a program, opens another file or sends data, a link to
  anything but `http`, `https` or `mailto`, media from a URL, an embedded
  file, rich media, and an XFA form. Names are decoded (`/J#61vaScript`
  is `/JavaScript`).
- **`DOCX`, `XLSX`, `PPTX`** (`OfficeCheck`) refuse a document whose main
  part is macro-enabled (a `.docm` named `.docx`), macros (`vbaProject.bin`,
  Excel 4.0 macro sheets), OLE objects, ActiveX controls, chunks imported
  from another format (`altChunk`), pictures or objects linked rather
  than held, a relationship to outside the package but a hyperlink to
  `http`, `https` or `mailto` (a remote template, a linked picture),
  whatever its `TargetMode` says, a Word field that pulls content in or
  runs a program (`INCLUDEPICTURE`, `INCLUDETEXT`, `INCLUDE`, `IMPORT`,
  `LINK`, `DATABASE`, `DDE`, `DDEAUTO`) or whose instruction starts with
  another field, OLE objects and controls written in the XML itself
  (`w:object`, `o:OLEObject`, `w:control`), a picture or an object that
  names a hyperlink's relationship, Excel's links to other workbooks,
  data connections, query tables, formulas anywhere in a sheet (cells,
  names, conditional formats, validations, tables, charts) that call
  `WEBSERVICE`, `RTD`, `IMAGE` or DDE (`cmd|'...'!A0`), and an XML part
  with a DTD or an entity, or in another encoding than UTF-8 and UTF-16.
  A part is XML by its name (`.xml`, `.rels`, `.vml`) or its content
  type (`+xml`), never by its first bytes; white space before its markup
  is passed over, however long, and a part that is not XML after all is
  `unreadable_file`. An embedded workbook (a
  chart's data) is checked the same way. A template, or another kind of
  document, is not the type: with `OFFICE` the next type is tried, else
  415. Power Query reaches its data through a connection, which is
  refused; a project that takes workbooks with external data writes a
  check of its own.
- **Bounds inside the check.** A second layer, under the process's: a
  ZIP is read from its directory before a byte is inflated: at most 2,000
  parts, 50 times the upload's size inflated in all and never more than
  200 MiB, and no part over 1 MiB inflating more than 200 times. A PDF's
  streams decode to 16 MiB each and its object streams to 64 MiB in all;
  its tables declare at most 200,000 objects; the check reads at most a
  million values; a PDF pypdf read only in part is refused. Each check
  stops at its own deadline, 10 seconds, looked at per object or part.
  XML is parsed without entities, a DTD, or the network.
- **Limits of your own.** `FileType(DOCX.content_type, ".docx",
  DOCX.matches, OfficeCheck("docx", max_expansion=100))`, or
  `PdfCheck(max_pages=50, max_seconds=5)`.
- `PDF` and `ZIP` read the first bytes, as before 2.5. A route that keeps
  them takes PDFs with scripts and Word documents with macros.
- Without pypdf or lxml, a check raises `ImproperlyConfigured`, naming
  the extra: a 500, "A check of an upload, in its process: Checking PDF
  and Office uploads needs pypdf: ...", logged at `ERROR`
  (`domainkit.inspection`) with where the check raised it.

The checks refuse what is known to be used against readers and
converters. They do not make a file harmless: keep the virus scan, serve
files with `serve`, and convert them in a sandbox.

### Checks of your own

A `FileType`'s `inspect` reads the whole file, from its start, in a
process of its own, which loads it by name: it must be a function at
the top level of a module the application imports, or an object of a
class that is (a lambda, or a check defined in `__main__`, raises
`TypeError` when the type is made). It raises `FileRefused(message,
code="unsafe_file")` to refuse the file (422 with `message`, which is
translated if the catalogs have it), and `NotOfType` when the file is
not of the type after all, so that the next type that matches is tried.
The process has no settings and none of the application's environment:
a check takes what it needs as arguments of its own (an object's
fields). What it prints goes to the process's standard error, which is
logged only when the check fails. Keep its module cheap to import: the
process imports it for each upload.

```python
from typing import IO

from domainkit.http.uploads import TEXT, FileType
from domainkit.i18n.lazy import ngettext_lazy
from domainkit.inspection import FileRefused


def no_formulas(file: IO[bytes]) -> None:
    if found := file.read().count(b"=cmd|"):
        raise FileRefused(
            ngettext_lazy(
                "The file holds a command.", "The file holds commands.", found
            )
        )


CSV = FileType("text/csv", ".csv", TEXT.matches, no_formulas)
```

A check's `message` is text, translated where it is shown
(`gettext_noop` marks it for the catalogs), or a lazy string of
`gettext_lazy`, `ngettext_lazy` or `pgettext_lazy` (since 2.6; 2.5
answered it 500). Making a lazy string text reads the settings, which
the process does not have: it sends the lookup and its arguments
instead (`LazyString.source`), and the application makes the string
again. The refusal is answered in the caller's language, as a domain
error's lazy message is, and logged in English, as a plain one is:
`An upload was refused by its check: ...` (`domainkit.http.uploads`, at
`INFO`). `ngettext_lazy` picks the plural form for its count, and fills
nothing in.

A text filled with values (`gettext_lazy(...).format(...)`), or a
call to `gettext`, is made at once, in the process, and reads the
settings it does not have: a 500, `ImproperlyConfigured` "A check of an
upload, in its process: Set the DOMAINKIT_SETTINGS_MODULE environment
variable.", logged at `ERROR` (`domainkit.inspection`) with the check's
traceback.

A message is one line of UTF-8, at most 500 characters: a line break or
a tab becomes a space, and another control or format character (a
right-to-left override) is dropped, so that the log line is the
refusal's alone. A message that is not UTF-8 (a file's bytes decoded
with `surrogateescape`) refuses the file as one that could not be read,
422 `unreadable_file`.

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
  (`localhost`), `port` (3310), and `timeout` (30 seconds for each step),
  and those of TLS, below.
- `configured_scanner()` returns the scanner that `UPLOAD_SCANNER` names,
  made once per configuration, or `None`.

### clamd on another host

clamd speaks no TLS, and asks no one who they are: on another host,
every upload crosses the network in clear, and anyone who reaches its
port can use it. In staging and production, `check` warns
(`security.W007`) when `UPLOAD_SCANNER` reaches clamd on a host other
than this one (`localhost`, `127.0.0.1`, `::1`) without TLS, through
`ClamAVScanner` or a subclass of it.

*New in 2.5.* Put a proxy that ends TLS in front of clamd, such as
stunnel, and turn TLS on in the scanner. It checks the proxy's
certificate, which must name `host`, against the system's CAs or
`tls_ca`, and can show a certificate of its own (`tls_cert`, `tls_key`,
and `tls_key_password` if the key is encrypted) for the proxy to
require, which clamd itself cannot:

```python
UPLOAD_SCANNER = {
    "BACKEND": "domainkit.storage.scanning.ClamAVScanner",
    "OPTIONS": {
        "host": "clamav",
        "port": 3311,
        "tls": True,
        "tls_ca": "/run/secrets/clamav-ca.pem",
        "tls_cert": "/run/secrets/api.pem",
        "tls_key": "/run/secrets/api.key",
    },
}
```

And on clamd's host, stunnel in server mode, in front of clamd on
`127.0.0.1:3310` (`TCPAddr 127.0.0.1` in `clamd.conf`):

```ini
[clamd]
accept = 3311
connect = 127.0.0.1:3310
cert = /etc/stunnel/clamav.pem
key = /etc/stunnel/clamav.key
CAfile = /etc/stunnel/api-ca.pem
verifyChain = yes
```

The scanner speaks TLS 1.2 or later. A certificate it cannot trust, or
a proxy that refuses its own, makes the upload fail as an unreachable
scanner does (503 `scanner_unavailable`). A TLS file without `tls`, a
certificate without its key, an encrypted key without its password, or
a file TLS cannot read is refused when the scanner is made
(`ImproperlyConfigured`). Where clamd runs on a
network only the service reaches, add `security.W007` to
`SILENCED_CHECKS` instead.

### A scanner of your own

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

## Removing files a change dropped

*New in 2.5.* A storage never deletes a file by itself. When a change
replaces an aggregate's file, or deletes the aggregate, the old file
stays, unless something removes it. Not in the use case: its
transaction may still roll back, and another row may hold the same name.
Defer the removal, to run once the change has committed:

```python
from datetime import timedelta

from domainkit.storage.cleanup import remove_unreferenced


async def replace_file(uow: DocumentsUnitOfWork, document_id: str, name: str) -> None:
    document = await uow.documents.get(document_id)
    uow.defer(
        remove_unreferenced,
        names=[document.file],  # None, where there was none, is passed over
        delay=timedelta(minutes=5),
    )  # storage="default"
    document.file = name
    await uow.commit()
```

And, beside the tables, in the infrastructure layer, declare each column
that holds names of a storage's files, one name per row. In the
framework's layout, where `table_for` makes the tables and
`start_mappers()` maps the entities later, declare the tables' own
columns, a link table's with no class included: the entities'
attributes are no columns until they are mapped.

```python
from domainkit.db import table_for
from domainkit.storage.cleanup import keeps_files

documents = table_for(Document, "documents", domain="documents")
keeps_files(documents.c.file, attachments.c.file)  # storage="default"
```

With declarative models, the mapped attributes will do:
`keeps_files(DocumentRow.file, TemplateRow.preview)`.

- The worker runs it after the commit, and again after a failure (it is
  a task of `domainkit.contrib.messaging`, registered as
  `domainkit.storage.remove_unreferenced`). A change that rolls back
  queues nothing, so its file stays.
- Before it deletes a name, it asks each declared column whether a row
  holds it, on the primary database of the column's table (its domain's,
  by `table_for(domain=...)` or `info={"domain": ...}`; untagged, its
  mapped class's domain, else `default`), never a replica that may not
  have seen the commit. A name a row still holds stays; one already
  gone, and an empty one (`None`), are passed over.
- With no column declared for the storage, it refuses
  (`ImproperlyConfigured`) and deletes nothing.
- A transaction still open as it reads, which copies the name into a
  row, is not seen: copy the file (`save` it again) rather than its name.
  A `delay` on the deferral, longer than any request takes, narrows the
  window further: a request that read the old name has committed, or
  failed, by then.
- Without messaging, call `await remove_unreferenced([...])` yourself
  once the commit is done; nothing then runs it again after a failure.

`forget_files()` forgets the declared columns (tests). Like
django-cleanup, this removes a file once the change commits; unlike it,
it checks the other rows first, as Active Storage's attachments keep a
blob that another record holds.

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
| `UPLOAD_CHECKS` | `PROCESSES` 4, `MEMORY` 512 MiB, `SECONDS` 15 | How the checks of uploaded files run, each in a process of its own ([Checking documents](#checking-documents)). |
| `MAX_REQUEST_BODY_SIZE` | 1 MiB | The largest request body of a route without `body_limit`. |

The arguments of the functions on this page:

| Name | Arguments |
| --- | --- |
| `save_upload` | `upload` (an `UploadFile`, bytes, or an async iterable of bytes), `types`, `max_size`, `storage="default"`, `directory=""`, `filename=None` |
| `receive_upload` | `upload`, `types`, `max_size`, `filename=None` |
| `PdfCheck` | `max_pages=1000`, `max_objects=200_000`, `max_decoded=64 MiB`, `max_seconds=10.0` |
| `OfficeCheck` | `kind` (`"docx"`, `"xlsx"`, `"pptx"`), `max_parts=2000`, `max_size=200 MiB`, `max_ratio=200`, `max_expansion=50`, `max_seconds=10.0` |
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
| 422 `unsafe_file` | The type's check refused what the file holds: scripts, actions, macros, objects, outside links, a DTD, a password, too many pages, or a ZIP too large to check. | Nothing is kept. Send the file without it. |
| 422 `unreadable_file` | The type's check could not read the file, or a strict reader could not (a PDF whose table needs repair), or its process took more time or memory than `UPLOAD_CHECKS` gives it, or a signal ended it (logged). | Nothing is kept. Send a file that opens; save a PDF again from a viewer. |
| 503 `service_busy` | `UPLOAD_CHECKS.PROCESSES` checks run already. | Try again shortly; raise `PROCESSES` if the host has room. |
| 422 `infected_file` | The scanner found something. | Nothing is kept. |
| `ImproperlyConfigured` | A checked type without pypdf or lxml. | Install the `documents` extra. |
| `RuntimeError` (500) | A check raised something else, or its process ended without an answer. | Read the log (`domainkit.inspection`): it holds the end of what the process wrote. |
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
- **Taking documents by their first bytes.** `PDF` and `ZIP` take a PDF
  that runs a script and a Word document with macros. Take `CHECKED_PDF`
  and `OFFICE`.
- **Serving uploads with `StaticFiles`.** Everyone who learns a name can
  fetch the file. Serve uploads from a route that checks the caller.

## See also

- [Backends reference](reference/backends.md#storages): the `STORAGES`
  and `UPLOAD_SCANNER` rows.
- [Documents](documents.md): filling uploaded Word templates, and laying
  text over uploaded PDFs.
- [Resources](resources.md): body limits for the routes of a
  `ResourceRouter`.
- [Security](security.md): deny by default, and what every answer
  carries.
- [Testing](testing.md): the test client, and the resets before each
  test.

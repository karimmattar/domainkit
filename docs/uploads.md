# Files and uploads

Uploaded files are the classic way into a web service: a file name that
climbs out of the upload directory, a "picture" that is really HTML with a
script in it, a file too large for the disk. domainkit stores files under
names it makes up, decides their type from their content, stops large ones
while they arrive, and serves them only from routes that say who may read
them. The decisions are in [ADR 0040](adr/0040-store-files-under-random-names.md).

## Storages

A storage keeps files under names it chooses. Configure them in `STORAGES`,
as in Django:

```python
STORAGES = {
    "default": {
        "BACKEND": "domainkit.storage.FileSystemStorage",
        "OPTIONS": {"location": "/srv/billing/files"},
    },
}
```

```python
from domainkit.storage import storages

storage = storages["default"]
name = await storage.save(pdf_bytes, directory="invoices", extension=".pdf")
# "invoices/3f2a9c...e1.pdf": keep it, in your table, to find the file again

async with await storage.open(name) as file:
    data = await file.read()  # or: async for chunk in file.chunks(): ...

await storage.size(name)  # bytes
await storage.exists(name)
await storage.delete(name)  # True if it existed
```

| Call | Does |
| --- | --- |
| `save(content, directory=, extension=, max_size=)` | Store bytes, or an async iterable of chunks, under a new name, and return it. Past `max_size` bytes it raises `FileTooLarge` and keeps nothing. |
| `open(name)` | A `StoredFile` to `read()` or iterate with `chunks()`; close it, or use `async with`. |
| `size(name)`, `exists(name)`, `delete(name)` | As they say. |
| `url(name)` | Where clients can fetch the file themselves, if a web server serves the storage; `None` otherwise. |

- **Names are made, never given.** A name is 32 random hex digits (128
  bits), in the directory you pass, with the extension you pass. Directories
  and extensions are plain (letters, digits, `_`, `-`), so no name can
  climb out of the storage, hide a file (`.htaccess`), or overwrite
  another. A name that could not have been made reads as missing:
  `FileMissing`, a 404 over HTTP.
- **Files appear whole.** `FileSystemStorage` writes to a hidden temporary
  file, syncs it, and renames it into place, so readers see a whole file
  or none, even after a crash. A failed or cancelled save leaves nothing.
- **No symbolic links.** Every directory of a path is opened from the
  storage's root without following links, and so is the file, so a link
  planted inside the storage cannot lead a read, a write, or a deletion
  elsewhere. Directories and FIFOs under a file's name are not files.
- **Private by default.** Files get mode `0600` and directories `0700`
  (less the umask): only the service's user can read them. Set
  `file_mode` and `directory_mode` when another user (a web server) must.
- `MemoryStorage` keeps files in the process, for tests. Staging and
  production refuse it. `storages.reset()` forgets the storages made, and
  their files, between tests.
- Write a storage of your own (a database, another service) by subclassing
  `domainkit.storage.Storage` and implementing its `_write`, `_open`,
  `_delete`, `_exists`, and `_size`; names reach them checked. For an
  object store, subclass `domainkit.storage.remote.ObjectStorage` and speak
  its protocol only.

`FileSystemStorage` needs `O_NOFOLLOW` and `dir_fd`: Linux, macOS, and the
BSDs. Its blocking calls run in worker threads.

## Object storage

Three storages keep files in an object store, through
`domainkit.http.client` (pooled, retried, bounded by the request's
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

Each takes `prefix` (a folder of the bucket every name goes under) and
`public_url` (where a CDN serves the files, which `url(name)` then
returns). A file of up to 8 MiB is one request; a larger one a multipart
upload (S3), a resumable upload (GCS), or blocks committed by a list
(Azure), which a failing content aborts: files appear whole or not at
all, as on the file system. A file is read 4 MiB at a time, one ranged
request each, so a large one never sits in memory.

### Presigned URLs

A client can fetch or send a file without it passing through the
application:

```python
url = await storage.presigned_url(name, expires=timedelta(minutes=5))

upload = await storage.presigned_upload(
    size=1_048_576, content_type="image/png", directory="avatars", extension=".png"
)
# Give the client upload.url, upload.method, and upload.headers; keep upload.name.
```

A presigned URL works for one second to seven days. An upload's URL signs
its size and its type on S3 and GCS, so it takes nothing else; Azure's
SAS cannot bind them, so check `size(name)` once the client says it is
done. An upload's URL only creates its name: S3 is sent `If-None-Match: *`
and GCS `x-goog-if-generation-match: 0`, both signed, and Azure's SAS
grants create only. So once the file is there and checked, the same URL
cannot replace it. GCS signs with the service account's key (the metadata
server cannot sign) and Azure with the account key.

A presigned upload never passes through the application, so nothing sniffs
its type or scans it, as `save_upload` does. Before the application trusts
the file, read it back and check it, or upload through the application
instead.

## Virus scanning

With `UPLOAD_SCANNER` set, `save_upload` streams every upload through the
scanner while the storage writes it:

```python
UPLOAD_SCANNER = {
    "BACKEND": "domainkit.storage.scanning.ClamAVScanner",
    "OPTIONS": {"host": "clamav", "port": 3310},
}
```

The scanner's verdict comes before the last chunk is handed over, so an
infected file makes the write fail and no storage keeps it: the client
gets 422 `infected_file`, and the log names what was found. A scanner that
cannot be reached, or that refuses the file (over clamd's size limit),
refuses the upload with 503 `scanner_unavailable`: an upload is never kept
unscanned. `ClamAVScanner` speaks clamd's `INSTREAM`; another scanner
subclasses `domainkit.storage.scanning.Scanner`.

## Thumbnails

`domainkit.storage.images.thumbnail` (the `images` extra, Pillow) stores a
scaled copy of a stored image:

```python
small = await thumbnail(storage, stored.name, size=(256, 256), format="webp")
```

It fits the image in `size`, keeping its proportions and never enlarging
it, as WebP, PNG, or JPEG, under `thumbnails/`. It runs in a thread. An
image of more than 50 megapixels is refused from its header before it is
decoded (a decompression bomb costs nothing), and so is a file Pillow
cannot read: `NotAnImage`. Metadata (EXIF, with its location) is not
copied.

## Receiving uploads

FastAPI reads uploads with `python-multipart`: install the `uploads` extra
(`pip install "domainkit[uploads]"`).

```python
from fastapi import Depends, UploadFile

from domainkit.http.access import Require
from domainkit.http.uploads import IMAGES, PDF, save_upload


@router.post("/documents", dependencies=[Depends(Require())])
async def upload_document(file: UploadFile) -> DocumentOut:
    stored = await save_upload(
        file,
        types=(*IMAGES, PDF),  # what this route accepts
        max_size=5_000_000,  # bytes
        directory="documents",
    )
    # stored.name: the storage's name; stored.content_type: from its bytes;
    # stored.size; stored.filename: the client's name, cleaned, to show people.
    ...
```

- **Types come from the content.** The first bytes of the file decide its
  type (`PNG`, `JPEG`, `GIF`, `WEBP`, `PDF`, `ZIP`, `TEXT`), never its name
  or the `Content-Type` the client sent: a script called `photo.png` is
  refused, and a PNG called `page.html` is stored as a PNG, with `.png`.
  A type the route does not list gets 415 `unsupported_file_type`. SVG is
  not offered: it can carry scripts. `ZIP` covers Office documents, which
  are zip files; `TEXT` covers CSV. Describe other types with
  `FileType(content_type, extension, matches)`, where `matches` reads the
  first 512 bytes.
- **Size is checked while the file streams** into the storage: past
  `max_size` the upload gets 413 `file_too_large` and nothing is kept. A
  file whose size is already known to be larger (FastAPI has read the form
  before the route runs) is refused before it is copied. An empty file
  gets 400 `empty_file`. The request as a whole is also held to
  `MAX_REQUEST_BODY_SIZE` (1 MiB by default), as it arrives, before the
  route runs: give an upload route a limit of its own rather than raising
  that one for every route ([Body limits per route](#body-limits-per-route)).
- **The client's file name** is cleaned with `sanitize_filename`, to show
  people and to suggest when downloading: only its last path segment is
  kept; control and formatting characters (right-to-left overrides, which
  make `gnp.exe` look like `exe.png`) and characters Windows refuses are
  dropped; names Windows reserves (`CON`, `LPT1`) get a `_` in front; and
  it is cut to 255 bytes, keeping its extension. Never use it as a path.
- The messages of these refusals are translated, as every error is
  ([i18n.md](i18n.md)).

### Body limits per route

FastAPI reads the whole form, spooling files to disk, before the route
runs, so `max_size` alone acts after the bytes arrived. `body_limit` gives
the route a limit that holds while they arrive, above or below
`MAX_REQUEST_BODY_SIZE`, so the JSON routes keep the global one:

```python
from domainkit.http.middleware import body_limit


@router.post("/avatars", dependencies=[Depends(Require())])
@body_limit(2_100_000)  # the file's 2 MB, and the form around it
async def upload_avatar(file: UploadFile) -> AvatarOut:
    stored = await save_upload(file, types=IMAGES, max_size=2_000_000)
    ...
```

- `RequestBodyLimitMiddleware` holds the route's requests to it once the
  router has matched them: a `Content-Length` over it is refused with 413
  before a byte is read, and a body sent in chunks as soon as it passes it,
  so a 20 MiB upload to a 100 kB route is read only to the limit and one
  chunk more. Middleware that reads bodies before routing
  (`IdempotencyMiddleware`) is held to the route's limit too: the route is
  matched as the router will match it, into mounted applications and
  routers kept included. Where a mount or router takes the path but no
  route of it can be told yet, the strictest limit below it holds.
- Before routing, a declared length is checked against the largest limit
  of any route, or `MAX_REQUEST_BODY_SIZE` if larger; a route that never
  reads its body is not refused for a length its own limit would refuse.
- Limits are 1 byte to 1 GiB (`MAX_ROUTE_BODY_SIZE`). The decorator marks
  the endpoint, so it may sit above or below the route's. A route of a
  mounted application, or of a router nested without flattening
  (`FLATTEN_ROUTERS = False`), keeps its limit, read before routing or
  after.
- `ResourceRouter` takes `body_limits={"command": 16_384, "batch":
  2_000_000}` per kind of route that takes a body (`create`, `update`,
  `command`, `batch`), and `command_route` and `batch_route` take
  `body_limit=` per route ([resources.md](resources.md)).
- A small limit suits the routes anonymous callers reach: a login or a
  sign-up takes a few hundred bytes, and need not take the global one.

## Serving files

Every route must say who may call it, and so must every way of serving a
file ([ADR 0021](adr/0021-check-mounted-applications.md)). Serve stored
files from a route with a rule, and check that the caller may read the
file the name points to:

```python
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

- Files download (`Content-Disposition: attachment`) unless `inline=True`,
  which only images and PDFs may use. Every response says
  `Content-Security-Policy: default-src 'none'; sandbox` and
  `X-Content-Type-Options: nosniff`, so a file cannot run scripts, load
  anything, or be read as another type. The name the browser offers is
  cleaned, in ASCII for old clients and UTF-8 for the others.
- Keep the content type `save_upload` found with the file's name, and pass
  it back to `serve`; never serve a client's own claim.
- Look files up by your own ids, not by names in the URL: a storage name
  is unguessable, but anyone who learns one could otherwise fetch it.
- To let a web server or CDN serve a directory itself, set `base_url`
  (then `storage.url(name)` gives each file's address) and remember that
  everyone who has the address can fetch the file. Mounting the directory
  in the application (`StaticFiles`) is refused by deny by default until
  it is wrapped in `mark_public`, on purpose:

  ```python
  app.mount("/media", mark_public(StaticFiles(directory="/srv/public")))
  ```

  Do that only for files meant for everyone, never for uploads.

# An upload with checks

This recipe writes a route that takes a customer's receipt, a PDF or a
picture, and keeps only a file that is what it claims to be, small
enough, and harmless to open.

## The problem

An upload is a file a stranger chose. Kept as it came, it can hurt:

- **The name is a path.** `../../config/settings.py` as a file name
  climbs out of the upload directory.
- **The type is a claim.** A "picture" sent as `image/png` may be HTML
  with a script, which a browser runs when the file is served.
- **The size has no end.** A file of 10 GB fills the disk while it
  arrives.
- **A real PDF can still run code.** A PDF can carry JavaScript, or an
  action that opens a link or a program when someone opens it.

domainkit's uploads take each in turn ([files and uploads](../uploads.md)):
`save_upload` stores a file under a name it makes up, decides its type
from its first bytes, stops reading at the size limit, and, for a
checked type such as `CHECKED_PDF`, reads the whole document, in a
process of its own, before anything is kept.

You need a project with `domainkit[sqlite,testing,uploads,documents]`
installed: `uploads` reads forms, and `documents` brings pypdf, which
checks PDFs. Every step here is run as written by
`domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project, and say where files go

```bash
domainkit init inboxAPI inbox
cd inbox
python manage.py makemigrations -m "users and permissions"
python manage.py migrate
```

The project's first migration makes the tables of the users who will
log in. A storage keeps files under names it chooses. Name the default one at
the end of `src/config/settings/base.py`: a directory beside the
project, which the application may write.

```python title="src/config/settings/base.py"
STORAGES = {
    "default": {
        "BACKEND": "domainkit.storage.FileSystemStorage",
        "OPTIONS": {"location": str(BASE_DIR / "files")},
    },
}
```

The tests keep files in memory, which the test plugin empties before
each test. Add this at the end of `src/config/settings/test.py`:

```python title="src/config/settings/test.py"
STORAGES = {"default": {"BACKEND": "domainkit.storage.MemoryStorage"}}
```

### Step 2: write the route

Make the new file `src/config/receipts.py`:

```python title="src/config/receipts.py (new file)"
"""Customers' receipts: a checked PDF or a picture, of 2 MB at most."""

from domainkit.http.access import Require
from domainkit.http.middleware import body_limit
from domainkit.http.uploads import CHECKED_PDF, IMAGES, save_upload
from fastapi import APIRouter, Depends, UploadFile, status
from pydantic import BaseModel

MAX_SIZE = 2_000_000
"""The largest receipt kept, in bytes."""

router = APIRouter()


class ReceiptOut(BaseModel):
    """A receipt kept: the storage's name for it, its type, size, and name."""

    name: str
    content_type: str
    size: int
    filename: str


@router.post(
    "/receipts",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(Require())],
)
@body_limit(MAX_SIZE + 100_000)  # The file, and the form around it.
async def upload_receipt(file: UploadFile) -> ReceiptOut:
    """Keep a receipt that is a harmless PDF or a picture, of 2 MB at most."""
    stored = await save_upload(
        file, types=(CHECKED_PDF, *IMAGES), max_size=MAX_SIZE, directory="receipts"
    )
    return ReceiptOut(
        name=stored.name,
        content_type=stored.content_type,
        size=stored.size,
        filename=stored.filename,
    )
```

- `Require()` lets in any caller who logged in. A project would ask for
  a permission of its own, as any route does.
- `body_limit` refuses a request larger than the limit as it arrives,
  with `413`: the request never reaches the route whole.
- `types` lists what the route takes. The type comes from the file's
  first bytes. A file of another type is refused with `415
  unsupported_file_type`, whatever its name or its claimed type.
- `CHECKED_PDF` reads the whole PDF before it is kept, and refuses
  JavaScript, actions run on opening, embedded files, and the like, with
  `422 unsafe_file`. A file it cannot read is `422 unreadable_file`.
- `max_size` stops reading the file at 2 MB: `413 file_too_large`, and
  nothing is kept.
- `stored.name` is the storage's name for the file. Keep it in your
  table. `stored.filename` is the client's name, cleaned of anything
  that could be a path: show it, never use it as one.

Mount the route. Add these lines at the end of `src/config/urls.py`:

```python title="src/config/urls.py"
from config.receipts import router as receipts_router

router.include_router(receipts_router)
```

### Step 3: test it

The tests make their PDFs with pypdf, one plain and one with JavaScript.
Make the new file `tests/test_receipts.py`:

```python title="tests/test_receipts.py (new file)"
"""A receipt is kept only when it is what it claims, small, and harmless."""

import io

from domainkit.testing import TestClient
from pypdf import PdfWriter
from pypdf.actions import JavaScript

URL = "/receipts"


def pdf(*, script: str | None = None) -> bytes:
    """A one-page PDF; with ``script``, JavaScript run when it opens."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if script is not None:
        writer.add_open_action(JavaScript(script))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_pdf_is_kept_under_a_name_of_its_own(admin_client: TestClient) -> None:
    """Its type from its bytes, a new name, and the client's name cleaned."""
    sent = {"file": ("../march<1>.pdf", pdf(), "text/html")}

    kept = admin_client.post(URL, files=sent)

    assert kept.status_code == 201, kept.json()
    assert kept.json()["content_type"] == "application/pdf"
    assert kept.json()["name"].startswith("receipts/")
    assert kept.json()["filename"] == "march1.pdf"


def test_a_pdf_that_runs_a_script_is_refused(admin_client: TestClient) -> None:
    """A real PDF, with JavaScript: checked whole, refused, and never kept."""
    sent = {"file": ("receipt.pdf", pdf(script="app.alert(1)"), "application/pdf")}

    refused = admin_client.post(URL, files=sent)

    assert (refused.status_code, refused.json()["code"]) == (422, "unsafe_file")


def test_a_script_named_png_and_a_large_file_are_refused(
    admin_client: TestClient, client: TestClient
) -> None:
    """Another type is 415, whatever its name; past 2 MB is 413; strangers 401."""
    script = {"file": ("photo.png", b"<script>alert(1)</script>", "image/png")}
    large = {"file": ("scan.pdf", b"%PDF-1.7 " + b"0" * 2_000_000, "application/pdf")}

    assert admin_client.post(URL, files=script).status_code == 415
    assert admin_client.post(URL, files=large).status_code == 413
    assert client.post(URL, files=script).status_code == 401
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format .
pytest
```

## How it works

1. `body_limit` counts the request's bytes as they arrive, and answers
   `413` past its limit, before the form is read whole.
2. `save_upload` reads the first bytes and matches them against the
   types the route takes. The client's `Content-Type` and file name
   choose nothing.
3. For `CHECKED_PDF`, the file is held in a temporary file, and a new
   Python process reads it, bounded in memory and CPU time
   (`UPLOAD_CHECKS`). Only a file that passes is stored.
4. The storage stores the file under a random name in `receipts/`, and
   `save_upload` returns it with its size, type, and cleaned name.

Keep these rules in mind:

- **Serve files from a route that says who may read them**, with
  `serve`, never from a public directory
  ([serving files](../uploads.md#serving-files)).
- **Scan for viruses** where files reach other people: set
  `UPLOAD_SCANNER` ([virus scanning](../uploads.md#virus-scanning)).
- **Word, Excel, and PowerPoint files** are checked the same way, with
  the types `OFFICE` ([checking documents](../uploads.md#checking-documents)).

## See also

- [Files and uploads](../uploads.md): storages, object storage and
  presigned URLs, thumbnails, and the files a change dropped.
- [Resources](../resources.md#files): a resource's route that takes a
  file, with its other fields.
- [Problem codes](../reference/problems.md): `unsupported_file_type`,
  `file_too_large`, and `unsafe_file`.

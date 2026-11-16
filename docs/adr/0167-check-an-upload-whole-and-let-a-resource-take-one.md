# 0167. Check an upload whole before it is kept, and let a resource take one

- Status: Accepted
- Date: 2026-11-16
- Amends: [0003](0003-enforced-layer-rules.md), [0040](0040-store-files-under-random-names.md)

## Context

A project that keeps, converts, and has people sign the documents its
users upload reported five gaps in 2.4 (user reports U1, U3, U5 and D2).

1. **Uploads are checked by their first bytes only** (U1).
   `save_upload` decides a type by `FileType.matches(head)`
   ([ADR 0040](0040-store-files-under-random-names.md)). A Word document
   is a ZIP to it: nothing bounds what it inflates to, and nothing looks
   for macros, OLE objects, ActiveX, a DTD, or a relationship to a file
   outside the package. A PDF passes whatever it holds: JavaScript, an
   action run on opening, an embedded file, an XFA form, a password. The
   project opened every file again after `save_upload`, deleted a refused
   one, and kept a copy of its PDF check in each domain that took files.
2. **A resource cannot take a file** (U3). Command routes take JSON, and
   `save_upload` takes only Starlette's `UploadFile`, which comes from a
   multipart form. `DeviceSigned` signs the raw body, and a page cannot
   know the bytes of a form before `fetch` encodes it, so a phone cannot
   sign one. The project wrote its upload routes by hand: the reach
   checked again beside the resource's, the body wrapped into an
   `UploadFile`, and, since 2.4, the act's twin written by hand too.
3. **`HttpClient` sends no form** (U5): `request` takes no `files`.
4. **A stub set in an async fixture is not seen by the application under
   test** (U5): `stub` is a context variable.
5. **A file gateway cannot import what it needs** (D2): the infrastructure
   layer may import `domainkit.http.client` since 2.4, but not
   `domainkit.http.uploads`.

### What the sources say

- **OWASP File Upload Cheat Sheet** [1]: a signature check "should not
  be used on its own, as bypassing it is pretty common and easy";
  validate the content by type; for ZIP files, "the file size limit
  should be considered after file decompression"; run documents through
  content disarm, an up-to-date library with resource limits, and a virus
  scan.
- **Office documents.** Whether Word runs macros is decided by the main
  part's content type in `[Content_Types].xml`: only the `docm` and `dotm`
  types run them, and renaming a file changes nothing [2][3]. Macros live
  in `vbaProject.bin` (`application/vnd.ms-office.vbaProject`) [3]. A
  document with no macro can still fetch a remote template through an
  external relationship (`TargetMode="External"`) in
  `word/_rels/settings.xml.rels` [4][5], and CVE-2022-30190 ("Follina")
  opened a remote HTML page through an external OLE relationship in
  `document.xml.rels` [6][7]. The Open Packaging Conventions forbid DTDs
  in a package's XML: a consumer treats one as an error [8].
- **PDFs.** Didier Stevens' PDFiD counts the names malicious PDFs use:
  `/JS`, `/JavaScript`, `/AA` and `/OpenAction` (automatic actions),
  `/Launch`, `/EmbeddedFile`, `/XFA`, `/RichMedia` [9][10]. Names may be
  written with `#xx` escapes (`/J#61vaScript`), so a byte search misses
  them; a parser decodes them.
- **pypdf** bounds what it inflates and reads (`Configuration`:
  stream lengths, array and dictionary sizes, the page tree) and leaves
  catching its errors to its caller [11].
- **pytest.** AnyIO's runner runs async fixtures and async tests in one
  task, so they share context variables, but "these context variables are
  not carried over to synchronous tests and fixtures" [12]; pytest-asyncio
  propagates them between async fixtures and tests since 0.25 [13]. The
  HTTP mocking plugins patch the transport for the whole process while a
  test runs (respx, pytest-httpx).

### Options

Where a type's check runs:

| Option | What it costs |
| --- | --- |
| Check the stored file, delete it on a refusal | The file sits in the storage, readable, until the check ends; a crash leaves it there; an object store is read back over the network. |
| **Hold the upload in a temporary file (memory up to 1 MiB), check it, then store it** | A second copy on local disk while the check runs. Nothing refused is ever stored, as with the virus scan. |

What `PDF` means:

| Option | What it costs |
| --- | --- |
| `PDF` checks the whole file | A minor release would refuse PDFs it took (encrypted ones, forms with scripts), and fail without pypdf. |
| **`PDF` stays first-bytes, `CHECKED_PDF` checks** | Two names; a project opts in. 3.0 may make the checked one the default. |

How the Office types are told apart, when all start as a ZIP:

| Option | What it costs |
| --- | --- |
| The first entry's name in the first 512 bytes | Not what the ZIP format promises; LibreOffice and Word order entries differently. |
| **The main part's content type, read by the check; `NotOfType` passes the file to the next type** | The ZIP's directory is read once per type tried. |

What a resource's upload route takes, and how its twin binds the file:

| Option | What it costs |
| --- | --- |
| A form only | A phone cannot sign it. |
| **The raw body (name in `X-Filename`), or a form** | Two ways to send; the body limit is the file's and a form's room. |
| The twin stores the file too | Two stored copies, and the act's digest differs from the twin's: the stored name is new each time. |
| **The twin receives and checks the file, stores nothing, and gives the command an upload with no name; `digest=` gives a field the SHA-256** | An `Action` for an upload act ignores the upload's field and binds the digest. |

How a fixture's stub reaches a test in another context:

| Option | What it costs |
| --- | --- |
| A fixture of the test plugin | One more fixture name; a project's own fixtures still could not stub. |
| `stub` always process-wide | Concurrent stubs in tasks, which tests use, would see each other's. |
| **`stub(..., everywhere=True)`: process-wide until the block ends, a context's own stub first** | A test that runs threads sees the stub in all of them, which is what it wants. |

## Decision

- **`FileType.inspect`**, a callable of the whole file (`IO[bytes]`, read
  from its start). `save_upload` holds the upload in a
  `SpooledTemporaryFile` when the type it matched has one, runs it in a
  worker thread, and stores the file only if it passes. It raises
  `FileRefused` (422, `unsafe_file` or `unreadable_file`, its message
  translated) or `NotOfType` (the next matching type is tried; 415 when
  none is left). Types without a hook stream as before.
- **`CHECKED_PDF`**, `DOCX`, `XLSX`, `PPTX` and `OFFICE`, with
  `PdfCheck` and `OfficeCheck` in `domainkit.storage.inspection` (the
  `documents` extra: pypdf, lxml; a missing library is
  `ImproperlyConfigured` naming the extra). `PDF` and `ZIP` are unchanged.
  - A PDF is read whole: every object the cross-reference names, the
    trailer, names decoded. Refused: encryption, more than `max_pages`
    (1,000), JavaScript, `/AA`, an `/OpenAction` that is an action (a
    destination is kept), `/Launch`, `/GoToR`, `/GoToE`, `/SubmitForm`,
    `/ImportData`, `/RichMediaExecute`, links but `http`, `https` and
    `mailto`, embedded files, rich media, XFA. pypdf runs with streams
    bounded to 32 MiB and a budget of a million values read.
  - An Office document is bounded by its ZIP directory before a byte is
    inflated: 2,000 parts, 200 MiB inflated, a ratio of 200 for a part
    over 1 MiB, no encryption, stored or deflated only, no two parts of
    one name. Its main part must be the plain document's (a
    macro-enabled one is refused for its macros, a template or another
    document is `NotOfType`). Refused: macro parts and relationships
    (VBA, Excel 4.0 macro sheets), OLE objects, ActiveX, Excel's links to
    other workbooks and DDE, any external relationship but a hyperlink to
    `http`, `https` or `mailto`, Word fields that pull content in or run
    a program (`INCLUDEPICTURE`, `INCLUDETEXT`, `LINK`, `DDE`,
    `DDEAUTO`), and a DTD in any XML part. XML is parsed by lxml without
    entities, a DTD, or the network, each element dropped once read. An
    embedded workbook, document, or presentation (a chart's data) is
    checked the same way, one level deep.
- **`save_upload` takes bytes or an async iterable of bytes**, with
  `filename=`; **`receive_upload`** receives and checks without storing
  (`ReceivedUpload.store`, `unstored`, `close`). **`StoredUpload`** moves
  to `domainkit.storage`, where the application layer may name it, and
  carries `sha256` (not compared, so earlier equalities hold).
- **`ResourceRouter.upload_route(command, field=, types=, max_size=,
  ...)`**, in a module of its own (`resources/routing/uploading.py`): a
  command route whose command's `field` gets the `StoredUpload`. The body
  is the file with `X-Filename`, or a form's part named `field`; the body
  limit is `max_size` and 64 KiB. It checks the reach as a command route
  does, stores the file before the service runs, outside its
  transaction, and deletes it when the request fails before the service
  returns. Its twin receives the file and stores nothing.
- **`HttpClient.request(files=...)`** sends `multipart/form-data` through
  httpx2, with `data=` for the other fields. A file is bytes or a binary
  file that can seek, since a retry sends the form again from each
  file's start; anything else, or a form with `content=` or `json=`, is
  `ValueError`.
- **`stub(handler, everywhere=True)`** stubs every call of the process
  until its block ends, from any task, thread, or context, unless a
  plain `stub` holds there. Blocks may end in any order.
- **The infrastructure layer may import `domainkit.http.uploads`**
  (`ALLOWED_MODULES`), as it may `domainkit.http.client`. Starlette and
  `domainkit.http.errors` stay forbidden: a gateway takes the bytes its
  route hands it, and raises domain errors, which the HTTP layer maps.
  [ADR 0003](0003-enforced-layer-rules.md) is amended.

## Consequences

- A project lists `CHECKED_PDF` and `OFFICE` where it listed `PDF` and
  `ZIP`, installs the `documents` extra, and drops its own checks and the
  deletion after `save_upload`. A file a check refuses never reaches the
  storage or the virus scanner.
- The checks refuse what is known to be used against readers and
  converters; they do not make a file harmless. A parser bug in pypdf or
  lxml is still a risk, so keep the virus scan, serve files with
  `serve` (sandboxed, never sniffed), and convert in a sandbox.
- Some legitimate documents are refused: one attached to a template on a
  share, one with a picture linked rather than embedded, a PDF whose
  links open files, or one protected by an owner password only. The
  refusal says why; a project that must take them writes a `FileType`
  of its own.
- A check holds the upload on local disk past 1 MiB, and its CPU is a
  worker thread's; a request's deadline does not stop a thread, so the
  bounds above are what bound the work.
- An `Action` for an upload act must ignore the upload's field
  (`ignore={"file"}`) and bind the digest, or the act's authorization
  never matches: that fails closed (403).
- `stub(..., everywhere=True)` is for tests that run one at a time in a
  process, as pytest does (and each pytest-xdist worker).

## Sources

1. OWASP, File Upload Cheat Sheet:
   <https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html>
2. Cisco Talos, "Macro intruders: sneaking past Office":
   <https://blog.talosintelligence.com/macro-intruders-sneaking-past-office/>
3. Microsoft, [MS-OFFMACRO2] and the Open XML SDK, "Convert a word
   processing document from the DOCM to the DOCX file format":
   <https://github.com/OfficeDev/open-xml-docs/blob/live/docs/word/how-to-convert-a-word-processing-document-from-the-docm-to-the-docx-file-format.md>
4. ired.team, "Inject macros from a remote dotm template":
   <https://ired.team/offensive-security/initial-access/phishing-with-ms-office/inject-macros-from-a-remote-dotm-template-docx-with-macros>
5. SANS Internet Storm Center diary 29976:
   <https://isc.sans.edu/diary/29976>
6. ReversingLabs, "Follina exploit powers live-off-the-land attacks":
   <https://reversinglabs.com/blog/threat-analysis-follina-exploit-powers-live-off-the-land-attacks>
7. Rapid7, CVE-2022-30190 analysis:
   <https://www.rapid7.com/blog/post/ra-cve-2022-30190-analysis/>
8. Microsoft, "Open Packaging Conventions v0.85: coming changes" (DTDs
   are an error): <https://learn.microsoft.com/en-us/archive/blogs/xps/open-packaging-conventions-v0-85-coming-changes>
9. Didier Stevens, PDF tools (PDFiD):
   <https://blog.didierstevens.com/programs/pdf-tools/>
10. Didier Stevens, "PDFiD false positives":
    <https://blog.didierstevens.com/2013/06/10/pdfid-false-positives>
11. pypdf, Security: <https://pypdf.readthedocs.io/en/stable/user/security.html>
12. AnyIO, Testing with AnyIO: <https://anyio.readthedocs.io/en/stable/testing.html>
13. pytest-asyncio, Changelog (0.25.0, 1.1.0):
    <https://pytest-asyncio.readthedocs.io/en/latest/reference/changelog.html>

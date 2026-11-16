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
  types run them, and renaming a file changes nothing [2], [3]. Macros live
  in `vbaProject.bin` (`application/vnd.ms-office.vbaProject`) [3]. A
  document with no macro can still fetch a remote template through an
  external relationship (`TargetMode="External"`) in
  `word/_rels/settings.xml.rels` [4], [5], and CVE-2022-30190 ("Follina")
  opened a remote HTML page through an external OLE relationship in
  `document.xml.rels` [6], [7]. The Open Packaging Conventions forbid DTDs
  in a package's XML: a consumer treats one as an error [8].
- **PDFs.** Didier Stevens' PDFiD counts the names malicious PDFs use:
  `/JS`, `/JavaScript`, `/AA` and `/OpenAction` (automatic actions),
  `/Launch`, `/EmbeddedFile`, `/XFA`, `/RichMedia` [9], [10]. Names may be
  written with `#xx` escapes (`/J#61vaScript`), so a byte search misses
  them; a parser decodes them.
- **pypdf** bounds what it inflates and reads per stream
  (`Configuration`: stream lengths, array and dictionary sizes, the page
  tree) and leaves catching its errors to its caller [11]. It bounds
  nothing in all: decoded object streams are kept, with no total. And
  when an object is not where its table says, or the table does not
  list it, pypdf copies the whole file and searches it, once per object,
  even in strict mode. A review of this change found a 4 MB file whose
  thousands of wrong entries took 51 s, a 1 MB file that took 1 GiB of
  memory, and scripts in objects the table left out that pypdf found and
  the check did not read. A second review found what no bound in the
  process can reach: pypdf's constructor decodes and keeps every table
  of a `/Prev` chain before a check starts (2 MB took 2 GiB); one of its
  calls reads an object stream's index again and again without
  returning (529 bytes ran past 60 s); and a thread that runs a check
  goes on after the request that started it is cancelled, freeing its
  slot for another.
- **Process isolation.** Chromium's Rule of 2: never pick more than two
  of untrustworthy input, an unsafe implementation language, and high
  privilege; parse untrusted input in a sandboxed process that returns a
  well-formed answer, which the privileged side validates [14]. The
  system holds a process to an address space and CPU time
  (`setrlimit`: `RLIMIT_AS`, `RLIMIT_CPU`, which ends it with `SIGXCPU`)
  [15], [16]; a thread has no such limits, and cannot be killed.
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

How a PDF check is bounded:

| Option | What it costs |
| --- | --- |
| pypdf's own limits | Per stream only; a search of the file per misplaced object. |
| Strict reading of the table, totals and a deadline in the process, checks in threads of their own | Taken first, and kept as a second layer; the second review showed what it cannot bound: pypdf's constructor, one call of its, and a thread a cancelled request leaves running. |
| A pool of processes kept warm | No start per upload; a process that checked one file checks the next, with what that file left in it. |
| **A new process per check (`python -I`), with `RLIMIT_AS` and `RLIMIT_CPU`, killed past its wall clock; a slot held until it exits; the answer read as JSON of known shapes** | A process start per checked upload: about 0.08 s for an Office document and 0.14 s for a PDF here, against 0.01 s in the process. A check must pickle. Windows has no such limits (the clock alone), and macOS does not hold a process to `RLIMIT_AS`. |

What a resource's upload route takes, and how its twin binds the file:

| Option | What it costs |
| --- | --- |
| A form only | A phone cannot sign it. |
| **The raw body (name in `X-Filename`), or a form** | Two ways to send; the body limit is the file's and a form's room. |
| The twin stores the file too | Two stored copies, and the act's digest differs from the twin's: the stored name is new each time. |
| The twin gives the command an upload with no name, and the `Action` ignores the field | An act's authorization is spent on any file; a project must remember to bind a digest. |
| **The twin receives and checks the file, stores nothing, and gives the command an upload with no name; an `Action` binds an upload by its content, never its storage name** | One rule in `Action.arguments`; nothing to declare. |

How a fixture's stub reaches a test in another context:

| Option | What it costs |
| --- | --- |
| A fixture of the test plugin | One more fixture name; a project's own fixtures still could not stub. |
| `stub` always process-wide | Concurrent stubs in tasks, which tests use, would see each other's. |
| **`stub(..., everywhere=True)`: process-wide until the block ends, a context's own stub first** | A test that runs threads sees the stub in all of them, which is what it wants. |

## Decision

- **`FileType.inspect`**, a callable of the whole file (`IO[bytes]`, read
  from its start), which must pickle. `save_upload` holds the upload in a
  `SpooledTemporaryFile` when the type it matched has one, runs it in a
  process of its own, and stores the file only if it passes. It raises
  `FileRefused` (422, `unsafe_file` or `unreadable_file`, its message
  translated) or `NotOfType` (the next matching type is tried; 415 when
  none is left). Types without a hook stream as before.
- **`CHECKED_PDF`**, `DOCX`, `XLSX`, `PPTX` and `OFFICE`, with
  `PdfCheck` and `OfficeCheck` in `domainkit.inspection` (the
  `documents` extra: pypdf, lxml; a missing library is
  `ImproperlyConfigured` naming the extra). `PDF` and `ZIP` are unchanged.
  - A PDF is read by pypdf in strict mode, and only as its tables say:
    every object the tables declare, every reference from the trailer
    (an object not declared, or not at its offset, refuses the file as
    `unreadable_file`, before pypdf would search the file for it), and
    every `N G obj` written in the file that the tables do not point at
    (an earlier revision, an object no table lists). Names are compared
    decoded. Refused: encryption, more than `max_pages` (1,000),
    JavaScript (an action's, a name tree's, a 3D annotation's
    `/OnInstantiate`), `/AA`, an `/OpenAction` that is an action (a
    destination is kept), `/Launch`, `/GoToR`, `/GoToE`, `/Thread` with
    a file, `/SubmitForm`, `/ImportData`, `/RichMediaExecute`, links but
    `http`, `https` and `mailto` (a link that is not text included),
    media from a URL, embedded files, rich media, XFA.
  - A PDF check's bounds: each stream decodes to 16 MiB, the object
    streams to `max_decoded` (64 MiB) in all; the tables declare at most
    `max_objects` (200,000); the check reads a million values; and its
    deadline, `max_seconds` (10), is looked at per object.
  - An Office document is bounded by its ZIP directory before a byte is
    inflated: 2,000 parts, and `max_expansion` (50) times the upload's
    size inflated in all, never past 200 MiB; a ratio of 200 for a part
    over 1 MiB; no encryption; stored or deflated only; no two parts of
    one name, nor a header that names a part otherwise than the
    directory. Its main part must be the plain document's (a
    macro-enabled one is refused for its macros, a template or another
    document is `NotOfType`). Refused: macro parts and relationships
    (VBA, Excel 4.0 macro sheets), OLE objects and ActiveX (their parts,
    and `w:object`, `o:OLEObject`, `w:control` in the XML), imported
    chunks (`altChunk`), pictures and objects linked (`r:link`), an
    element but a hyperlink that names a hyperlink's relationship,
    Excel's links to other workbooks, data connections and query tables,
    formulas anywhere in a sheet (cells, names, conditional formats,
    validations, tables, charts) that call `WEBSERVICE`, `RTD`, `IMAGE`
    or DDE, any relationship to outside the
    package but a hyperlink to `http`, `https` or `mailto` (external
    unless its `TargetMode` is absent or `Internal`, or whenever its
    target names a scheme or a share), Word fields that pull content in
    or run a program (`INCLUDEPICTURE`, `INCLUDETEXT`, `INCLUDE`,
    `IMPORT`, `LINK`, `DATABASE`, `DDE`, `DDEAUTO`, read by the letters
    they start with) or whose instruction starts with another field, and
    an XML part that declares a DTD or an entity, or is in another
    encoding than UTF-8 and UTF-16. A part is XML by its name (`.xml`,
    `.rels`, `.vml`) or its content type (`+xml`), never by its bytes,
    which may start with anything: one that is not XML after all is
    `unreadable_file`. White space before its markup is passed over,
    however long, and its declaration read whole. A DTD is refused in
    the bytes, in the part's encoding, before lxml parses them; the part
    is never decoded to text. XML is parsed by lxml without
    entities, a DTD, or the network, each element dropped once read. An
    embedded workbook, document, or presentation (a chart's data) is
    copied to a temporary file and checked the same way, one level deep,
    within the same budget. The deadline, `max_seconds` (10), is looked
    at per part and every 1,024 elements.
  - Each upload's checks run in a new process (`sys.executable -I`),
    handed the checks (pickled) and the file over a pipe, its own output
    sent to standard error. It limits itself first (`RLIMIT_AS` to
    `UPLOAD_CHECKS.MEMORY`, 512 MiB; `RLIMIT_CPU` to `SECONDS`, 15), and
    the parent kills it past `SECONDS` of wall clock. Its answer is one
    line of JSON of a few known shapes, read as data. A process stopped
    by a limit refuses the file, 422 `unreadable_file` ("took too long",
    "needed too much memory"). At most `PROCESSES` (4) run at once in a
    process of the application, counted across its event loops; one more
    upload is 503 `service_busy`, not queued. A slot is freed only once
    its process has exited: a cancelled request kills it, waits for it,
    then frees the slot. A type without a check takes no process.
  - The checks themselves stay bounded as a second layer, and a PDF
    pypdf read only in part (it logs that it skipped a table, or found
    an object elsewhere) is refused.
- **`save_upload` takes bytes or an async iterable of bytes**, with
  `filename=`; **`receive_upload`** receives and checks without storing
  (`ReceivedUpload.store`, which drops the temporary file, `unstored`,
  `close`). **`StoredUpload`** moves
  to `domainkit.storage`, where the application layer may name it, and
  carries `sha256` (not compared, so earlier equalities hold).
- **`ResourceRouter.upload_route(command, field=, types=, max_size=,
  ...)`**, in a module of its own (`resources/routing/uploading.py`): a
  command route whose command's `field` gets the `StoredUpload`. The body
  is the file with `X-Filename`, or a form's part named `field`; the body
  limit is `max_size` and 64 KiB. Before it reads a byte of the body,
  on one aggregate, it checks the aggregate within the caller's reach,
  the rules on it, and `If-Match`; then it stores the file before the
  service runs, outside its transaction, and checks again in it as a
  command route does. A request whose unit of work did not commit
  deletes the file. Its twin checks the reach first too, receives the
  file, and stores nothing.
- **`Action.arguments` binds an upload by its content**: a
  `StoredUpload`, wherever it is in the command (a list, a mapping, a
  model), is read without the name the storage gave it. The twin's
  command and the act's agree on the same file, and differ for another;
  `digest=` is a convenience. One without a SHA-256 (made by hand) is
  bound by its name, so that two such uploads never agree.
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
  links open files or name no scheme (`www.example.com`), one protected
  by an owner password only, or one Acrobat Distiller made with its job
  options embedded (a `.joboptions` file). The
  refusal says why; a project that must take them writes a `FileType`
  of its own.
- A check holds the upload on local disk past 1 MiB, and a process of
  its own for at most `SECONDS`. Its memory and CPU are the system's to
  bound; a request cancelled meanwhile ends it. A checked upload costs a
  process start more. A check of a project's own runs there too: its
  module is imported for each upload, so keep it cheap.
- A PDF that a strict reader cannot read is refused, though a viewer
  that repairs files would show it: a cross-reference table at the
  wrong place, an object not where the table says. Saving it again from
  a viewer makes a readable one.
- An `Action` for an upload act declares nothing about the upload. One
  that ignores the upload's field binds no file at all: don't.
- `stub(..., everywhere=True)` is for tests that run one at a time in a
  process, as pytest does (and each pytest-xdist worker).

## Sources

1. OWASP, File Upload Cheat Sheet:
   <https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html>
2. Cisco Talos, "Macro intruders: sneaking past Office":
   <https://blog.talosintelligence.com/macro-intruders-sneaking-past-office/>
3. Microsoft, MS-OFFMACRO2 and the Open XML SDK, "Convert a word
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
14. Chromium, The Rule Of 2:
    <https://chromium.googlesource.com/chromium/src/+/main/docs/security/rule-of-2.md>
15. Python, `resource`: resource usage information:
    <https://docs.python.org/3/library/resource.html>
16. Linux, getrlimit(2): <https://man7.org/linux/man-pages/man2/getrlimit.2.html>

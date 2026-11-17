# 0175. Bound a PDF's page content, check an act's upload once, and read a signed body last

- Status: Accepted
- Date: 2026-11-17

## Context

The project that keeps, converts, and has people sign the documents its
users upload reported five gaps against 2.6 (user reports U3, U5, U6),
in what [0167](0167-check-an-upload-whole-and-let-a-resource-take-one.md)
built: the upload checks, `upload_route` and its twin.

1. **`PdfCheck` decodes no page's content** (U6, the check's half). It
   bounds what a PDF's object streams decode to (`max_decoded`) but never
   decodes a page's content stream. A PDF of 20 pages, each a deflated
   content stream of 8 MiB of spaces, is 170 KB: `CHECKED_PDF` takes it,
   and laying text over it (`overlay_pdf`) made 168 MB, 390 MB of memory,
   in the web worker. The project ran a check of its own after
   `PdfCheck`, decoding the pages' contents within 16 MiB in the check's
   process. (`overlay_pdf`'s half is another change's.)
2. **`OfficeCheck` misses unended fields and VML pictures** (U5). It read
   a field's instruction from `w:instrText` once the field separated or
   ended: a field never ended, and one whose instruction is a tracked
   deletion (`w:delInstrText`), passed with `INCLUDEPICTURE` in them. It
   refused a picture naming a hyperlink's relationship only by an
   attribute of the relationships' namespace (`r:id`): a VML picture
   (`v:imagedata`, `v:fill`) loaded by `src`, or by `o:relid` naming a
   relationship outside the package, passed. The project ran a scan of
   its own after `OfficeCheck`.
3. **An upload's twin checks the file again** (U5). The twin of an
   `upload_route` receives and checks the whole file; the act, once the
   phone confirmed, receives it and checks it again: two check processes
   for one file. The project throttled the twin's asks on their own.
4. **`DeviceSigned` reads the whole body before it looks the device up**
   (U5). `await request.body()` came first: a stolen session without the
   phone's key made the service read every body it sent, up to the
   route's limit (50 MiB there). The project throttled the phone's upload
   before its rules, and held its body to a smaller limit.
5. **`upload_route` fixes its types, size and storage when declared, and
   takes no `dependencies=`** (U3). A route whose file's type depends on
   the aggregate (a template's kind) stayed hand-written; bounds that are
   settings a test overrides were read once, at import; and a limit of the
   act went in `provide`, which the twin runs too, with a check that
   skipped the twin.

### What the sources say

- **pypdf** bounds what each filter decodes to (`Configuration`:
  `zlib_maximum_output_length`, 75 MB by default; LZW, run-length,
  Brotli, JBIG2 alike), per stream and per filter, never in all, and
  leaves catching its errors to its caller [1], [2]. It decodes a page's
  content whole (`get_data`), and keeps it. Its `apply_configuration`
  sets the limits for a block, in a context variable.
- **zlib** inflates a bounded amount at a time: `Decompress.decompress(data,
  max_length)` returns at most `max_length` bytes and keeps the rest of
  the input in `unconsumed_tail` [3]. So a deflated stream can be counted
  in constant memory. OWASP counts a file's size after decompression [4],
  and asks that every payload's size be bounded [5].
- **Word fields.** A complex field is a `begin` field character, its
  instruction (`w:instrText` runs), an optional `separate`, its result,
  and an `end` [6]. A run of `w:delInstrText` is a field's instruction
  deleted with change tracking on: rejecting the change brings it back,
  accepting it drops it [7].
- **VML.** A VML picture's `src` "defines a source for the image" and its
  `o:href` "defines a URL for an image" [8]. LibreOffice takes a VML
  picture's and a fill's image from `o:relid` when it is there, else from
  `r:id`, both through the part's relationships [9].
- **Signed bodies.** HTTP Message Signatures cover a body through its
  `Content-Digest` field, and the verifier must then check the digest
  against the content too: the signature alone does not [10]. RFC 9530
  writes the field as a dictionary of algorithms, each a byte sequence in
  base64 between colons (`sha-256=:...:`) [11]. A key named by the
  request (`keyid`) is found before anything is verified [10].
- **A verdict kept by content** tells of the content: a service that
  answers sooner for a file another user sent confirms that file exists
  ("confirmation of a file", in deduplicated storage) [12].
- **Per-request bounds.** Django's `FileField` takes a callable for
  `upload_to`, given the instance and the file name, and for `storage`
  [13]. FastAPI runs a route's `dependencies` as any dependency, its
  values unused [14].

### Options

How a PDF's page content is bounded:

| Option | What it costs |
| --- | --- |
| pypdf's per-filter limits alone | 75 MB a stream; twenty streams pass. |
| Decode each page with `get_data`, counting | pypdf keeps what it decoded: the check holds every page's content. |
| **Decode each page's content in the check, counting, within `max_content` in all: `/FlateDecode` alone inflated 64 KiB at a time and dropped; other filters by pypdf, each filter held to what is left** | Each stream decoded in the check's process (64 MiB at most, by default: about a tenth of a second). A stream two pages draw counts twice, as a program that lays text over each page writes it twice. Images and form XObjects are not counted: a renderer decodes them, not `overlay_pdf`. |

What `max_content` defaults to:

| Option | What it costs |
| --- | --- |
| None: opt in | The default `CHECKED_PDF` keeps taking the report's file. |
| 16 MiB, as the project's check | A long or drawn document (maps, plans) of more content is refused. |
| **64 MiB, as `max_decoded`** | A PDF of more content, which 2.6 took, is refused: a security fix, in the changelog's `Security` and in the upgrading notes. |

How fields and VML are read:

| Option | What it costs |
| --- | --- |
| Refuse any field never ended, or any tracked deletion of an instruction | Simple; refuses a document edited with tracking whose field codes changed. |
| **Check an unended field's instruction at the part's end; read `w:delInstrText` as instruction text, the instruction checked with its deletions and without them** | Two readings of each instruction. A mix of some deletions accepted and others rejected is not tried. |
| **Refuse a VML element's `src`, a VML picture's `o:href` and `o:althref`, and `o:relid` naming a hyperlink's relationship, as `r:id` is** | A VML shape's own `href` (a link to follow) stays allowed. |

How the act skips the twin's check:

| Option | What it costs |
| --- | --- |
| Keep checking twice | A second process per confirmed upload. |
| A token from the twin, sent back by the client | Stateless; every client changes, and the twin's answer is the rule's model, not ours. |
| A memo in the process | Another worker misses it. |
| **The verdict in the default cache, signed with `SECRET_KEY`, under a digest of the route, the caller, the content's SHA-256, the candidates' checks (pickled), and domainkit's version; for 30 minutes** | A cache round trip per checked upload of a route that asks for an authorization. A cache that is down, or holds a forged value, has the file checked. A project's own check changed by a deploy is taken as before for up to 30 minutes. |
| The same, shared between callers | Answers sooner for a file another caller sent: tells of it [12]. |

How `DeviceSigned` reads the body:

| Option | What it costs |
| --- | --- |
| Look the device up first, in the same unit of work, reading the body in it | Holds a database connection while a client sends its body. |
| **Look the device up first in a unit of work of its own, then read the body, then record the request in another** | A second transaction for a signed request with `SIGNATURE_STORE = "database"`; none for the cache. A session thief who names a device of the user (`GET /devices` lists them) is still read. |
| **Also take `Content-Digest`: verify the signature with the declared digest before the body is read, then the body against it; `DEVICES.CONTENT_DIGEST = "required"` refuses a request without one** | The signed message is unchanged, so clients that send nothing work as before. With `"required"`, nothing a thief sends is read. |

How `upload_route` decides per request:

| Option | What it costs |
| --- | --- |
| A FastAPI dependency per bound | Runs before the aggregate is reached: it cannot see it. |
| One callable returning every bound | A new type a project builds. |
| **Each of `types`, `max_size`, `storage`, `directory` a value or a callable of the request and the aggregate reached (None on the collection), sync or async, run after the reach and before the body** | A `max_size` decided per request needs `body_limit`, which the body is held to while it arrives. |

## Decision

- **`PdfCheck(max_content=64 MiB)`**: once every object is read, the
  check decodes each page's `/Contents` (a stream, or each stream of an
  array) and counts the bytes, refusing past `max_content` in all
  ("too large or too complex", `unsafe_file`). A stream whose filters are
  `/FlateDecode` alone (up to 16, pypdf's own bound) is inflated 64 KiB
  at a time and nothing of it kept; the clock is looked at for each
  chunk. One with other filters, or whose deflated data is damaged (gzip,
  which pypdf recovers), is decoded by pypdf under `apply_configuration`,
  each filter's limit what is left of `max_content` plus one byte. A page
  with no content, or content that is no stream, counts nothing.
- **`OfficeCheck`** reads `w:delInstrText` as instruction text, and checks
  each instruction twice: with its deletions (rejected) and without them
  (accepted); a nested field that starts an instruction whose text so far
  is all deleted is refused as one that starts with a field. The fields a
  part began and never separated nor ended are checked at the part's end.
  A VML element (`urn:schemas-microsoft-com:vml`) with a `src`, a VML
  picture with an `o:href` or `o:althref`, and an `o:relid` naming a
  hyperlink's relationship are refused as content from elsewhere.
- **`receive_upload(..., remember=None)`** (New in 2.7) names who receives
  the file. With it, a file whose checks took it under that name within
  `CHECKED_FOR` (30 minutes, `AUTHORIZATIONS.TTL`'s most), of the same
  content and checks, is not checked again; one taken now is kept. Only a
  file a check reads is remembered, and only what the checks took. An
  `upload_route` whose rules ask for an authorization receives its file
  under the route's name and the caller's id, on the twin and the act.
- **`DeviceSigned`** reads the headers first (device id, time, signature,
  `Content-Digest`), looks the device up in a unit of work of its own,
  and refuses one that is missing or not the caller's before the body is
  read. With a declared SHA-256 the signature holds for, the body is read
  and must have it. Otherwise the signature is checked once the body is
  read, unless `DEVICES.CONTENT_DIGEST = "required"` (New in 2.7; default
  `"optional"`), which refuses the request unread. The request is then
  recorded in another unit of work, as before.
- **`upload_route`** takes `types`, `max_size`, `storage` and `directory`
  as values or callables (`Decided`), and `dependencies=` as
  `command_route` does; the twin runs none of them. What a callable
  decides is checked as a declared value is (a `ValueError` or
  `TypeError`: a 500).

## Consequences

- A PDF whose pages' contents decode to more than 64 MiB in all is
  refused by `CHECKED_PDF`; one whose content uses a filter pypdf does not
  know is `unreadable_file`. A project that takes larger drawings gives
  `PdfCheck(max_content=...)` its own bound.
- A Word document with an unended `INCLUDEPICTURE`, a tracked deletion of
  one, or a VML picture or fill from a URL, is refused. A document whose
  VML picture names its image by `src` alone (HTML saved as a document
  by an old tool) is refused too.
- A confirmed upload costs one check process, not two, when the act runs
  in a process that shares the default cache with the twin's (Redis, or
  one process). The file is still sent twice: the act stores the bytes it
  receives, which a phone signs.
- `DeviceSigned` with `SIGNATURE_STORE = "database"` takes two short
  transactions where it took one, so that no connection is held while a
  body arrives. A phone app that sends `Content-Digest` lets the operator
  set `CONTENT_DIGEST = "required"`; until every app in use sends it,
  keep `"optional"`.
- A route that decides its bounds per request reads them after the
  reach, so a test that overrides a setting sees it at the next request.

## Sources

1. pypdf, `Configuration`: <https://pypdf.readthedocs.io/en/stable/modules/configuration.html>
2. pypdf, Security: <https://pypdf.readthedocs.io/en/stable/user/security.html>
3. Python, `zlib.Decompress.decompress`: <https://docs.python.org/3/library/zlib.html#zlib.Decompress.decompress>
4. OWASP, File Upload Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html>
5. OWASP API Security Top 10 2023, API4 Unrestricted Resource
   Consumption: <https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption>
6. ECMA-376, `fldChar` (Complex Field Character): <https://webapp.docx4java.org/OnlineDemo/ecma376/WordML/fldChar.html>
7. ECMA-376, `delInstrText` (Deleted Field Code): <https://webapp.docx4java.org/OnlineDemo/ecma376/WordML/delInstrText.html>
8. Microsoft, VML `imagedata` element: <https://learn.microsoft.com/en-us/windows/win32/vml/msdn-online-vml-imagedata-element>
9. LibreOffice, `oox/source/vml/vmlshapecontext.cxx`: <https://docs.libreoffice.org/oox/html/vmlshapecontext_8cxx_source.html>
10. RFC 9421, HTTP Message Signatures (1.4, 7.2.8): <https://www.rfc-editor.org/rfc/rfc9421.html>
11. RFC 9530, Digest Fields (2): <https://www.rfc-editor.org/rfc/rfc9530.html>
12. Harnik, Pinkas, Shulman-Peleg, "Side Channels in Cloud Services:
    Deduplication in Cloud Storage", IEEE Security & Privacy 8(6), 2010:
    <https://doi.org/10.1109/MSP.2010.187>
13. Django, `FileField.upload_to` and `storage`: <https://docs.djangoproject.com/en/5.2/ref/models/fields/#filefield>
14. FastAPI, Dependencies in path operation decorators: <https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-in-path-operation-decorators/>

# 0175. Bound a PDF's page content, check an act's upload once, and read a signed body last

- Status: Accepted; amended 2026-11-17 (at the end), and by [0181](0181-decide-a-body-limit-per-request-count-before-the-rules-hand-over-the-file-received-and-bound-the-xml-parsed.md)
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
| Decode each page's content in the check, counting, within `max_content` in all: `/FlateDecode` alone inflated 64 KiB at a time and dropped; other filters by pypdf, each filter held to what is left | Taken first. A review found what it missed: a predictor's padding (292 KB of `/Predictor 12` became 3.8 GB in pypdf), and the filters pypdf runs in Python, unbounded in memory or time (60 KB deflated then ASCII85 made 5.7 GB; 2.5 KB of LZW clear codes took 11 s), stopped by the process's limits alone. |
| **Decode each page's content as `overlay_pdf` decodes it, by one policy in `domainkit.inspection` ([0176](0176-bound-what-a-pdf-decodes-list-placeholders-fill-drafts-and-clean-less.md)): its filters read first (`filters_refused`), each stream decoded once within what is left of `max_content` (`DecodeBudget`), the file read by `bounded_reader`** | pypdf keeps each stream it decoded, so the check holds up to `max_content`, and zlib's buffer as much again. A stream two pages draw counts twice, as a program that lays text over each page writes it twice; it is decoded once and counted again. Images and form XObjects are not counted: a renderer decodes them, not `overlay_pdf`. |

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
| **Look the device up first in a unit of work of its own, then read the body, then read the device again and record the request in another** | Two transactions for a signed request where 2.6 took one, with either store. A session thief who names a device of the user (`GET /devices` lists them) is still read. |
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
  ("too large or too complex", `unsafe_file`). It does so by the policy
  `overlay_pdf` decodes with, moved to `domainkit.inspection` as public
  names (ADR 0176): the file is opened by `bounded_reader` (32
  cross-reference sections, each stream deflated and 8 bytes an entry;
  32 searches; no rebuilt table, no object stream in another, no object
  from a stream not decoded); the object streams are decoded first,
  within `max_decoded`, by one `DecodeBudget`, and the pages' content by
  another, within `max_content`. Each stream's filters are read before it
  is decoded (`filters_refused`: no predictor, no image's filter or
  `/Crypt`, a filter pypdf runs in Python only first, and storing
  `max_content / 32` in all, LZW `/ 512`); it then decodes no further
  than what is left, and the deadline is read after it, as pypdf looks
  at no clock. A stream named again is counted again, not decoded again,
  and each name is a value the check reads (`PDF_NODES`): a page naming
  one byte 49,000 times is refused in under a second. A page with no
  content, or content that is no stream, counts nothing.
- Measured on the review's 39 files, through `PdfCheck` in its process:
  each took 0.2 to 1.0 s and at most 176 MiB (the interpreter, pypdf,
  and zlib's buffer of twice the 64 MiB it may return), all refused but
  the four within the bounds; through `overlay_pdf`, at most 1.2 s and
  112 MiB.
- **`OfficeCheck`** reads `w:delInstrText` as instruction text, and checks
  each instruction twice: with its deletions (rejected) and without them
  (accepted); a nested field that starts an instruction whose text so far
  is all deleted is refused as one that starts with a field. The fields a
  part began and never separated nor ended are checked at the part's end.
  A VML element (`urn:schemas-microsoft-com:vml`) with a `src`, a VML
  picture, fill or stroke with an `o:href` or `o:althref`, and an
  `o:relid` naming a hyperlink's relationship are refused as content from
  elsewhere. A `HYPERLINK` field's target is held to what a hyperlink's
  relationship may name (`http`, `https`, `mailto`), its switches' own
  arguments left out. A part a relationship names, of any type, is read
  as XML when its bytes start as XML does, whatever its name, its content
  type and its relationship's type say: a reader follows a header's
  relationship to `header2.png`, typed an image, and parses it, and a
  LibreOffice conversion fetched what such a header linked. One lxml
  cannot read is passed over only if, read again by lxml's pull parser
  with no limit of its own (`huge_tree`), it stops within its first 64
  characters with no element or namespace begun: binary data. A pattern
  of the bytes took a `>` in quotes for a tag's end; an element begun
  missed a comment of 10 MB before the root, which lxml stops at and
  LibreOffice reads past; and the time of an error says nothing, as
  lxml's push parser holds bytes until it can read them, and reports a
  name too long where the name begins. Any other part is a document,
  refused however lxml stopped: too deep (LibreOffice reads 300 levels,
  lxml 256), too big before its root, or broken. An embedded
  font's scrambled first byte is `<` 1 time in 256, and 4 such fonts in
  100 begin an element, measured on 10,000: about 1 embedded font in
  6,600 is refused.
- **`receive_upload(..., remember=None)`** (New in 2.7) names who receives
  the file. With it, a file whose checks took it under that name within
  30 minutes (`AUTHORIZATIONS.TTL`'s most), of the same content and
  checks, is not checked again; one taken now is kept. Only a
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
  recorded in another unit of work, which reads the device again and
  refuses it if it was unbound, or its key changed, while the body
  arrived: 2.6 read it and recorded the request in one.
- **`upload_route`** takes `types`, `max_size`, `storage` and `directory`
  as values or callables of the request and the aggregate, and
  `dependencies=` as `command_route` does; the twin runs none of them.
  What a callable decides is checked as a declared value is (a
  `ValueError` or `TypeError`: a 500), the storage an alias names looked
  up and the directory checked before the body is read, a declared one's
  too.

## Consequences

- A PDF whose pages' contents decode to more than 64 MiB in all is
  refused by `CHECKED_PDF`, and so is one the shared policy refuses: a
  predictor or an image's filter on a page's content, a filter pypdf
  runs in Python but first or past its share, more than 32 sections. A
  project that takes larger drawings gives `PdfCheck(max_content=...)`
  its own bound, and `UPLOAD_CHECKS.MEMORY` room for it: about 100 MiB,
  plus the file, plus `max_decoded`, plus twice `max_content`, as the
  check keeps what it decodes and zlib holds the largest stream twice
  (a page of 250 MiB needed 540 MiB).
- A Word document with an unended `INCLUDEPICTURE`, a tracked deletion of
  one, or a VML picture or fill from a URL, is refused. A document whose
  VML picture names its image by `src` alone (HTML saved as a document
  by an old tool) is refused too.
- A confirmed upload costs one check process, not two, when the act runs
  in a process that shares the default cache with the twin's (Redis, or
  one process). The file is still sent twice: the act stores the bytes it
  receives, which a phone signs. A verdict outlives a deploy that changes
  a project's own check, for up to 30 minutes; and a check that holds a
  set pickles in another order in each process (string hashes are
  salted per process), so its verdict is found only where it was kept.
- `DeviceSigned` takes two short transactions where it took one, so that
  no connection is held while a body arrives. A phone app that sends `Content-Digest` lets the operator
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

## Amendment (2026-11-17): read a field's name as readers do, SVG pictures, and mail merges

The project reported three ways past `OfficeCheck` on 2.7 (user report
U8), each found by a check of its own after it:

1. **A field's name only when written bare.** The name was the letters an
   instruction starts with, past white space. `\INCLUDEPICTURE "..."`,
   `"INCLUDETEXT" "..."` and `"HYPERLINK" "file:///..."`, in a field's
   runs or in `w:fldSimple`, gave no name and passed.
2. **No SVG picture.** An SVG part (`word/media/*.svg`, and `xl/` and
   `ppt/` alike) was parsed as XML for its type, but nothing in it was
   read for what it loads.
3. **No mail merge.** `w:mailMerge` in `settings.xml`, whose
   `w:connectString` or `w:query` names a data source with no
   relationship, passed; a relationship to the source is refused.

### What was found

- LibreOffice reads a field's type past a leading backslash, since Word
  opens such fields (tdf#54584) [15], and takes a quoted token as one
  token [16]. LibreOffice 24.2 headless, converting to ODT, imported
  `\INCLUDETEXT`, `"INCLUDETEXT"`, `"\INCLUDETEXT"`, `\includeText`, a
  backslash split into its own run, and both in `w:fldSimple`, as field
  68, `INCLUDETEXT`. It fetched nothing for these fields, nor for a bare
  `INCLUDEPICTURE`: its importer drops `INCLUDEPICTURE` and keeps
  `INCLUDETEXT` as a field it does not update. Word, or a later
  LibreOffice, may update them.
- LibreOffice's SVG reader takes an `image`'s `href` or `xlink:href`
  (by the attribute's name, whatever its prefix's namespace), resolves it
  against the document, and opens it as a file; a `data:` URL it decodes
  from base64 and imports by its bytes, whatever its media type [17].
  Converting a Word document to PDF, LibreOffice 24.2 drew a local PNG
  named by `file:///...` into the PDF: from an `image`, a `pattern`, a
  `feImage`; from an SVG named `image1.png` and typed a PNG; from one
  compressed with gzip; from one with no namespace; from one whose root
  was an `image`, with `<svg` in a comment after it; from an SVG in a
  `data:` URL of an SVG, typed `image/svg+xml` or `image/png`; and from
  one whose `<svg` came after a comment of 3,000 characters. `http:` it
  did not fetch, nor a relative path. 2.7 took every one of these files.
  A URL LibreOffice expands (`vnd.sun.star.expand:`) sent a server's
  environment to whoever named it (CVE-2024-12426) [18].
- An SVG loads from `href` and `xlink:href` on many elements (`image`,
  `use`, `feImage`, `pattern`, `a`) [19], and from CSS: `url()`,
  `@import`, and `image-set()`, which takes a URL as a string [20]. CSS
  escapes spell a name otherwise (`\75 rl(` is `url(`) [21].
- Word's mail merge reconnects to its source by `w:connectString`, an
  OLE DB connection string [22]; `w:query` names the file or table it
  reads, and `w:dataSource` and `w:odso` name it by relationship. Word
  opening a file on a share sends the user's NTLM hash to its host, as
  2.7's refusal of an outside relationship (a remote template) already
  says [23]. LibreOffice binds a merge only to a data source registered
  with it, and fetched nothing.
- A comment or an instruction before an XML part's root made 2.7's
  reader raise once the root was read: the part was refused as
  `unreadable_file`. A drawing program's SVG starts so (`<!-- Generator:
  ... -->`); LibreOffice 24.2's own export of a document holding one, a
  Word file, a workbook and a presentation, was refused.
- Excel's outside data is reached through `xl/connections.xml`, query
  tables, and links to other workbooks, all refused by type and by
  relationship since 2.5, Power Query and pivot caches through them; a
  presentation has no data source of its own. Nothing was added there.

### Options

How a field's name is read:

| Option | What it costs |
| --- | --- |
| Past a backslash at the start and quotes, as LibreOffice 24.2 does | Word's reading is not documented; a reader more lenient (a space before the backslash, curly quotes) passes. |
| **Past any white space, Unicode's default ignorable code points (zero-width space and joiners, byte order mark, soft hyphen, directional marks) [24], backslashes and quotes, straight, curly, low and guillemets; the letters that follow, those marks between them left out, in any case. A quote that closes a quoted name is no argument of `HYPERLINK`** | An instruction that starts with a quoted word in the list (`"LINK"`) is refused; no field Word writes does. |

Which SVG parts are read:

| Option | What it costs |
| --- | --- |
| Parts typed `image/svg+xml` | An SVG called `image1.png` passes, and LibreOffice draws it. |
| **Parts SVG by name or type, and every part read as XML (2.7's rule: by name or type, or named by a relationship and starting as XML) that holds `<svg` anywhere or an element `svg` of a prefix; a part a relationship names, or an SVG's, that is gzip, read inflated within the check's bytes (its header as RFC 1952 writes it [25], its checksum not read, as LibreOffice reads it [26]); broken deflated data refused** | A part that names `<svg` in a comment is read as an SVG. A font whose scrambled first bytes are a gzip header (about 1 in 130 million) is refused. |

What an SVG may load:

| Option | What it costs |
| --- | --- |
| `image`'s `href` alone | LibreOffice draws `pattern`s and `feImage`s; a browser opening the part loads the rest. |
| **Any attribute `href` or `src`, whatever its namespace, and any value `set` or `animate` gives one, names a fragment (`#id`) or a `data:` URL. CSS anywhere (an attribute, a `style` element, any text, its comments left out and escapes undone) has no `@import`, `image()` or `image-set()`, and `url()` and `src()` name a fragment or a `data:` URL. No `<?xml-stylesheet?>`. A `data:` URL, base64 or not, whose bytes start as XML and hold `<svg` is checked as an SVG, four deep; any other is a picture** | An SVG's link (`a href`) is refused, and text that reads `url(` in a label. A drawing program's SVG, its gradients and clips named by fragment and its pictures held in `data:` URLs, passes. |

How a mail merge is held:

| Option | What it costs |
| --- | --- |
| Refuse any `w:mailMerge`, as the project did | A letter begun as a merge, no recipients chosen, is refused. |
| **Refuse a `w:mailMerge` holding a `w:connectString`, `w:query`, `w:dataSource`, `w:headerSource`, or `w:odso`'s `w:udl` or `w:src`, with a value that is not blank** | A merge document with its source is refused, as one naming it by relationship was. |

### Decision

- `_check_field` reads the name as the second option says, for each
  instruction, with and without its tracked deletions, in complex fields
  and in `w:fldSimple`, against `OFFICE_FIELDS` and the `HYPERLINK` rule
  of 2.7.
- `OfficeCheck` reads SVG parts and what they load as the options in bold
  say, with the same parser as every part (no entities, no DTD, no
  network); an SVG's DTD or entity is refused in its bytes, a `data:`
  URL's too. A refusal is `EXTERNAL` (`unsafe_file`); an SVG in a
  `data:` URL that cannot be read, or nested deeper than four, is "too
  large or too complex".
- A `w:mailMerge` naming its source is refused as `EXTERNAL`.
- An XML part's comments and instructions before its root are read past.

### Consequences

- These are security fixes in a minor release: documents 2.7 took are
  refused (`docs/upgrading.md`, 2.7 to 2.8). The real documents tried, a
  table of contents with `PAGEREF`, `REF`, `SEQ`, `DATE`, `PAGE` fields
  and hyperlinks, LibreOffice's export of a Word file, a workbook and a
  presentation each holding an SVG, pass; Word's own SVG pictures name
  their PNG and SVG by relationship and load nothing.
- A large SVG costs more to check: 100,000 elements, each with a style
  naming a gradient, took 1.2 s where 2.7 took 0.4 s. A Word document of
  200,000 paragraphs took as long as on 2.7 (4.4 s).
- The check reads what LibreOffice and Word are known to follow. A
  browser that opens an SVG part on its own follows more than this reads
  (a script, HTML in a `foreignObject` but its `src`); serve files with
  `serve`, which sends them to download.

### Sources

15. LibreOffice, commit for tdf#54584, "field type may be prepended with
    backslash": <https://lists.freedesktop.org/archives/libreoffice-commits/2015-December/107093.html>
16. LibreOffice, `writerfilter/source/dmapper/DomainMapper_Impl.cxx`
    (`lcl_ExtractToken`, `splitFieldCommand`): <https://docs.libreoffice.org/writerfilter/html/DomainMapper__Impl_8cxx_source.html>
17. LibreOffice, `svgio/source/svgreader/svgimagenode.cxx`: <https://docs.libreoffice.org/svgio/html/svgimagenode_8cxx_source.html>
18. Codean Labs, "Exploiting LibreOffice (CVE-2024-12425 and
    CVE-2024-12426)": <https://codeanlabs.com/2025/02/exploiting-libreoffice-cve-2024-12425-and-cve-2024-12426/>
19. W3C, SVG 2, Linking (`href` on `a`, `image`, `use`, `feImage`,
    `pattern`): <https://www.w3.org/TR/SVG2/linking.html>
20. W3C, CSS Images Module Level 4, `image()` and `image-set()`:
    <https://www.w3.org/TR/css-images-4/>
21. W3C, CSS Syntax Module Level 3, escapes (4.3.7): <https://www.w3.org/TR/css-syntax-3/#consume-escaped-code-point>
22. ECMA-376, `connectString` (Data Source Connection String): <https://webapp.docx4java.org/OnlineDemo/ecma376/WordML/connectString.html>
23. Securify, "Living off the land: stealing NetNTLM hashes": <https://securify.nl/blog/living-off-the-land-stealing-netntlm-hashes/>
24. Unicode, `DerivedCoreProperties.txt`, Default_Ignorable_Code_Point:
    <https://www.unicode.org/Public/UCD/latest/ucd/DerivedCoreProperties.txt>
25. RFC 1952, GZIP file format specification (2.3): <https://www.rfc-editor.org/rfc/rfc1952.html>
26. LibreOffice, `tools/source/zcodec/zcodec.cxx` (`ZCodec::InitDecompress`):
    <https://docs.libreoffice.org/tools/html/zcodec_8cxx_source.html>

# 0176. Bound what a PDF decodes, list placeholders, fill drafts, and clean less

- Status: Accepted
- Date: 2026-11-17
- Amends: [0168](0168-render-documents-through-a-port-with-gotenberg-and-fill-them-locally.md)

## Context

The project that builds contract templates on `domainkit.documents`
([ADR 0168](0168-render-documents-through-a-port-with-gotenberg-and-fill-them-locally.md))
reported four gaps against 2.6 (U6, U5, U2). Each has a workaround in the
project.

- **`overlay_pdf` decodes a page's content whole (U6).** A PDF keeps a
  page's drawing in content streams, most often deflated. pypdf bounds
  each stream it decodes: 75 MB by default for each filter that
  inflates, through `pypdf.Configuration` [1]. The limits came after
  decompression bombs: a crafted PDF that exhausted memory, content
  streams included "on explicit access" (CVE-2025-55197, fixed in 6.0),
  and an LZW stream that could take 1 GB (CVE-2025-66019, fixed in 6.4)
  [2], [3]. They bound one stream, not what the streams of a file come to
  together, and pypdf leaves the rest to its caller [4].
  `overlay_pdf` decoded the content of every page it laid over, merged
  the overlay into it, and wrote the page back decoded. A PDF of 20
  pages, each 8 MiB of deflated spaces, is 170 KB: it became 168 MB on
  disk, and took 390 MB of memory, in the web worker. This is CWE-409,
  data amplification [5]. The project counts the pages' content itself,
  within 16 MiB, and runs previews under pypdf limits it sets to the
  same budget. `page_sizes`, called on the same file first, reads its
  object streams, where a PDF keeps its objects compressed, with the
  same per-stream bound only.
- **pypdf bounds four filters, and runs four in Python.** In the pypdf
  this release locks (6.21.4), a limit bounds what deflate, LZW,
  run-length, and Brotli decode to [1]; nothing bounds ASCIIHex (half
  what it reads), ASCII85 (four times, through `z`), or the image
  filters, which return what they read (DCT, JPX, CCITT) or run the
  `jbig2dec` program (JBIG2). LZW, run-length, ASCIIHex, ASCII85, and the
  predictors run in Python. Measured here, per MiB read: ASCII85 of `z`
  94 MiB of memory (under `tracemalloc`), and in time, without it,
  ASCII85 0.15 s, run-length up to 0.3 s, LZW 0.6 s, a Paeth predictor
  0.3 s. A review found that a budget on what a stream decodes to misses
  them: a 31 KB PDF whose content was deflate, then ASCII85, decoded 30
  MiB of `z`s within the budget and took 2.8 GB before it was refused.
  A second found that a bound on each stream misses many: a 33 MB file
  of 64 object streams of LZW, each under it, took 17 seconds a call. ASCII85 is no attack's alone:
  ReportLab, a common generator, armours its streams with it by default
  (its `useA85` setting), first, over deflate. A cross-reference stream
  listing 1.99 million entries took pypdf 26 seconds and 389 MiB as it
  opened a 20 KB file; pypdf's default allows 2 million [16]. pypdf
  follows every `/Prev` link of a chain of sections: 500 of them, each a
  stream of 1 MiB with a predictor, took 47 seconds.
- **pypdf's limits are per context.** pypdf keeps its configuration in a
  `ContextVar`, and `apply_configuration` sets it for a `with` block,
  resetting it on the way out [1], [6]. Each thread has its own context,
  so a context variable set in one is not seen in another, as with
  `threading.local()` [7]; `asyncio.to_thread` runs the function in a
  copy of the caller's context [8]. So a call can lower the limits for
  itself without touching other threads. Changing the module constants
  of `pypdf.filters`, which older advice gives, is global, and deprecated
  [1].
- **`renderer()` with nothing configured is a 500 (U5).** It raised
  `ImproperlyConfigured`, which no handler answers. Where rendering is
  optional (a preview, a developer's machine), the project reads
  `DOCUMENTS["RENDERER"]` before each call to answer 503 itself. A
  server that cannot handle a request for now answers 503 [9];
  `renderer_unavailable` is already the answer when Gotenberg is down.
- **`fill_docx` has no lenient mode (U5).** It refuses a placeholder
  without a value, a value without a placeholder (unless
  `allow_unused`), and braces that make no placeholder. A draft's
  preview, made to show what is left to fill or to fix, cannot be made:
  the project gives each missing name its own placeholder as a value,
  and answers 422 for stray braces. Jinja offers the same choice of
  policies: `StrictUndefined` refuses, `DebugUndefined` prints
  `{{ foo }}` as written [10].
- **The placeholders cannot be listed (U2).** `fill_docx` names them only
  by refusing, sorted, so their order is lost. An application that shows
  a template's fields reads the archive again, and must read it exactly
  as `fill_docx` will, or a preview fails on a placeholder it never
  listed. docxtpl offers `get_undeclared_template_variables`, a set of
  the names a template needs [11], as Jinja's `meta` does.
- **`clean_html` cannot keep less than its allowlist (U2).** It keeps
  typographic styles and inline images. An editor that formats with
  classes, as Quill does by default [12] and CKEditor 5 can [13], keeps
  alignment by class; its styles would let text be made tiny or the
  page's colour, which docs/documents.md says the cleaner does not stop.
  nh3 (ammonia) filters class names per tag, and refuses to run if the
  `class` attribute itself is allowed on that tag, so no class gets
  through by accident [14].

Options considered for U6:

1. **pypdf's limits lowered to a budget, nothing counted.** The project's
   own stopgap. Twenty streams under the limit each still come to twenty
   times it.
2. **Count every stream pypdf decodes, by wrapping its decoder.** Exact,
   but it patches a module of pypdf for every thread, which the per-call
   bound must not do.
3. **Decode what the call needs first, within a budget.** Before a
   page's content or a file's object streams are used, decode each,
   under pypdf's limits lowered to what is left, and count it; pypdf
   keeps what it decoded, so nothing is decoded twice. The cross-reference
   streams pypdf reads as it opens a file are not counted: they are
   decoded before any code of the caller runs, and stay bounded per
   stream.

For the filters pypdf does not bound, or runs in Python: refuse ASCII85
everywhere, as the review allowed, which refuses ReportLab's PDFs; or
read the filters before decoding and allow each where its input is
bounded. The second keeps what generators write.

Options considered for the draft: one flag, `draft=True`, that lets both
through; or one flag for each kind, beside `allow_unused`. One flag per
kind keeps the existing pattern, and lets a project allow stray braces
in text that may hold them while still asking a value for every
placeholder.

## Decision

Option 3 for PDFs, with the filters read first, and four additions to
`domainkit.documents`, all keyword arguments or new names: nothing that
worked in 2.6 is refused but a PDF past the new bounds.

- **`overlay_pdf(..., max_decoded=MAX_DECODED)`** and
  **`page_sizes(..., max_decoded=MAX_DECODED)`**, 32 MiB by default, the
  size of `ArchiveLimits.max_bytes`. Within the call, pypdf's limits on
  what one stream decodes (`zlib_`, `lzw_`, `run_length_`, `brotli_`,
  `jbig2_maximum_output_length`) are lowered to `max_decoded`, with
  `apply_configuration`, for that context only: a lower limit the caller
  set stays, and one it disabled (0) becomes `max_decoded`. Before it is
  used, each object stream of each PDF read, and the content of each page
  laid over and of its overlay, is decoded under the limits lowered again
  to what is left of the budget, and counted. Past it, the call is
  refused (`DocumentError`), as is a stream pypdf cannot decode, even an
  object stream nothing uses: skipped, each could cost what it decoded
  before it failed. A page laid over is written back deflated (pypdf's
  `compress_content_streams` [15]), so the result is about the size of
  the document, not of what was decoded. Memory peaks at about three
  times what is decoded: a page of 31 MiB peaked at 97 MiB.
- **A stream's `/Filter` and `/DecodeParms` are read before it is
  decoded**, and it is refused unread unless: each filter is deflate,
  LZW, run-length, ASCIIHex, ASCII85, or Brotli; one of the four run in
  Python at most, first, so that it reads the stream as stored, never
  what another filter inflated; and no filter has a predictor, which
  belongs to images and cross-reference streams. What the streams
  decoded in Python store is counted, per call, and bounded in all at
  `max_decoded / 32` (1 MiB), with LZW at `max_decoded / 512` (64 KiB),
  since its clear codes cost pypdf 11 seconds a MiB: about a second and
  100 MiB at worst, measured. `[/ASCII85Decode /FlateDecode]` is read;
  `[/FlateDecode /ASCII85Decode]` is refused in a millisecond, and so is
  the 64-stream file once two streams are decoded. The judgement is one
  function (`filters_refused`), and the per-call count one object
  (`DecodeBudget`), for an upload check to use too.
- **The cross-reference sections** are counted where pypdf reads them:
  a subclass of its reader (`bounded_reader`) counts each call of
  pypdf's own `_read_xref` and `_read_pdf15_xref_stream`, which a
  hybrid file's `/XRefStm` calls too, and refuses the one past 32 before
  reading it. 32: one for the file and one for each save added to it,
  two in a hybrid file, as Acrobat saves a form filled or signed. A
  first version read the links from the file's bytes, and a third
  review fooled it with `/P#72ev`, a comment, and a string holding
  `stream`: pypdf parses PDF, a regex does not. The methods are pypdf's
  private ones, so a test checks that each runs. The same subclass
  refuses to rebuild a damaged file's table (`_rebuild_xref_table`),
  which reads the whole file and decodes every object stream with
  whatever filters it has, outside any budget: 1000 of them took 65
  seconds, and the file was accepted. A fourth review found two more
  ways around the budget, and the subclass closes both where pypdf
  takes them: it reads no object from an object stream the budget has
  not decoded (`_get_object_from_stream`), so an object stream held in
  another, which PDF does not allow and which took a 31 KB file to 2.9
  GB, is refused, and checked before anything is decoded; and it
  decodes a cross-reference stream with `FlateDecode` only, read as
  pypdf caches the stream, before it decodes it (`cache_indirect_object`):
  1 MiB of LZW clear codes in one took 12 seconds. It also counts the
  searches pypdf makes of the whole file for an object its table lacks
  or misplaces, as copies of the file it reads from (`getbuffer`), and
  refuses the 33rd: 20,000 took 406 seconds. Their streams list
  65,536 entries in all, or one for each 512 bytes of `max_decoded`
  where that is more (`xref_stream_maximum_entries`), and each decodes
  to 8 bytes an entry at most while the file is opened, an entry of a
  file under 4 GB and its predictor's byte (512 KiB). Thirty-two
  sections, each as large and slow as that allows, took 3.8 seconds. A
  file past any of these is refused at once.
- **`renderer()` with no renderer raises `RendererNotConfigured`**, a
  `RendererUnavailable` and an `ImproperlyConfigured`: the HTTP layer
  answers it 503 `renderer_unavailable`, with the same detail, and code
  that caught `ImproperlyConfigured` still does. A 503 is never sent to
  `ERROR_REPORTERS`, and a missing setting where one should be is a
  deployment's mistake someone must see, as they saw the 500: so
  `renderer()` logs it as an error and reports it, with the request, the
  first time a process asks for a renderer, and raises it every time.
  Once a process, not once a call: a caller that catches it (a preview
  that answers without one), a worker's retries, and three calls in one
  request would each report it again. A process forked from it reports
  once more: its reporters are its own. Nothing is reported before the
  first call. Reporting it there rather than in
  the HTTP layer reports it from a worker too. No check is added: a project that
  renders nowhere leaves the setting unset on purpose.
- **`fill_docx(..., allow_missing=False, allow_malformed=False)`**: each
  leaves what it allows as written, a placeholder with no value or braces
  that make none. The defaults stay strict, and the docs say a draft is
  for a preview only.
- **`placeholders(document, limits=None) -> Placeholders`**: `names`,
  each once, in the order written, and `malformed`, the braces that make
  none as `PlaceholderError.malformed` quotes them. It runs the reading
  of `fill_docx` with no values: the same parts, paragraphs, grammar,
  XML parser, and `ArchiveLimits`. Both read the parts in a stable
  order: the body, the headers, the footers, the footnotes, then the
  endnotes, each kind by part name (`header2` before `header10`), not
  in the order the content types list them, which is the writer's (Word
  lists the notes before the headers). A typed pair
  rather than one tuple of names and stray text, so a caller never tells
  them apart by looking for braces.
- **`clean_html(..., styles=True, images=True, classes=None)`**: `styles`
  is True, False, or a set of the properties kept, which must be among
  those the cleaner keeps (a `ValueError` otherwise, and a `TypeError`
  for one string, whose letters would be read as names); `images=False` drops
  images; `classes` maps tags kept (or `*`) to the class names kept on
  them, plain CSS identifiers (a set, never one string), and drops every
  other. The only thing it
  adds is a class name the project listed: it loads and runs nothing, and
  means what the project's style sheet says.

## Consequences

- An application lays text over any PDF without a decompression bomb in
  its worker: what the call decodes is bounded in all, and so is what it
  writes. The project's own counting and its pypdf limits can go.
- A PDF whose object streams, or whose pages laid over, decode past 32
  MiB is refused where 2.6 laid it. Drawings and maps of many megabytes
  a page need `max_decoded` raised; the upgrade notes say so.
- PDFs whose content or objects use an image's filter, a stage run in
  Python after another filter, such stages on more than 1 MiB in all, or
  a predictor are refused, as are object streams that cannot be decoded,
  files of more than 65,536 cross-reference entries, files saved more
  than 31 times over (15 for a hybrid file), damaged files whose table
  pypdf would rebuild, object streams held in others, cross-reference
  streams decoded otherwise than with `FlateDecode`, and files that name
  more than 32 objects their table lacks. PDFs of the
  early 1990s used `[/ASCII85Decode /LZWDecode]`: they are refused too.
- `page_sizes` decodes every object stream of the file up front, where
  it decoded those of the page tree only; the work is bounded by the
  budget, and is the work `overlay_pdf` does anyway when it clones the
  file.
- Not counted in all: the parsing of the overlay into operators, which
  costs far more than its bytes (2 MiB of operators took 8 seconds and
  200 MiB, without a profiler). The overlay is the renderer's output
  from the project's own HTML; the docs say never to pass a PDF a
  person sent. Uploads are checked whole before they are stored (ADR
  0167).
- An unset renderer is a 503 in every request that needs one, logged as
  an error and reported once a process. A project's handler for `ImproperlyConfigured`
  no longer sees it (`UpstreamError` comes first among its classes), and
  `except RendererUnavailable` now catches a missing setting too.
- A draft filled with `allow_missing` or `allow_malformed` holds braces.
  Nothing marks it as a draft: the project keeps it from being sent.
- `PlaceholderError.malformed` lists the braces in the stable order of
  the parts, where it followed the content types' order.

## References

1. pypdf, "Configuration":
   <https://pypdf.readthedocs.io/en/stable/modules/configuration.html>
2. CVE-2025-55197, pypdf's memory exhausted by a crafted PDF:
   <https://osv.dev/vulnerability/CVE-2025-55197>
3. CVE-2025-66019, pypdf's LZW output limit:
   <https://osv.dev/vulnerability/CVE-2025-66019>
4. pypdf, "Security" (limits through `apply_configuration`, kept in
   context variables; catching errors left to the caller):
   <https://pypdf.readthedocs.io/en/stable/user/security.html>
5. CWE-409, Improper Handling of Highly Compressed Data (Data
   Amplification): <https://cwe.mitre.org/data/definitions/409.html>
6. pypdf, `pypdf._configuration` (`CURRENT_CONFIGURATION`, a
   `ContextVar`):
   <https://pypdf.readthedocs.io/en/stable/_modules/pypdf/_configuration.html>
7. Python, `contextvars`: <https://docs.python.org/3/library/contextvars.html>
8. Python, `asyncio.to_thread`:
   <https://docs.python.org/3/library/asyncio-task.html#running-in-threads>
9. RFC 9110, 503 Service Unavailable:
   <https://www.rfc-editor.org/rfc/rfc9110#name-503-service-unavailable>
10. Jinja, `StrictUndefined` and `DebugUndefined`:
    <https://jinja.palletsprojects.com/en/stable/api/#undefined-types>
11. docxtpl, `get_undeclared_template_variables`:
    <https://docxtpl.readthedocs.io/en/latest/>
12. Quill, "Customization" (classes rather than inline styles):
    <https://quilljs.com/docs/customization>
13. CKEditor 5, "Text alignment" (alignment by class):
    <https://ckeditor.com/docs/ckeditor5/latest/features/text-alignment.html>
14. ammonia, `Builder::allowed_classes`:
    <https://docs.rs/ammonia/latest/ammonia/struct.Builder.html#method.allowed_classes>
15. pypdf, "Reduce file size":
    <https://pypdf.readthedocs.io/en/stable/user/file-size.html>
16. pypdf, `PdfReader` (`xref_stream_maximum_entries`, 2,000,000 by
    default): <https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html>

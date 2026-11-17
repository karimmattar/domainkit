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

Options considered for the draft: one flag, `draft=True`, that lets both
through; or one flag for each kind, beside `allow_unused`. One flag per
kind keeps the existing pattern, and lets a project allow stray braces
in text that may hold them while still asking a value for every
placeholder.

## Decision

Option 3 for PDFs, and four additions to `domainkit.documents`, all
keyword arguments or new names: nothing that worked in 2.6 is refused
but a PDF that decodes past the new default.

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
  refused (`DocumentError`). An object stream pypdf cannot decode is
  skipped, to fail only where it is used, as before. A page laid over is
  written back deflated (pypdf's `compress_content_streams` [15]), so the
  result is about the size of the document, not of what was decoded.
- **`renderer()` with no renderer raises `RendererNotConfigured`**, a
  `RendererUnavailable` and an `ImproperlyConfigured`: the HTTP layer
  answers it 503 `renderer_unavailable`, with the same detail, and code
  that caught `ImproperlyConfigured` still does. It logs a warning, as
  the adapter logs Gotenberg's failures. No check is added: a project
  that renders nowhere leaves the setting unset on purpose.
- **`fill_docx(..., allow_missing=False, allow_malformed=False)`**: each
  leaves what it allows as written, a placeholder with no value or braces
  that make none. The defaults stay strict, and the docs say a draft is
  for a preview only.
- **`placeholders(document, limits=None) -> Placeholders`**: `names`,
  each once, in the order written, and `malformed`, the braces that make
  none as `PlaceholderError.malformed` quotes them. It runs the reading
  of `fill_docx` with no values: the same parts, paragraphs, grammar,
  XML parser, and `ArchiveLimits`. Both read the body first, then the
  other parts in the order their content types list them. A typed pair
  rather than one tuple of names and stray text, so a caller never tells
  them apart by looking for braces.
- **`clean_html(..., styles=True, images=True, classes=None)`**: `styles`
  is True, False, or a set of the properties kept, which must be among
  those the cleaner keeps (a `ValueError` otherwise); `images=False` drops
  images; `classes` maps tags kept (or `*`) to the class names kept on
  them, plain CSS identifiers, and drops every other. The only thing it
  adds is a class name the project listed: it loads and runs nothing, and
  means what the project's style sheet says.

## Consequences

- An application lays text over any PDF without a decompression bomb in
  its worker: what the call decodes is bounded in all, and so is what it
  writes. The project's own counting and its pypdf limits can go.
- A PDF whose object streams, or whose pages laid over, decode past 32
  MiB is refused where 2.6 laid it. Drawings and maps of many megabytes
  a page need `max_decoded` raised; the upgrade notes say so.
- `page_sizes` decodes every object stream of the file up front, where
  it decoded those of the page tree only; the work is bounded by the
  budget, and is the work `overlay_pdf` does anyway when it clones the
  file.
- The chain of cross-reference streams pypdf reads as it opens a file is
  bounded per stream only. Uploads are checked whole before they are
  stored (ADR 0167).
- An unset renderer is a 503 in a request, logged as a warning each
  time.
- A draft filled with `allow_missing` or `allow_malformed` holds braces.
  Nothing marks it as a draft: the project keeps it from being sent.
- `PlaceholderError.malformed` lists the body's braces first, where it
  followed the content types' order.

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

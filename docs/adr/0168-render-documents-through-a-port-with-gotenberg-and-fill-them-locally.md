# 0168. Render documents through a port with Gotenberg, and fill them locally

- Status: Accepted
- Date: 2026-11-16

## Context

A project that builds contract templates reported (U2) that domainkit
has storage, uploads, scanning, and mail templates, but nothing that
turns HTML or a Word document into a PDF, fills the placeholders of a
Word document, or lays text over a PDF's pages. It wrote each part
itself: a renderer client on Gotenberg 8 (Chromium for HTML, LibreOffice
for Word), run merging in WordprocessingML, PDF page geometry, and an
HTML allowlist on nh3. Each is easy to get subtly wrong:

- **Word splits text into runs at will.** A run is "a region of text
  with a common set of properties" [1]; a spelling mark, an edit, or a
  change of language starts a new one, so `{{client}}` is often stored
  in three. docxtpl, the common Python tool, asks that a tag stay "inside
  the same run", and escapes nothing by default [2].
- **Word's XML is untrusted input** when a user uploads the template.
  OWASP says to disable DTDs, and to reject a DOCTYPE where the parser
  can [3]. lxml told not to load a DTD or resolve entities still opens
  an outside DTD subset, or an outside parameter entity, as it parses
  the DOCTYPE: a review found `file:///dev/stdin` and
  `file:///proc/self/fd/1` hang a parse for good. A `.docx` is a ZIP:
  Apache POI refuses an entry that inflates past 100 times its size (a
  minimum ratio of 1%) [4]. Python's `ZipFile.read` asks the
  decompressor for up to 2 GiB at once and cuts to the stated size only
  after, so a file that states 10 bytes is inflated whole: a review's
  1.9 KB bzip2 bomb took the process to 3 GiB.
- **A rotated page shifts an overlay.** `/Rotate` turns the page as
  shown; content is drawn in the unrotated space, from the crop box,
  which need not start at the origin. pypdf's merge ignores both, and its
  docs suggest `transfer_rotation_to_content()` on the page first [5], [6],
  which rewrites the page's content and boxes.
- **Arabic needs shaping**: turning code points into "a properly
  arranged sequence of glyphs" [7]. HarfBuzz does it in Chrome and in
  LibreOffice [8]; pypdf lays out no text.
- **The renderer fetches what a page links.** Gotenberg "reaches any
  address its network reaches" and asks for no credentials out of the
  box [9]. Its flags harden it: basic auth and TLS (`--api-enable-basic-auth`,
  `--api-tls-cert-file`), `--chromium-disable-javascript`,
  `--chromium-deny-list` (by default `^file:(?!//\/tmp/).*`),
  `--chromium-allow-list`, `--chromium-deny-private-ips` (8.32),
  `--api-disable-download-from`, `--webhook-disable` [9], [10], [11]. Two
  bypasses of its deny lists were fixed in 8.31 (CVE-2026-40280,
  CVE-2026-42596) [12], [13]; 8.32 reverted 8.31's strict defaults and
  made private-address checks opt-in [14]. The flags are set where it
  runs; its API does not show them.
- **Gotenberg 9.0.0** came out on 5 November 2026, with 8.40.0. It
  moves post-processing fields to `stages` and renames some fields
  (`nativePageRanges` to `nativePages`, `waitWindowStatus` to
  `waitForWindowStatus`); a Gotenberg 8 field it no longer takes is a 400
  [15], [16]. The page fields (`paperWidth`, the margins, `landscape`,
  `scale`, `printBackground`, `preferCssPageSize`,
  `failOnResourceLoadingFailed`), the routes `/forms/chromium/convert/html`
  and `/forms/libreoffice/convert`, `files`, and the `Gotenberg-Trace`
  header are the same in both [10], [17], [18], [19].

Options considered:

1. **Leave it to projects**, as 2.4 did: each writes the four parts again.
2. **A local renderer** (WeasyPrint, or a headless browser in the
   process). WeasyPrint is not a browser, Word documents still need an
   office suite, and a browser in every web process is heavy and runs
   untrusted pages beside the application's secrets.
3. **A `Renderer` port, a Gotenberg adapter, and local filling.** The
   service that renders runs apart, behind its own flags; filling,
   overlaying, and cleaning are pure functions on bytes.

## Decision

Option 3: a new package, `domainkit.documents`, behind a new extra,
`documents` (pypdf, lxml, nh3); each function imports its library when
called and says which extra to install when it is missing.

- **`Renderer`**, a protocol: `html_to_pdf(html, assets=, page=)` and
  `office_to_pdf(document, filename=)`. `GotenbergRenderer`
  (`domainkit.documents.gotenberg`) posts a multipart form through
  `domainkit.http.client`: timeouts, the deadline, retries (a rendering
  changes nothing, so its POST is retried), and a circuit. It sends only
  the fields Gotenberg 8 and 9 both take, the request's id as
  `Gotenberg-Trace`, and asks it to fail on a resource that fails to
  load. 400, 409, and 413 are `RenderingRefused`; a failure, an
  unreachable server, or a refused login is `RendererUnavailable` (503
  `renderer_unavailable`).
- **`DOCUMENTS = {"RENDERER": {...}}`**: `URL`, `USERNAME`, `PASSWORD`,
  `TIMEOUT` (30 seconds, Gotenberg's own), `TRACE_HEADER`,
  `ALLOW_HTTP`. `renderer()` makes the configured one. Staging and
  production refuse plain http to a host other than this one
  (`documents.E001`), unless `ALLOW_HTTP` says the network encrypts (a
  service mesh); they warn without a login (`documents.W001`); and
  `check` says, as information (`documents.I001`), that Gotenberg's
  flags cannot be seen, naming those to set: Chromium's and LibreOffice's
  alike (`--chromium-deny-private-ips`, `--libreoffice-deny-private-ips`,
  a LibreOffice deny or allow list), since LibreOffice fetches what a
  document links too.
- **`fill_docx(document, values)`** fills `{{ name }}` (letters of any
  script, digits, `_`, dotted parts) across the runs of each paragraph
  of the body, tables, text boxes, headers, footers, footnotes, and
  endnotes. Each value goes into the run where its placeholder starts.
  Values are text, escaped as written; line breaks and tabs become
  Word's. The policy is strict and reports everything at once
  (`PlaceholderError`): a placeholder without a value, a value without a
  placeholder (unless `allow_unused`), and braces that make no
  placeholder. A part that declares a DTD or an entity is refused before
  it is parsed; lxml then reads UTF-8 only, without entities, DTDs, or
  the network, and resolves any outside reference to nothing. Each
  paragraph's text is gathered in one walk, so nesting costs nothing.
  Prefixes are kept. The archive is read within `ArchiveLimits`: its
  stated sizes and POI's ratio are checked first, then each file is
  inflated no further than its stated size. Files are stored or deflated,
  and named by plain relative paths, each name once.
- **`overlay_pdf(document, overlay)`** lays page *i* of a rendered PDF
  (or one page per index of a mapping) over page *i*, as shown: the
  overlay is mapped from its shown coordinates to the page's own, by the
  page's rotation and its crop box within its media box, top-left corner
  on top-left corner. The page's content and annotations are left as
  they were. `page_sizes` gives each page's shown size, for the HTML's
  `@page` rules. An overlay more than 2 points off its page's size is
  refused.
- **`clean_html(html, links=False)`** keeps rich text to an allowlist on
  nh3: formatting, direction, inline raster images as data; no script,
  style sheet, frame, form, SVG, handler, `class`, `id`, URL to fetch, or
  CSS `url()`; style values that are plain and not negative,
  `vertical-align` by keyword, `line-height` at least 1. nh3 writes a
  style back as its CSS parser read it, so the text is cleaned twice and
  the second pass checks what the first wrote. It is the
  first layer; the renderer's flags are the second, since a sanitizer
  parses HTML its own way, the template around it is the project's, and
  only the browser that would fetch can refuse for sure.

Arabic reaches a PDF through HTML or Word, rendered by Chromium or
LibreOffice; nothing here draws text.

## Consequences

- A project configures a renderer and calls four functions; the run
  merging, the page geometry, and the allowlist are tested once, here.
- The adapter works with Gotenberg 8.40 and 9. A field that only one of
  them takes (page ranges, PDF/A) is not offered; a project that needs
  one writes its own `Renderer`.
- The deployment checks cannot see Gotenberg's flags: the docs list
  them ("Hardening Gotenberg"), and `check` says they are unseen. A
  deployment that leaves JavaScript on, or private addresses reachable,
  passes `check`.
- `fill_docx` and `overlay_pdf` are synchronous and CPU-bound; the docs
  say to run them with `asyncio.to_thread` in a request.
- U1 (uploads checked by their first bytes) is separate: these functions
  bound what they read, but they do not inspect an upload for macros or
  scripts.

## References

1. ISO/IEC 29500, quoted in Microsoft's "Working with runs":
   <https://learn.microsoft.com/en-us/office/open-xml/word/working-with-runs>
2. docxtpl's documentation: <https://docxtpl.readthedocs.io/en/latest/>
3. OWASP, XML External Entity Prevention Cheat Sheet:
   <https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html>
4. Apache POI, `ZipSecureFile`:
   <https://poi.apache.org/apidocs/dev/org/apache/poi/openxml4j/util/ZipSecureFile.html>
5. pypdf, "Cropping and transforming PDFs":
   <https://pypdf.readthedocs.io/en/stable/user/cropping-and-transforming.html>
6. pypdf, "Adding a stamp or watermark":
   <https://pypdf.readthedocs.io/en/stable/user/add-watermark.html>
7. HarfBuzz, "What is text shaping?":
   <https://harfbuzz.github.io/what-is-harfbuzz.html>
8. HarfBuzz's README (its users): <https://github.com/harfbuzz/harfbuzz>
9. Gotenberg, "Secure your deployment" and "Restrict outbound
   requests": <https://gotenberg.dev/docs/operate/secure>,
   <https://gotenberg.dev/docs/operate/outbound>
10. Gotenberg 8, configuration (its flags and their defaults):
    <https://gotenberg.dev/docs/8.x/configuration>
11. Gotenberg, configuration: <https://gotenberg.dev/docs/configuration>
12. CVE-2026-40280: <https://osv.dev/vulnerability/CVE-2026-40280>
13. CVE-2026-42596: <https://osv.dev/vulnerability/CVE-2026-42596>
14. Gotenberg 8.32.0's release notes:
    <https://newreleases.io/project/github/gotenberg/gotenberg/release/v8.32.0>
15. Gotenberg 9.0.0's release notes:
    <https://github.com/gotenberg/gotenberg/releases/tag/v9.0.0>
16. Gotenberg, "Upgrade from 8.x":
    <https://gotenberg.dev/docs/getting-started/upgrade>
17. Gotenberg 8, "Convert HTML to PDF":
    <https://gotenberg.dev/docs/8.x/convert-with-chromium/convert-html-to-pdf>
18. Gotenberg 8, LibreOffice "Convert to PDF":
    <https://gotenberg.dev/docs/8.x/convert-with-libreoffice/convert-to-pdf>
19. Gotenberg, headers (`Gotenberg-Trace`, `Authorization`):
    <https://gotenberg.dev/docs/reference/headers>

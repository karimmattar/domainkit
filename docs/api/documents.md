# Public API: documents

Documents (`domainkit.documents`): HTML and Office documents turned into
PDFs by a renderer, placeholders filled in Word documents, rendered pages
laid over a PDF's, and rich text kept to an allowlist. Part of the
[public API](../api.md), under its compatibility policy. The guide is
[documents.md](../documents.md).

## `domainkit.documents`

Documents: HTML and Office documents to PDF, Word placeholders, PDF overlays.

- `ArchiveLimits`: How much of a Word document is read: a zip bomb is refused within these.
- `DocumentError`: A document, or what was given to fill it, cannot be used.
- `PageOptions`: How a browser prints HTML; None leaves the renderer's default.
- `PlaceholderError`: A Word document's placeholders and the values given do not agree.
- `Renderer`: Turns HTML, or an Office document, into a PDF (`GotenbergRenderer`).
- `RendererUnavailable`: The renderer could not render now: unreachable, failing, or refusing the login.
- `RenderingRefused`: The renderer refused what it was sent: the document cannot be rendered.
- `clean_html`: `html`, a fragment of rich text, kept to the module's allowlist.
- `fill_docx`: `document` (a .docx) with each `{{ name }}` replaced by `values[name]`.
- `overlay_pdf`: `document` with overlays drawn over its pages.
- `page_sizes`: Each page's width and height as it is shown, in points (1/72 inch).
- `renderer`: The `DOCUMENTS["RENDERER"]` Gotenberg server, made once per configuration.

## `domainkit.documents.gotenberg`

The renderer on Gotenberg: HTML by Chromium, Office documents by LibreOffice.

- `CHROMIUM_HTML`: Gotenberg's route that prints `index.html` with Chromium.
- `LIBREOFFICE`: Gotenberg's route that converts Office documents with LibreOffice.
- `GotenbergRenderer`: A `Renderer` on a Gotenberg 8 or 9 server, with basic auth if it asks.

"""Documents: HTML and Office documents to PDF, Word placeholders, PDF overlays.

::

    from domainkit.documents import clean_html, fill_docx, overlay_pdf, renderer

    body = clean_html(contract.terms)  # rich text a user wrote
    pdf = await renderer().html_to_pdf(template.render(body=body))
    filled = await asyncio.to_thread(fill_docx, template, {"client": "Salma"})
    pdf = await renderer().office_to_pdf(filled)

- ``Renderer`` is the port that turns HTML, or an Office document, into a
  PDF; ``renderer()`` gives the one ``DOCUMENTS["RENDERER"]`` names, on
  Gotenberg (``domainkit.documents.gotenberg``, ``domainkit[httpclient]``).
- ``fill_docx`` fills ``{{ name }}`` placeholders in a Word document,
  wherever Word split them into runs; ``placeholders`` lists them as it
  reads them.
- ``overlay_pdf`` lays rendered pages over a PDF's pages, rotated and
  cropped pages too; ``page_sizes`` gives the size to render each at.
  Both decode no more than ``max_decoded`` bytes.
- ``clean_html`` keeps rich text to an allowlist that loads and runs
  nothing, before it goes to the renderer, or to less of it.

Text in Arabic, or any script whose letters join or that runs right to
left, is shaped by a layout engine: Chromium and LibreOffice do it
(HarfBuzz), pypdf does not. So such text reaches a PDF through HTML or a
Word document, rendered, never drawn on a page here. ``fill_docx``,
``placeholders``, ``overlay_pdf``, ``page_sizes``, and ``clean_html`` need
``domainkit[documents]`` (pypdf, lxml, nh3), and say so when it is
missing. See docs/documents.md, ADR 0168, and ADR 0176.
"""

from domainkit.documents.base import (
    DocumentError,
    PageOptions,
    PlaceholderError,
    Renderer,
    RendererNotConfigured,
    RendererUnavailable,
    RenderingRefused,
    renderer,
)
from domainkit.documents.docx import (
    ArchiveLimits,
    Placeholders,
    fill_docx,
    placeholders,
)
from domainkit.documents.html import clean_html
from domainkit.documents.pdf import overlay_pdf, page_sizes

__all__ = [
    "ArchiveLimits",
    "DocumentError",
    "PageOptions",
    "PlaceholderError",
    "Placeholders",
    "Renderer",
    "RendererNotConfigured",
    "RendererUnavailable",
    "RenderingRefused",
    "clean_html",
    "fill_docx",
    "overlay_pdf",
    "page_sizes",
    "placeholders",
    "renderer",
]

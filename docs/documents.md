# Documents

Contracts, invoices, and certificates are documents a person reads and
signs. A service makes them from templates: a page of HTML, a Word
document with placeholders, or a PDF that text is laid over. Each step is
easy to get subtly wrong. Word splits a placeholder into pieces at will,
a rotated page moves every box, Arabic letters must be joined by a real
layout engine, and the renderer that prints a page will also fetch what
the page links. `domainkit.documents` does these steps. *New in 2.5.*

On this page you learn how to:

- configure the renderer, a Gotenberg server, and harden it;
- turn HTML and Word documents into PDFs;
- fill the placeholders of a Word document;
- lay rendered text over the pages of a PDF;
- clean rich text that a person wrote before it is rendered;
- write documents in Arabic.

`fill_docx`, `overlay_pdf`, `page_sizes`, and `clean_html` need the
`documents` extra (pypdf, lxml, and nh3); the renderer needs the
`httpclient` extra. Each says which extra it needs when it is missing:

```bash
pip install "domainkit[documents,httpclient]"
```

The decisions are in
[ADR 0168](adr/0168-render-documents-through-a-port-with-gotenberg-and-fill-them-locally.md)
and
[ADR 0176](adr/0176-bound-what-a-pdf-decodes-list-placeholders-fill-drafts-and-clean-less.md).

## A first example

The settings name the Gotenberg server:

```python
DOCUMENTS = {
    "RENDERER": {
        "URL": "https://gotenberg.internal:3000",
        "USERNAME": "shop",
        "PASSWORD": env.str("GOTENBERG_PASSWORD"),
    },
}
```

A use case fills a lease from a Word template, and converts it to PDF:

```python
import asyncio

from domainkit.documents import fill_docx, renderer


async def lease_pdf(template: bytes, tenant: str, rent: str) -> bytes:
    filled = await asyncio.to_thread(
        fill_docx, template, {"tenant": tenant, "monthly_rent": rent}
    )
    return await renderer().office_to_pdf(filled, filename="lease.docx")
```

The template says `Rent of {{ monthly_rent }} is due from {{ tenant }}.`,
however Word stored it. A placeholder with no value, a value with no
placeholder, or a misspelled placeholder is refused with a
`PlaceholderError` that lists every problem at once. So no lease goes out
with `{{tenant}}` in it.

## The renderer

`renderer()` returns the renderer that `DOCUMENTS["RENDERER"]` names: a
`GotenbergRenderer`, made once per configuration. A `Renderer` has two
methods:

- `html_to_pdf(html, assets=..., page=...)` prints HTML with Chromium.
  `assets` maps plain file names to bytes: the images, fonts, and style
  sheets the HTML links by name (`<img src="logo.png">`). `header.html`
  and `footer.html` are printed at the top and the bottom of each page,
  as Gotenberg does. `page` is a `PageOptions`: the paper's size and
  margins (`"210mm"`, `"8.5in"`, `"72pt"`), `landscape`, `scale`,
  `print_background` (on), and `prefer_css_page_size` (on, so an
  `@page` rule of the HTML sets the size).
- `office_to_pdf(document, filename=...)` converts a Word, Excel,
  PowerPoint, or OpenDocument file with LibreOffice. The extension of
  `filename` names the kind: `docx`, `doc`, `odt`, `rtf`, `xlsx`, `xls`,
  `ods`, `pptx`, `ppt`, or `odp`. The kinds named for macros (`docm`,
  `xlsm`, ...) are refused, but `doc`, `xls`, and `ppt` may hold macros
  too: what keeps them from running is Gotenberg's
  `--libreoffice-enable-macros`, off by default. Leave it off.

```python
from domainkit.documents import PageOptions, renderer

pdf = await renderer().html_to_pdf(
    page_html,
    assets={"logo.png": logo, "NotoNaskhArabic.ttf": font},
    page=PageOptions(width="210mm", height="297mm", margin_top="2cm"),
)
```

Each rendering is one call through `domainkit.http.client`:

- bounded by `TIMEOUT` (30 seconds) and by the deadline of the request
  it serves;
- retried after a network error, or a 502, 503, or 504 answer, since
  rendering changes nothing;
- refused at once while Gotenberg's circuit is open, after five failures
  in a row (`HTTP_CLIENT`'s `BREAKER_FAILURES`);
- sent with the request's id in `Gotenberg-Trace`, which Gotenberg logs;
  an error names the id Gotenberg answered with, so its log line can be
  found;
- read up to `HTTP_CLIENT`'s `MAX_RESPONSE_BYTES` (10 MiB). Raise it if
  your PDFs are larger.

A page that links something that fails to load for a network error is
refused (409), not printed without it: the form asks Gotenberg to fail on
such a resource (`failOnResourceLoadingFailed`; Gotenberg 9 leaves out
timeouts and certificate errors).

The adapter uses only the routes and form fields that Gotenberg 8 and
Gotenberg 9 both take, so it works with either. Run Gotenberg 8.40 or
later, or 9.

### Hardening Gotenberg

Gotenberg converts what it is sent and fetches what a page links, for
anyone who reaches it. Out of the box it asks for no login and reaches
every address its network reaches. Its flags change that, and they are
set where Gotenberg runs. domainkit cannot check them: Gotenberg's API
does not show its configuration. So in staging and production `check`
says it, as information (`documents.I001`), and names the flags. Start
Gotenberg with these:

| Flag | Why |
| --- | --- |
| `--api-enable-basic-auth`, with `GOTENBERG_API_BASIC_AUTH_USERNAME` and `GOTENBERG_API_BASIC_AUTH_PASSWORD` | Only callers that know the password render. Set the same as `USERNAME` and `PASSWORD`; staging and production warn without them (`documents.W001`). |
| `--api-tls-cert-file`, `--api-tls-key-file` | Gotenberg serves HTTPS: documents and the password are encrypted on the way. Staging and production refuse an `http://` URL to another host (`documents.E001`). |
| `--chromium-disable-javascript` | The scripts of a page do not run. The pages you render need none. |
| `--chromium-deny-private-ips` (8.32 and later) | A page cannot reach the private network: the cloud's metadata service, a database, an admin page. |
| `--chromium-deny-list`, kept as it is | Its default, `^file:(?!//\/tmp/).*`, keeps a page from reading the server's files. Keep it if you add to it. |
| `--chromium-allow-list` | Where every asset is sent with the page, an allow list of Gotenberg's working directory (`^file:///tmp/.*`) keeps pages off the network. Try it with your templates. |
| `--libreoffice-deny-private-ips` (8.32 and later) | A document converted by LibreOffice, which may link an image or a template, cannot reach the private network either. |
| `--libreoffice-deny-list` or `--libreoffice-allow-list` | What LibreOffice may fetch, by URL: deny `^https?://` where documents need nothing from outside, or allow only the hosts they do. Gotenberg's docs do not say that the lists cover a `file:` link, so refuse uploaded documents that link outside files. |
| `--api-disable-download-from` | No form makes Gotenberg download a URL. |
| `--webhook-disable` | No form makes Gotenberg send the result to a URL. |
| `--api-body-limit` | Bodies past a size (`10MB`) are refused before they are read. |

Leave these unset: `--api-enable-debug-route`, which shows the
configuration; `--libreoffice-enable-macros`, which runs a document's
macros; `--chromium-shared-browser-context`, which lets one conversion's
cookies and cache reach the next; and
`--chromium-allow-file-access-from-files`,
`--chromium-ignore-certificate-errors`, `--chromium-disable-web-security`,
and `--chromium-proxy-server`, which weaken Chromium's isolation.
`--chromium-disable-routes` and `--libreoffice-disable-routes` remove
the routes you do not use.

Run 8.40 or later. 8.31 fixed two ways around the deny lists of
`downloadFrom` and webhooks (CVE-2026-40280, CVE-2026-42596); 8.32 added
the `*-deny-private-ips` flags; 8.39 and 8.40 fixed more. In Compose:

```yaml
gotenberg:
  image: gotenberg/gotenberg:8.40.0
  command:
    - gotenberg
    - --api-enable-basic-auth
    - --api-tls-cert-file=/run/secrets/gotenberg.crt
    - --api-tls-key-file=/run/secrets/gotenberg.key
    - --chromium-disable-javascript
    - --chromium-deny-private-ips
    - --libreoffice-deny-private-ips
    - --libreoffice-deny-list=^https?://
    - --api-disable-download-from
    - --webhook-disable
    - --api-body-limit=20MB
  environment:
    GOTENBERG_API_BASIC_AUTH_USERNAME: shop
    GOTENBERG_API_BASIC_AUTH_PASSWORD: ${GOTENBERG_PASSWORD}
```

Where Gotenberg runs beside the service, on `localhost`, the URL may be
`http://`. In a network that encrypts by itself, a service mesh with
mutual TLS, set `ALLOW_HTTP = True`. If Gotenberg names the request's id
in another header, set `TRACE_HEADER` to it: its
`--api-correlation-id-header` (8.29 and later) replaced
`--api-trace-header`, which Gotenberg 9 removed. Gotenberg's own guide is
[Secure your deployment](https://gotenberg.dev/docs/operate/secure).

## Filling a Word document

`fill_docx(document, values)` takes a `.docx` and a mapping of names to
text, and returns the filled `.docx`:

- A placeholder is a name between double braces: `{{ tenant }}` or
  `{{tenant}}`. A name has letters (Arabic ones too), digits, and `_`, and
  starts with a letter or `_`. Dots join parts: `{{ tenant.name }}`.
- Word splits a paragraph's text into runs at will: a spelling mark, an
  edit, a change of language. `fill_docx` reads each paragraph across its
  runs. The value goes into the run where its placeholder starts, so it
  takes that run's formatting; the text around it keeps its own runs.
- The body, its tables, and its text boxes are filled, and so are the
  headers, footers, footnotes, and endnotes. A placeholder lives in one
  paragraph.
- A value is text: format numbers, dates, and money first, in the
  reader's language. `<`, `&`, and quotes are written as text, never as
  markup. A line break in a value becomes a line break in Word, and a tab
  a tab. A control character is refused.
- Every placeholder needs a value, and every value a placeholder. With
  `allow_unused=True`, values that no placeholder takes are fine, for one
  set of values that serves several templates. `{{` and `}}` are kept for
  placeholders: braces that do not make one, such as `{{ tenant name }}`,
  are refused.

Every part of the document but the filled ones is written back as it
was, in its order. The XML is read as UTF-8 with lxml (a part in UTF-16
or UTF-32 is refused, as Word writes UTF-8), without entities,
a DTD, or anything outside the part. A part that declares a DTD or an
entity is refused before it is parsed, since Word writes none: a
parser told not to load a DTD still opens an outside DTD subset or
parameter entity, which may be a file, or a pipe that never ends. The
parser also resolves any outside reference to nothing. The namespace
prefixes are kept, since Word reads `mc:Ignorable` by prefix. The text
is read in one walk of each part, so paragraphs nested in text boxes,
however deep, cost no more than the rest.

The archive is read within `ArchiveLimits`, so a zip bomb costs nothing:

| `ArchiveLimits` field | Default | Bounds |
| --- | --- | --- |
| `max_bytes` | 32 MiB | The document as given. |
| `max_parts` | 1000 | Files in the archive. |
| `max_part_bytes` | 64 MiB | One file, inflated. |
| `max_total_bytes` | 256 MiB | Every file, inflated. |
| `max_ratio` | 100 | A file past 100 KiB may inflate at most this many times its stored size, as Apache POI allows. |

Sizes are checked as the archive states them, before anything is
inflated. Then each file is inflated no further than its stated size, so
a file that states 10 bytes and inflates to gigabytes costs 10 bytes: it
fails its checksum or is cut to them. The count of files, and the size
of the list of them (1 KiB a file at most), are read from the end of the
archive before anything else. Files are stored or deflated, as Word writes
them; other methods (bzip2, LZMA) are refused. Each file is named by a
plain relative path (no `/` first, no `..`, `\`, or `:`), and no two
names are the same once case and Unicode forms are set aside.

`fill_docx` works on the CPU: in a request, run it in a thread with
`asyncio.to_thread`, as the first example does.

## Laying text over a PDF

A person uploads a contract as a PDF, and the service writes names and
dates onto its pages. pypdf moves drawings, but it does not lay out text,
so the text is rendered first, as a PDF of its own, by the browser:

1. `page_sizes(pdf)` gives each page's size as it is shown, in points.
2. The HTML has one page per page, of that size, with the text placed in
   it.
3. The renderer prints it: the overlay.
4. `overlay_pdf(pdf, overlay)` lays the overlay's page *i* over the
   document's page *i*.

```python
import asyncio

from domainkit.documents import PageOptions, overlay_pdf, page_sizes, renderer


async def signed_names(contract: bytes, name: str) -> bytes:
    sizes = await asyncio.to_thread(page_sizes, contract)
    pages = []
    for number, (width, height) in enumerate(sizes):
        pages.append(
            f"<style>@page p{number} {{ size: {width}pt {height}pt; margin: 0 }}</style>"
            f'<section style="page: p{number}; height: {height}pt; position: relative">'
            f'<p dir="rtl" style="position: absolute; top: 700pt; right: 72pt">{name}</p>'
            "</section>"
        )
    overlay = await renderer().html_to_pdf(
        "".join(pages), page=PageOptions(margin_top="0", margin_bottom="0")
    )
    return await asyncio.to_thread(overlay_pdf, contract, overlay)
```

Escape `name` with `html.escape` before you place it, or clean it with
`clean_html` if it is rich text. `page_sizes` and `overlay_pdf` work on
the CPU, so they run in a thread. If a section's text runs onto a second
page, the overlay has more pages than the contract, and `overlay_pdf`
refuses it.

A page's shown size is its crop box (where it lies within the media box),
turned by its rotation: a portrait page rotated 90 degrees is shown, and
measured, as landscape. `overlay_pdf` turns and moves each overlay into
the page's own coordinates, so a rotated page or a crop box away from
the origin shifts nothing. The overlay's top-left corner meets the shown
page's, and its size may differ from the page's by 2 points, as browsers
round sizes; more than that is refused. The page's own content is left
as it was, in a graphics state of its own, and the overlay is drawn over
it.

For some pages only, pass a mapping of page indexes to one-page PDFs:
`overlay_pdf(contract, {0: first, 3: fourth})`. An encrypted or
unreadable PDF is refused. Lay text over a PDF before it is signed: any
change breaks a signature.

### What a PDF may decode

A PDF keeps a page's drawing, and most of its objects, deflated. A page
of 8 KB may inflate to 8 MB, so a file of 170 KB can hold hundreds of
megabytes once it is read. pypdf bounds each stream it decodes (75 MB by
default), not what they come to together. So `overlay_pdf` and
`page_sizes` take `max_decoded`, the bytes they decode in all: 32 MiB by
default. *New in 2.7.*

- `page_sizes` counts the PDF's object streams, where it keeps its
  objects compressed.
- `overlay_pdf` counts the object streams of the document and of the
  overlays, and the content of each page it lays over, and of its
  overlay.

Each stream is decoded before it is used, and no further than what is
left, so a PDF that inflates past the budget is refused with a
`DocumentError` once the budget runs out, not decoded whole. A page's
content is written back deflated: the result is about the size of the
document, not of what it decoded.

```python
laid = await asyncio.to_thread(
    overlay_pdf, drawing, overlay, max_decoded=128 * 1024 * 1024
)
```

The default holds a contract of hundreds of pages. Raise it for drawings
or maps of many megabytes a page, and keep it as low as your documents
allow: what is decoded is held in memory, about twice over, while the
call runs.

pypdf's limits are lowered for the call only. pypdf keeps them in a
context variable, so other threads, and the code around the call, keep
their own, and a limit lower than `max_decoded` that your code set with
`pypdf.apply_configuration` still holds. The cross-reference streams
pypdf reads as it opens a file are held to its limits each, not counted
in all. A PDF a person uploads is checked before it is stored
([Uploads](uploads.md)); `max_decoded` bounds what is laid over later
all the same, whether it was stored before, or converted from a Word
document.

## Rich text from people

A person writes a contract's terms in an editor. Before the text goes
into a template, `clean_html` keeps it to an allowlist:

```python
from domainkit.documents import clean_html

terms = clean_html(contract.terms)
page = template.render(terms=terms)  # inserted as HTML, unescaped
```

It keeps paragraphs, headings, lists, tables, emphasis, direction
(`dir`, `lang`, `<bdi>`, `<bdo>`), and images given inline as data
(`data:image/png;base64,...`). It drops everything that makes the
renderer load or run anything: scripts, style sheets, frames, objects,
forms, SVG and MathML, event handlers, `class` and `id`, and every URL
to fetch, in `src` or in CSS (`url()`). A `style` keeps a list of
typographic properties (`color`, `font-weight`, `text-align`, margins,
...) with plain values, none negative. `vertical-align` keeps keywords
only, `line-height` is at least 1 (or 100%), and no `width`, `height`,
or `white-space` is kept, since a box too small for its text lets the
text run over the next. nh3 writes a style back as its CSS parser read
it (`0-500px` as `0 -500px`), so the text is cleaned twice, and the
second pass checks what the first wrote. Links are dropped, their text
kept; `clean_html(html, links=True)` keeps `https:` and `mailto:` links,
which a PDF shows and never loads. Comments are dropped.

That keeps out what most often lays text over text, not all of it. Text
made tiny or the colour of the page, and large text (a big font, wide
spacing) in a box your template made narrow, which runs over its
neighbour, are not stopped: show a preview of what is signed.

### Why cleaning is not enough

The sanitizer is the first of two layers; Gotenberg's flags are the
second ([Hardening Gotenberg](#hardening-gotenberg)):

- The sanitizer reads HTML with its own parser, which may not read it as
  Chromium does (mutation XSS).
- The template around the clean text is yours, and may link what it
  likes.
- An allowlist changes with releases, and a bug in one is a hole.

Only the browser that would make a request can refuse it for sure. With
JavaScript off, the deny list kept, private addresses denied, and
downloads and webhooks off, what gets past the sanitizer still reaches
nothing.

## Arabic and other scripts

Arabic letters change shape with their neighbours, and Arabic runs from
right to left. Placing them takes a layout engine that shapes text, as
HarfBuzz does in Chromium and in LibreOffice. pypdf has none, and draws
no text here. So Arabic reaches a PDF through HTML or through a Word
document, rendered by Gotenberg:

- In HTML, write `dir="rtl"` and `lang="ar"`, and send the font as an
  asset (`@font-face { src: url("NotoNaskhArabic.ttf") }`), so the PDF
  does not depend on the fonts of Gotenberg's image.
- In Word, the template's runs carry the direction; `fill_docx` writes
  Arabic values and Arabic placeholder names as they are.
- Over a PDF, render the Arabic text as the overlay, as above.

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `PlaceholderError` | Placeholders and values disagree; `missing`, `unused`, and `malformed` list each problem. | Fix the template or the values. For a template a user uploaded, answer 422 with the lists. |
| `DocumentError` | A document cannot be read (not a `.docx`, past `ArchiveLimits`, a DTD, an encrypted PDF, a PDF that decodes past `max_decoded`), or overlays do not fit their pages. The base of `PlaceholderError` and `RenderingRefused`; also a `ValueError`. | Read its message. |
| `RenderingRefused` | Gotenberg refused the input: 400 (a form or a document it cannot read), 409 (a resource did not load), 413 (past its body limit). `status` and `trace` say which. | Fix the document; sending it again changes nothing. |
| 503 `renderer_unavailable` (`RendererUnavailable`) | Gotenberg could not be reached, failed once the retries were spent, refused the login, or answered something other than a PDF. | Try again later; check Gotenberg and its login. |
| 503 `upstream_unavailable`, 504 `upstream_timeout`, 502 `upstream_response_too_large` | The client's own: the circuit is open, `TIMEOUT` passed, or the PDF is past `MAX_RESPONSE_BYTES`. | See [outgoing calls](reference/http.md). |
| `ImproperlyConfigured` | `renderer()` with no `DOCUMENTS["RENDERER"]`. | Set it. |
| `ImportError` | The `documents` extra is not installed. | `pip install "domainkit[documents]"`. |

## Testing

Tests never reach Gotenberg: the HTTP client's `stub` answers for it.

```python
import httpx2

from domainkit.documents import renderer
from domainkit.documents.gotenberg import LIBREOFFICE
from domainkit.http.client import stub


async def test_a_lease_is_converted() -> None:
    sent = []

    def gotenberg(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.path)
        return httpx2.Response(200, content=b"%PDF-1.7 fake")

    with stub(gotenberg):
        pdf = await renderer().office_to_pdf(b"PK...", filename="lease.docx")

    assert sent == [LIBREOFFICE]
    assert pdf.startswith(b"%PDF-")
```

`fill_docx`, `overlay_pdf`, and `clean_html` need no server: test them on
small documents built in the test.

## Common mistakes

- **Rendering text a person wrote without `clean_html`.** The renderer
  prints and fetches what the HTML says.
- **Leaving Gotenberg's defaults.** It runs scripts and reaches every
  address. Set the flags of [Hardening Gotenberg](#hardening-gotenberg).
- **Drawing Arabic with a PDF library.** The letters come out unjoined
  and left to right. Render it.
- **Making an overlay for the page unrotated.** Use `page_sizes`: it
  gives the size as shown.
- **Calling `fill_docx` on the event loop.** A large document holds every
  request meanwhile; use `asyncio.to_thread`.
- **Raising `max_decoded` for every document.** Raise it where a kind of
  document needs it, not for files people upload.

## See also

- [Backends reference](reference/backends.md#documents): the `DOCUMENTS`
  settings.
- [Checks](reference/checks.md): `documents.E001`, `documents.W001`, and
  `documents.I001`.
- [Uploads](uploads.md): receiving the templates and PDFs people upload.
- [Gotenberg's documentation](https://gotenberg.dev/docs/8.x/getting-started/introduction):
  its routes and flags.

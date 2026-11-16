"""The renderer on Gotenberg: HTML by Chromium, Office documents by LibreOffice.

::

    DOCUMENTS = {
        "RENDERER": {
            "URL": "https://gotenberg.internal:3000",
            "USERNAME": "shop",
            "PASSWORD": env.str("GOTENBERG_PASSWORD"),
        },
    }

    pdf = await renderer().html_to_pdf(html, assets={"logo.png": logo})

Each rendering is one ``POST`` of a multipart form through
``domainkit.http.client`` (``domainkit[httpclient]``): bounded by
``TIMEOUT`` and by the deadline of the work it serves, retried after a
network error or a 502, 503, or 504 answer (rendering changes nothing,
so it may be sent twice), and refused at once while Gotenberg's circuit
is open. It sends the request's id in ``Gotenberg-Trace``, which
Gotenberg logs, and its errors name the id Gotenberg answered with.

It uses the routes and fields Gotenberg 8 and 9 both take
(https://gotenberg.dev/docs/8.x/convert-with-chromium/convert-html-to-pdf,
https://gotenberg.dev/docs/getting-started/upgrade):

- ``POST /forms/chromium/convert/html``: ``index.html`` and the files it
  links by name; ``paperWidth``, ``paperHeight``, the four margins,
  ``landscape``, ``scale``, ``printBackground``, ``preferCssPageSize``,
  and ``failOnResourceLoadingFailed``, always on: a page that links
  something that fails to load for a network error is refused (409), not
  printed without it;
- ``POST /forms/libreoffice/convert``: one document.

A refusal (400, 409, 413) raises ``RenderingRefused``. A Gotenberg that
cannot be reached, fails (5xx, once the retries are spent), or refuses
the login (401, 403) raises ``RendererUnavailable``; the client's
``CallTimeout`` and ``CircuitOpen`` go through as they are.
"""

import base64
import logging
import mimetypes
import re
import secrets
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

import httpx2

from domainkit.context import current_request
from domainkit.documents.base import (
    PageOptions,
    RendererUnavailable,
    RenderingRefused,
)
from domainkit.exceptions import UpstreamError
from domainkit.http.client import HttpClient

if TYPE_CHECKING:
    from domainkit.conf.global_settings import RendererConfig

__all__ = ["CHROMIUM_HTML", "LIBREOFFICE", "GotenbergRenderer"]

CHROMIUM_HTML: Final = "/forms/chromium/convert/html"
"""Gotenberg's route that prints ``index.html`` with Chromium."""
LIBREOFFICE: Final = "/forms/libreoffice/convert"
"""Gotenberg's route that converts Office documents with LibreOffice."""
TRACE_HEADER: Final = "Gotenberg-Trace"
FILE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""A file sent along: a plain name, no folder, as Gotenberg drops folders."""
INDEX: Final = "index.html"
MAX_ASSETS: Final = 64
"""The most files one page may link."""
OFFICE_EXTENSIONS: Final = frozenset(
    {"doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx", "odp"}
)
"""What ``office_to_pdf`` sends: Office and OpenDocument files, not those named
for macros; Gotenberg runs no macro unless ``--libreoffice-enable-macros``."""
REFUSALS: Final = frozenset({400, 409, 413})
"""Answers that say the input cannot be rendered: sending it again changes nothing."""
LOGIN_REFUSED: Final = frozenset({401, 403})
MAX_REASON: Final = 300
"""The most characters of Gotenberg's reason an error repeats."""
PDF_MAGIC: Final = b"%PDF-"

logger = logging.getLogger(__name__)


class GotenbergRenderer:
    """A ``Renderer`` on a Gotenberg 8 or 9 server, with basic auth if it asks."""

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        trace_header: str = TRACE_HEADER,
    ) -> None:
        """Call the Gotenberg at ``url``, as ``username`` if it has basic auth.

        Raises:
            ValueError: One of ``username`` and ``password`` was given
                without the other, or the user has a colon.
        """
        if (username is None) != (password is None):
            raise ValueError("Give Gotenberg's username and password together.")
        headers = {}
        if username is not None and password is not None:
            if ":" in username:
                raise ValueError("A basic auth username has no colon.")
            login = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {login}"
        self.trace_header = trace_header
        self.client = HttpClient(
            base_url=url, headers=headers, timeout=timeout, name="gotenberg"
        )
        """The calls' client: one circuit for this Gotenberg."""

    @classmethod
    def from_config(cls, config: "RendererConfig") -> "GotenbergRenderer":
        """The renderer ``DOCUMENTS["RENDERER"]`` describes."""
        password = config.PASSWORD
        return cls(
            config.URL,
            username=config.USERNAME,
            password=None if password is None else password.get_secret_value(),
            timeout=config.TIMEOUT,
            trace_header=config.TRACE_HEADER,
        )

    async def html_to_pdf(
        self,
        html: str,
        *,
        assets: Mapping[str, bytes] | None = None,
        page: PageOptions | None = None,
    ) -> bytes:
        """``html`` printed by Chromium, with the ``assets`` it links by name.

        ``assets`` are images, fonts, and style sheets, by the plain names
        the HTML links them by (``<img src="logo.png">``). ``header.html``
        and ``footer.html`` are printed at the top and the bottom of each
        page, as Gotenberg does.

        Raises:
            ValueError: An asset's name is not a plain file name, is
                ``index.html``, or there are more than ``MAX_ASSETS``.
            RenderingRefused: Gotenberg cannot print it.
            RendererUnavailable: Gotenberg could not print now.
        """
        files = [(INDEX, html.encode(), "text/html; charset=utf-8")]
        given = dict(assets or {})
        if len(given) > MAX_ASSETS:
            raise ValueError(f"A page links {MAX_ASSETS} files at most.")
        for name, content in given.items():
            if not FILE_NAME.fullmatch(name) or name.lower() == INDEX:
                raise ValueError(
                    f"An asset is named by a plain file name other than {INDEX}, "
                    f"as 'logo.png', not {name!r}."
                )
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
            files.append((name, content, kind))
        return await self._render(CHROMIUM_HTML, page_fields(page), files)

    async def office_to_pdf(
        self, document: bytes, *, filename: str = "document.docx"
    ) -> bytes:
        """``document`` converted by LibreOffice; ``filename``'s extension names it.

        ``doc``, ``xls``, and ``ppt`` may hold macros: Gotenberg runs none
        unless started with ``--libreoffice-enable-macros``.

        Raises:
            ValueError: ``filename`` is not a plain file name, or its
                extension is not in ``OFFICE_EXTENSIONS``.
            RenderingRefused: LibreOffice cannot read it.
            RendererUnavailable: Gotenberg could not convert now.
        """
        extension = filename.rpartition(".")[2].lower()
        if not FILE_NAME.fullmatch(filename) or extension not in OFFICE_EXTENSIONS:
            raise ValueError(
                "An Office document is named by a plain file name ending in one "
                f"of {', '.join(sorted(OFFICE_EXTENSIONS))}, not {filename!r}."
            )
        files = [(filename, document, "application/octet-stream")]
        return await self._render(LIBREOFFICE, {}, files)

    async def _render(
        self,
        route: str,
        fields: Mapping[str, str],
        files: Sequence[tuple[str, bytes, str]],
    ) -> bytes:
        """Post the form to ``route``; the PDF Gotenberg answers with.

        Raises:
            RenderingRefused: Gotenberg refused the form (400, 409, 413).
            RendererUnavailable: It could not be reached, failed, refused
                the login, or answered something that is not a PDF.
        """
        body, content_type = encode_form(fields, files)
        headers = {"Content-Type": content_type}
        request = current_request()
        if request is not None:
            headers[self.trace_header] = request.request_id
        try:
            response = await self.client.post(
                route, content=body, headers=headers, retry=True
            )
        except UpstreamError:
            raise  # CircuitOpen, CallTimeout, ResponseTooLarge: their own answers.
        except httpx2.HTTPError as exc:
            logger.warning("Gotenberg could not be reached: %s", type(exc).__name__)
            raise RendererUnavailable(
                f"Gotenberg could not be reached ({type(exc).__name__})."
            ) from exc
        status = response.status_code
        trace = response.headers.get(self.trace_header, "")
        if status == httpx2.codes.OK and response.content.startswith(PDF_MAGIC):
            return response.content
        if status in REFUSALS:
            reason = " ".join(response.text[:MAX_REASON].split())
            logger.warning(
                "Gotenberg refused %s (%s, trace %s): %s", route, status, trace, reason
            )
            raise RenderingRefused(
                f"Gotenberg refused the document ({status}, trace {trace!r}): {reason}",
                status=status,
                trace=trace,
            )
        if status in LOGIN_REFUSED:
            problem = (
                f"Gotenberg refused the login ({status}): check DOCUMENTS"
                '["RENDERER"]\'s USERNAME and PASSWORD'
            )
        elif status == httpx2.codes.OK:
            problem = "Gotenberg answered with something that is not a PDF"
        else:
            problem = f"Gotenberg answered {status}"
        logger.error("%s (%s, trace %s)", problem, route, trace)
        raise RendererUnavailable(f"{problem} (trace {trace!r}).")


def page_fields(page: PageOptions | None) -> dict[str, str]:
    """The Chromium route's form fields for ``page`` (its defaults if None)."""
    page = page or PageOptions()
    fields = {
        "landscape": _flag(page.landscape),
        "scale": f"{page.scale:g}",
        "printBackground": _flag(page.print_background),
        "preferCssPageSize": _flag(page.prefer_css_page_size),
        "failOnResourceLoadingFailed": "true",
    }
    lengths = {
        "paperWidth": page.width,
        "paperHeight": page.height,
        "marginTop": page.margin_top,
        "marginBottom": page.margin_bottom,
        "marginLeft": page.margin_left,
        "marginRight": page.margin_right,
    }
    return fields | {name: value for name, value in lengths.items() if value}


def _flag(value: bool) -> str:
    """A boolean as Gotenberg reads it."""
    return "true" if value else "false"


def encode_form(
    fields: Mapping[str, str], files: Sequence[tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body (RFC 7578) and its content type.

    Each of ``files`` is a name, its bytes, and its type, sent as a
    ``files`` field, which both Gotenberg routes read. The names and the
    fields are this module's own, or checked plain names, so none needs
    quoting. The boundary is random, and drawn again in the unlikely case
    that a file holds it.
    """
    boundary = _boundary()
    while any(boundary.encode() in content for _, content, _ in files):
        boundary = _boundary()
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    for filename, content, kind in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="{filename}"\r\nContent-Type: {kind}\r\n\r\n'.encode()
        )
        parts += [content, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _boundary() -> str:
    """A fresh multipart boundary."""
    return f"domainkit-{secrets.token_hex(16)}"

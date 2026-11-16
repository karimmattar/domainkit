"""What the parts of ``domainkit.documents`` share: errors, the port, the extra.

Nothing here imports the ``documents`` extra: each function imports its
package when it is called, and ``missing`` says how to install it.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, runtime_checkable

from domainkit.checks import CheckMessage
from domainkit.exceptions import DomainkitError, ImproperlyConfigured, UpstreamError

if TYPE_CHECKING:
    from domainkit.conf.global_settings import RendererConfig

LENGTH: Final = re.compile(
    r"(?:[0-9]{1,4}(?:\.[0-9]{1,4})?|\.[0-9]{1,4})(?:mm|cm|in|pt|px|pc)?"
)
"""A page length as Gotenberg reads it: a number and its unit; inches without one."""
MIN_SCALE: Final = 0.1
MAX_SCALE: Final = 2.0
"""The scales Chromium prints at (Gotenberg refuses others)."""


def missing(package: str) -> ImportError:
    """The error of a function whose package, of the extra, is not installed."""
    return ImportError(
        f'domainkit.documents needs {package}: pip install "domainkit[documents]".'
    )


class DocumentError(DomainkitError, ValueError):
    """A document, or what was given to fill it, cannot be used.

    The message says why, for the logs and for whoever wrote the template.
    It names placeholders and pages, never a value. A route answers it as
    it sees fit: 422, for a template a user uploaded.
    """


class PlaceholderError(DocumentError):
    """A Word document's placeholders and the values given do not agree.

    Every problem is listed at once: ``missing``, placeholders without a
    value; ``unused``, values no placeholder takes; ``malformed``, text
    that opens or closes a placeholder but is not one.
    """

    def __init__(
        self,
        *,
        missing: Iterable[str] = (),
        unused: Iterable[str] = (),
        malformed: Iterable[str] = (),
    ) -> None:
        """Keep each list, sorted, and say what they hold."""
        self.missing = tuple(sorted(missing))
        self.unused = tuple(sorted(unused))
        self.malformed = tuple(malformed)
        problems = []
        if self.missing:
            names = ", ".join(f"{{{{{name}}}}}" for name in self.missing)
            problems.append(f"no value for {names}")
        if self.unused:
            problems.append(f"no placeholder takes {', '.join(self.unused)}")
        if self.malformed:
            texts = ", ".join(repr(text) for text in self.malformed)
            problems.append(f"not a placeholder: {texts}")
        super().__init__(
            f"The placeholders and the values do not agree: {'; '.join(problems)}."
        )


class RenderingRefused(DocumentError):
    """The renderer refused what it was sent: the document cannot be rendered.

    Sending it again changes nothing. ``status`` is the renderer's answer:
    400 for a form or a document it cannot read, 409 for a page that
    failed a check (a resource that did not load), 413 for a body past its
    limit. ``trace`` is the id its logs hold the call under.
    """

    def __init__(self, message: str, *, status: int, trace: str) -> None:
        """Keep the renderer's status and trace with the message."""
        super().__init__(message)
        self.status = status
        self.trace = trace


class RendererUnavailable(UpstreamError):
    """The renderer could not render now: unreachable, failing, or refusing the login.

    A request that lets it go is answered 503 ``renderer_unavailable``.
    """

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "renderer_unavailable"
    detail: ClassVar[str] = "Documents cannot be rendered now; try again later."


@dataclass(frozen=True, slots=True, kw_only=True)
class PageOptions:
    """How a browser prints HTML; None leaves the renderer's default.

    Lengths are a number and a unit, as ``210mm``, ``8.5in``, ``72pt``,
    ``1cm``, ``96px``, or ``6pc``; a number alone is inches. While
    ``prefer_css_page_size`` holds (the default), an ``@page`` rule of the
    HTML sets the page's size and margins, and the lengths here serve pages
    no rule sizes.
    """

    width: str | None = None
    height: str | None = None
    margin_top: str | None = None
    margin_bottom: str | None = None
    margin_left: str | None = None
    margin_right: str | None = None
    landscape: bool = False
    scale: float = 1.0
    """0.1 to 2."""
    print_background: bool = True
    """Print backgrounds: colours and images behind the text."""
    prefer_css_page_size: bool = True

    def __post_init__(self) -> None:
        """Refuse a length or a scale the renderer would.

        Raises:
            ValueError: A length is not a number and a unit, or the scale is
                outside 0.1 to 2.
        """
        for name in (
            "width",
            "height",
            "margin_top",
            "margin_bottom",
            "margin_left",
            "margin_right",
        ):
            value = getattr(self, name)
            if value is not None and not LENGTH.fullmatch(value):
                raise ValueError(
                    f"A page's {name} is a number and a unit, as '210mm', "
                    f"'8.5in', or '72pt', not {value!r}."
                )
        if not MIN_SCALE <= self.scale <= MAX_SCALE:
            raise ValueError(f"A page's scale is 0.1 to 2, not {self.scale!r}.")


@runtime_checkable
class Renderer(Protocol):
    """Turns HTML, or an Office document, into a PDF (``GotenbergRenderer``)."""

    async def html_to_pdf(
        self,
        html: str,
        *,
        assets: Mapping[str, bytes] | None = None,
        page: PageOptions | None = None,
    ) -> bytes:
        """``html`` printed by a browser, with the ``assets`` it links by name.

        Raises:
            RenderingRefused: The renderer cannot render it.
            RendererUnavailable: The renderer could not render now.
        """

    async def office_to_pdf(
        self, document: bytes, *, filename: str = "document.docx"
    ) -> bytes:
        """``document`` converted by an office suite; ``filename`` says its kind.

        Raises:
            RenderingRefused: The renderer cannot read it.
            RendererUnavailable: The renderer could not render now.
        """


_made: dict[str, tuple["RendererConfig", Renderer]] = {}


def renderer() -> Renderer:
    """The ``DOCUMENTS["RENDERER"]`` Gotenberg server, made once per configuration.

    Raises:
        ImproperlyConfigured: ``DOCUMENTS["RENDERER"]`` is not set.
        ImportError: ``domainkit[httpclient]``, which calls Gotenberg, is
            not installed.
    """
    from domainkit.conf import settings

    config = settings.DOCUMENTS.RENDERER
    if config is None:
        raise ImproperlyConfigured(
            'No renderer is set: DOCUMENTS = {"RENDERER": {"URL": ...}} names '
            "the Gotenberg server (docs/documents.md)."
        )
    held = _made.get("renderer")
    if held is not None and held[0] == config:
        return held[1]
    try:
        from domainkit.documents.gotenberg import GotenbergRenderer
    except ImportError as exc:
        raise ImportError(
            "The renderer calls Gotenberg with httpx2: "
            'pip install "domainkit[httpclient]".'
        ) from exc
    made = GotenbergRenderer.from_config(config)
    _made["renderer"] = (config, made)
    return made


def flags_statement() -> CheckMessage:
    """What ``check`` cannot see of the renderer: the flags Gotenberg runs with."""
    return CheckMessage(
        "documents.I001",
        "Gotenberg's flags are not checked: they are set where it runs, and "
        "its API does not show them.",
        "Start it with --chromium-disable-javascript, --chromium-deny-private-ips, "
        "--libreoffice-deny-private-ips, a --libreoffice-deny-list or allow list, "
        "--api-disable-download-from, and --webhook-disable, and keep "
        "--chromium-deny-list's default (docs/documents.md, 'Hardening Gotenberg').",
    )

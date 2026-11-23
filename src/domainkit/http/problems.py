"""The problems a route or an adapter raises: a coded HTTP error, and its fields.

``APIError`` answers with the status, the detail, and the code it is
given, and ``FieldError`` names one invalid part of a request.
``domainkit.http.errors`` maps them to the error body and re-exports them,
so ``from domainkit.http.errors import APIError`` still works.

They are apart from ``domainkit.http.errors``, which builds responses
and maps every other error, so that the infrastructure layer may import
them, by the names of ``__all__`` alone: a file gateway refuses a file with
the code a route would (``domainkit.architecture.ALLOWED_MODULES``, New in
2.8, ADR 0182).
Business refusals stay domain errors (``domainkit.ddd.exceptions``),
which the HTTP layer maps.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel
from starlette.exceptions import HTTPException

from domainkit.exceptions import DomainkitError

if TYPE_CHECKING:
    from domainkit.i18n.lazy import LazyString

__all__ = ["APIError", "FieldError"]

_NONE: Final[Mapping[str, object]] = MappingProxyType({})


class FieldError(BaseModel):
    """One invalid part of a request; the submitted value is left out."""

    loc: list[str | int]
    msg: str
    type: str


class APIError(HTTPException, DomainkitError):
    """An HTTP error outside the domain (a bad cursor, an oversized body).

    ``detail`` is text, translated when it is answered, or a lazy string
    (``gettext_lazy``), answered as its text in the caller's language. It
    is kept as given: a lazy one compares and formats as its text, and
    ``str()`` makes it text.
    """

    def __init__(
        self,
        status_code: int,
        detail: "str | LazyString",
        *,
        code: str,
        headers: Mapping[str, str] | None = None,
        errors: Sequence[FieldError] | None = None,
        extensions: Mapping[str, object] | None = None,
    ) -> None:
        """Carry a machine-readable ``code``, field ``errors``, and ``extensions``."""
        super().__init__(
            status_code=status_code,
            detail=detail,  # type: ignore[arg-type]  # Kept lazy: see the class.
            headers=dict(headers) if headers else None,
        )
        self.code = code
        self.errors = list(errors) if errors else None
        self._extensions = MappingProxyType(dict(extensions)) if extensions else _NONE

    def extensions(self) -> Mapping[str, object]:
        """What the body adds to its members, by name, as a ``DomainError``'s.

        The ``extensions`` given (since 2.10), JSON values, read only; none
        by default. A name the body has in either format is left out.
        """
        return self._extensions

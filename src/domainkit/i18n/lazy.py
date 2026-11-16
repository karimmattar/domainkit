"""Texts translated later: marks, and strings translated when they are shown.

A domain layer may import this module, and not the rest of
``domainkit.i18n`` (``domainkit.architecture``): nothing here reads the
settings or a catalog when it is called, and importing it loads neither.
``gettext_noop`` marks a text that is translated where it is shown, as a
domain error's message is at the HTTP boundary (ADR 0039); the ``_lazy``
functions make a ``LazyString``, looked up each time it is turned into
text, in the language active then::

    from domainkit.i18n.lazy import gettext_lazy, gettext_noop

    raise BusinessRuleViolation(gettext_noop("A paid invoice cannot change."))

    class Status(TextChoices):
        PAID = "paid", gettext_lazy("Paid in full")

``makemessages`` finds the texts each of them marks (ADR 0166).
A ``LazyString`` names the lookup that makes it (``source``), so that a
process with no settings can tell another which string to make again.
"""

from collections.abc import Callable
from typing import Final, NamedTuple

__all__ = [
    "LazyString",
    "gettext_lazy",
    "gettext_noop",
    "ngettext_lazy",
    "pgettext_lazy",
]


class LazyString:
    """A translation made each time the string is used, in the language active then.

    Define messages at import time with ``gettext_lazy``; ``str()`` turns
    one into text. It compares, hashes, formats, and concatenates as its
    current translation.
    """

    __slots__ = ("_arguments", "_function")

    def __init__(self, function: Callable[..., str], *arguments: object) -> None:
        """Translate with ``function(*arguments)`` when used."""
        self._function = function
        self._arguments = arguments

    def __str__(self) -> str:
        """The translation in the active language."""
        return self._function(*self._arguments)

    def __repr__(self) -> str:
        """Show the class and the current translation."""
        return f"<LazyString {str(self)!r}>"

    def __eq__(self, other: object) -> bool:
        """Equal to text, or another lazy string, with the same translation."""
        if isinstance(other, LazyString):
            other = str(other)
        return str(self) == other

    def __hash__(self) -> int:
        """Hash as the current translation."""
        return hash(str(self))

    def __add__(self, other: str) -> str:
        """The translation followed by ``other``."""
        return str(self) + other

    def __radd__(self, other: str) -> str:
        """``other`` followed by the translation."""
        return other + str(self)

    def __mod__(self, values: object) -> str:
        """``%``-format the translation."""
        return str(self) % values

    def format(self, *args: object, **kwargs: object) -> str:
        """``str.format`` the translation."""
        return str(self).format(*args, **kwargs)

    def source(self) -> tuple[str, tuple[object, ...]] | None:
        """The lookup that makes the text, and its arguments; None for another function.

        ``"gettext"``, ``"ngettext"`` or ``"pgettext"``, for a string made by
        ``gettext_lazy``, ``ngettext_lazy`` or ``pgettext_lazy``:
        ``gettext_lazy("Paid")`` is ``("gettext", ("Paid",))``. Reading it
        needs no settings: a process that has none to make the text sends it
        instead, and another makes the same string again (an upload check's
        refusal, ``domainkit.inspection``).
        """
        lookup = next(
            (name for function, name in _NAMED if function is self._function), None
        )
        return None if lookup is None else (lookup, self._arguments)


def gettext_noop(message: str) -> str:
    """Mark ``message`` for translators without translating it now."""
    return message


def gettext_lazy(message: str) -> LazyString:
    """``gettext`` when the string is used."""
    return LazyString(_gettext, message)


def ngettext_lazy(singular: str, plural: str, n: int) -> LazyString:
    """``ngettext`` when the string is used."""
    return LazyString(_ngettext, singular, plural, n)


def pgettext_lazy(context: str, message: str) -> LazyString:
    """``pgettext`` when the string is used."""
    return LazyString(_pgettext, context, message)


# The lookups are imported when a string is first shown, then kept: importing
# this module loads no settings, no domain registry, and no catalog.


class _Lookups(NamedTuple):
    """``domainkit.i18n.translation``'s lookups."""

    gettext: Callable[[str], str]
    ngettext: Callable[[str, str, int], str]
    pgettext: Callable[[str, str], str]


_found: _Lookups | None = None


def _lookups() -> _Lookups:
    """The lookups, imported the first time a lazy string is shown."""
    global _found
    from domainkit.i18n.translation import gettext, ngettext, pgettext

    _found = _Lookups(gettext, ngettext, pgettext)
    return _found


def _gettext(message: str) -> str:
    """``translation.gettext``."""
    return (_found or _lookups()).gettext(message)


def _ngettext(singular: str, plural: str, n: int) -> str:
    """``translation.ngettext``."""
    return (_found or _lookups()).ngettext(singular, plural, n)


def _pgettext(context: str, message: str) -> str:
    """``translation.pgettext``."""
    return (_found or _lookups()).pgettext(context, message)


_NAMED: Final[tuple[tuple[Callable[..., str], str], ...]] = (
    (_gettext, "gettext"),
    (_ngettext, "ngettext"),
    (_pgettext, "pgettext"),
)
"""Each lookup, and the name ``LazyString.source`` gives it: a function is
compared by identity, so that any callable may make a ``LazyString``."""

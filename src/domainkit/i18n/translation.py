"""Translations: the active language, gettext catalogs, and lazy strings.

The language of the work in progress lives in a ``ContextVar``:
``LocaleMiddleware`` sets it per request from ``Accept-Language``, tasks
started in a request inherit it, and ``override`` sets it for a block.
Without one, it is ``LANGUAGE_CODE``.

Messages are looked up in gettext catalogs (``.mo`` files compiled from
``.po``, see ``catalogs``) named ``messages``, in this order; the first
that has a message wins:

1. each directory of ``LOCALE_PATHS``;
2. each installed domain's ``locale`` directory, in ``INSTALLED_DOMAINS``
   order;
3. domainkit's own (English and Arabic).

A catalog is ``<directory>/<locale>/LC_MESSAGES/messages.mo``, where the
locale of ``pt-br`` is ``pt_BR``; ``pt`` is looked up after it.
"""

import gettext as gnu
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from pathlib import Path
from typing import Final

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.global_settings import LANGUAGE_PATTERN

DOMAIN: Final = "messages"
"""The name of every catalog file: ``messages.po`` and ``messages.mo``."""
LOCALE_DIR: Final = Path(__file__).parent / "locale"
"""domainkit's own catalogs."""
MAX_ACCEPT_LANGUAGE: Final = 1024
"""Characters of ``Accept-Language`` read; the rest is ignored."""
MAX_LANGUAGE_RANGES: Final = 32
"""Language ranges of ``Accept-Language`` read; the rest are ignored."""

_LANGUAGE: Final = re.compile(LANGUAGE_PATTERN)
_RANGE: Final = re.compile(r"[a-z]{1,8}(?:-[a-z0-9]{1,8})*|\*")
_QUALITY: Final = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")

_active: ContextVar[str | None] = ContextVar("language", default=None)


def get_language() -> str:
    """The language of the request or block being served (``LANGUAGE_CODE`` if none)."""
    return _active.get() or settings.LANGUAGE_CODE


@contextmanager
def override(language: str) -> Iterator[None]:
    """Use ``language`` (``"ar"``) inside the block, as a request in it would.

    Raises:
        ValueError: ``language`` is not a language code.
    """
    check_language(language)
    token = _active.set(language)
    try:
        yield
    finally:
        _active.reset(token)


def check_language(language: str) -> str:
    """Return ``language`` if it is a code like ``ar`` or ``pt-br``; raise otherwise."""
    if not _LANGUAGE.fullmatch(language):
        raise ValueError(
            f"{language!r} is not a language code: lower case, as 'ar' or 'pt-br'."
        )
    return language


def to_locale(language: str) -> str:
    """The locale a language's catalogs are filed under: ``pt-br`` is ``pt_BR``.

    Scripts are title case (``zh-hant`` is ``zh_Hant``), as in Django.
    """
    code, _, region = language.lower().partition("-")
    if not region:
        return code
    region, _, rest = region.partition("-")
    region = region.title() if len(region) > 2 else region.upper()
    return f"{code}_{region}" + (f"-{rest}" if rest else "")


def negotiate(header: str | None, languages: Sequence[str], default: str) -> str:
    """The language of ``languages`` an ``Accept-Language`` header prefers most.

    Ranges are tried by quality (``q``), then in order; a range matches a
    language that is equal, its primary subtag (``ar-eg`` matches ``ar``),
    or a variant of it (``pt`` matches ``pt-br``). ``*``, nothing that
    matches, and a missing header give ``default``. Malformed ranges are
    skipped, and only the first ``MAX_LANGUAGE_RANGES`` ranges are read.
    """
    if not header:
        return default
    ranges: list[tuple[float, int, str]] = []
    items = header[:MAX_ACCEPT_LANGUAGE].split(",")[:MAX_LANGUAGE_RANGES]
    for position, item in enumerate(items):
        found = _language_range(item)
        if found is not None:
            tag, quality = found
            ranges.append((-quality, position, tag))
    for _, _, tag in sorted(ranges):
        if tag == "*":
            return default
        match = _supported(tag, languages)
        if match is not None:
            return match
    return default


def _language_range(item: str) -> tuple[str, float] | None:
    """One range of ``Accept-Language`` and its quality; None if malformed or 0."""
    tag, _, parameters = item.partition(";")
    tag = tag.strip().lower()
    if not _RANGE.fullmatch(tag):
        return None
    quality = 1.0
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() != "q":
            continue
        value = value.strip()
        if not _QUALITY.fullmatch(value):
            return None
        quality = float(value)
    return (tag, quality) if quality > 0 else None


def _supported(tag: str, languages: Sequence[str]) -> str | None:
    """The language of ``languages`` that ``tag`` asks for, if any."""
    if tag in languages:
        return tag
    primary = tag.partition("-")[0]
    if primary in languages:
        return primary
    return next((code for code in languages if code.startswith(f"{primary}-")), None)


def catalog(language: str | None = None) -> gnu.NullTranslations:
    """The merged catalogs of ``language`` (default: the active one)."""
    return _catalog(check_language(language or get_language()), locale_dirs())


def locale_dirs() -> tuple[Path, ...]:
    """Where catalogs are looked for, first match first (see the module's notes)."""
    domains = (config.path / "locale" for config in registry)
    return (*settings.LOCALE_PATHS, *domains, LOCALE_DIR)


@cache
def _catalog(language: str, directories: tuple[Path, ...]) -> gnu.NullTranslations:
    """Every catalog of ``language`` in ``directories``, chained by precedence."""
    merged = gnu.NullTranslations()
    locale = to_locale(language)
    found = [
        Path(path)
        for directory in directories
        for path in gnu.find(DOMAIN, str(directory), [locale], all=True)
    ]
    for path in reversed(found):
        with path.open("rb") as file:
            translations = gnu.GNUTranslations(file)
        translations.add_fallback(merged)
        merged = translations
    return merged


def reset_catalogs() -> None:
    """Forget loaded catalogs, so recompiled ``.mo`` files are read again (tests)."""
    _catalog.cache_clear()


def gettext(message: str) -> str:
    """``message`` in the active language, or itself when no catalog has it.

    The empty string stays empty (gettext would give the catalog's header).
    """
    return catalog().gettext(message) if message else message


def ngettext(singular: str, plural: str, n: int) -> str:
    """The form of ``singular``/``plural`` for ``n``, by the language's plural rule."""
    return catalog().ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    """``message`` as translated in ``context`` (a ``msgctxt``)."""
    return catalog().pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, n: int) -> str:
    """``ngettext`` within ``context``."""
    return catalog().npgettext(context, singular, plural, n)


def gettext_noop(message: str) -> str:
    """Mark ``message`` for translators without translating it now."""
    return message


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


def gettext_lazy(message: str) -> LazyString:
    """``gettext`` when the string is used."""
    return LazyString(gettext, message)


def ngettext_lazy(singular: str, plural: str, n: int) -> LazyString:
    """``ngettext`` when the string is used."""
    return LazyString(ngettext, singular, plural, n)


def pgettext_lazy(context: str, message: str) -> LazyString:
    """``pgettext`` when the string is used."""
    return LazyString(pgettext, context, message)

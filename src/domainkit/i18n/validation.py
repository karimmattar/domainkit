"""Validation messages in the request's language.

Pydantic writes its messages in English from a template per error type
(``String should have at most {max_length} character{expected_plural}``).
Catalogs translate the templates, in the ``validation`` context, and the
error's context fills them, numbers formatted for the language.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final, cast

from pydantic_core import PydanticKnownError
from pydantic_core.core_schema import ErrorType

from domainkit.i18n.formats import number_format
from domainkit.i18n.translation import gettext, pgettext

VALIDATION_CONTEXT: Final = "validation"
"""The ``msgctxt`` of pydantic's message templates in catalogs."""


def validation_message(error: Mapping[str, Any]) -> str:
    """The ``msg`` of one pydantic (or FastAPI) validation error, translated.

    A known error type is translated from its message template and filled
    with its context. A custom error's message is looked up as it is.
    Without a translation, ``msg`` is returned as pydantic wrote it.
    """
    message = str(error.get("msg", ""))
    context = dict(error.get("ctx") or {})
    try:
        kind = cast("ErrorType", str(error.get("type", "")))
        known = PydanticKnownError(kind, context)
    except (KeyError, TypeError):  # A custom type, or a context it refuses.
        return gettext(message)
    template = known.message_template
    translated = pgettext(VALIDATION_CONTEXT, template)
    if translated == template:
        return message
    values = {name: _localized(value) for name, value in context.items()}
    if "error" in values:
        values["error"] = gettext(str(values["error"]))
    try:
        return translated.format_map(values)
    except (KeyError, IndexError, ValueError):  # The translation names other fields.
        return message


def _localized(value: object) -> object:
    """Numbers written as the language writes them; anything else unchanged."""
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return number_format(value)
    return value

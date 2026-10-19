"""Responses in the caller's language, and dates in the business's time zone.

``LocaleMiddleware`` (``domainkit.http.locale``) picks a language of
``LANGUAGES`` from each request's ``Accept-Language`` and makes it current;
error details and validation messages are then translated, and the
response says which language it is in (``Content-Language``)::

    from domainkit.i18n import gettext, gettext_lazy, override

    gettext("Too many requests; slow down.")  # in the request's language
    TITLE = gettext_lazy("Invoices")          # translated when used
    with override("ar"):
        ...

Catalogs are gettext ``.po`` files compiled to ``.mo`` with
``manage.py compilemessages``; domainkit ships English and Arabic, and
projects add their own per domain (see ``translation`` and ADR 0039).

``timezone`` shows stored UTC times in ``TIME_ZONE``, and ``formats``
writes numbers and dates as the language does.
"""

from domainkit.i18n import timezone
from domainkit.i18n.formats import (
    date_format,
    datetime_format,
    number_format,
    time_format,
)
from domainkit.i18n.timezone import localdate, localtime
from domainkit.i18n.translation import (
    LazyString,
    get_language,
    gettext,
    gettext_lazy,
    gettext_noop,
    negotiate,
    ngettext,
    ngettext_lazy,
    npgettext,
    override,
    pgettext,
    pgettext_lazy,
)
from domainkit.i18n.validation import validation_message

__all__ = [
    "LazyString",
    "date_format",
    "datetime_format",
    "get_language",
    "gettext",
    "gettext_lazy",
    "gettext_noop",
    "localdate",
    "localtime",
    "negotiate",
    "ngettext",
    "ngettext_lazy",
    "npgettext",
    "number_format",
    "override",
    "pgettext",
    "pgettext_lazy",
    "time_format",
    "timezone",
    "validation_message",
]

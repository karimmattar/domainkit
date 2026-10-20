"""``diffsettings``: the settings that differ from the framework's defaults."""

import re
from argparse import ArgumentParser
from typing import Final

from domainkit.conf import unwrap_settings
from domainkit.conf.global_settings import HIDDEN
from domainkit.management.base import BaseCommand

SECRET_KEY: Final = re.compile(r"pass|secret|token|key|credential|auth", re.IGNORECASE)
"""Names of free-form entries (``CONNECT_ARGS``, ``LOGGING``) shown hidden."""
URL_PASSWORD: Final = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", re.I)
"""The password in a URL written into a plain string."""
UNSHOWN: Final = frozenset({"SETTINGS_MODULE"})


class Command(BaseCommand):
    """Print ``NAME = value`` for each changed setting; ``--all`` for every one.

    Secrets stay hidden, as in ``repr(settings)``: secret values (URLs,
    tokens), domain settings (``AUTH``, ``EVIDENCE``, ...) as a whole, and
    settings or entries of free-form settings whose names suggest secrets
    (a project's ``PAYMENT_API_KEY``), or passwords in URLs written as plain
    text.
    """

    help = "Show settings that differ from the defaults (--all: every setting)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept ``--all``."""
        parser.add_argument(
            "--all",
            dest="every",
            action="store_true",
            help="show every setting; defaults are marked with ###",
        )

    def handle(self, *, every: bool, **options: object) -> None:
        """Print one line per setting, sorted by name."""
        current = unwrap_settings()
        values = current.model_dump(mode="json")
        defaults = type(current).model_construct().model_dump(mode="json")
        self.write(f"# {current.SETTINGS_MODULE}: settings that differ from defaults")
        for name in sorted(set(values) - UNSHOWN):
            value = _scrub(values[name])
            unchanged = name in defaults and _scrub(defaults[name]) == value
            if SECRET_KEY.search(name):
                value = HIDDEN
            if unchanged and not every:
                continue
            mark = "### " if unchanged else ""
            self.write(f"{mark}{name} = {value!r}")


def _scrub(value: object) -> object:
    """``value`` with secret-looking entries and URL passwords hidden."""
    if isinstance(value, dict):
        return {
            key: HIDDEN if SECRET_KEY.search(str(key)) else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return URL_PASSWORD.sub(rf"\g<1>{HIDDEN}@", value)
    return value

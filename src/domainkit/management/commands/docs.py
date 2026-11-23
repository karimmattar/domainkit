"""``docs``: the link to a page of the documentation, for the release installed.

Docstrings, error messages, ``check``'s hints, and the notes the templates
write name pages as ``docs/<page>.md`` and decisions as ``ADR NNNN``. The
wheel holds neither (ADR 0152): this command prints where each one is for
the release installed, under the release's tag, which nobody moves or
deletes::

    domainkit docs docs/live.md     # .../blob/v2.10.0/docs/live.md
    domainkit docs "ADR 0057"       # the index of decisions of 2.10.0
    domainkit docs                  # the first page of 2.10.0's

The index of decisions has a row per record, which links it and says
what amended or replaced it. Like ``init``, the command needs no project;
in one, ``python manage.py docs`` does the same. From a source checkout,
whose version is no release, the links name ``main`` (``docs_url``).
"""

import re
from argparse import ArgumentParser
from typing import Final

from domainkit import __version__
from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.commands.init import docs_url

HOME: Final = "index.md"
"""The page a call that names nothing opens: the documentation's first."""
DECISIONS: Final = "adr/README.md"
"""The index of decisions: a row per record, linking it, with its status."""
PAGE: Final = re.compile(
    r"(?:docs/)?(?P<page>[\w-][\w.-]*(?:/[\w-][\w.-]*)*\.md)(?:#(?P<anchor>[\w-]+))?"
)
"""A page, as the code names it (``docs/live.md``) or without ``docs/``,
with its anchor if any; no part of its path starts with a dot."""
DECISION: Final = re.compile(r"ADR ?\d{4}", re.IGNORECASE)
"""A decision, as the code cites it: ``ADR 0057``."""


def link(reference: str, version: str) -> str:
    """Where ``reference`` is in the documentation of ``version``.

    A page, ``docs/live.md`` or ``live.md``, is that page, at its anchor if
    one is given; a decision, ``ADR 0057``, is the index of decisions.

    Raises:
        CommandError: ``reference`` is neither.
    """
    reference = reference.strip()
    if DECISION.fullmatch(reference):
        return f"{docs_url(version)}/{DECISIONS}"
    if match := PAGE.fullmatch(reference):
        anchor = f"#{match['anchor']}" if match["anchor"] else ""
        return f"{docs_url(version)}/{match['page']}{anchor}"
    raise CommandError(
        f"{reference!r} names no page and no decision: give a page, as "
        "docs/live.md, with its #anchor if any, or a decision, as 'ADR 0057'."
    )


class Command(BaseCommand):
    """Print the link to each page or decision named, for the release installed."""

    help = (
        "Print the link to a page of the documentation (docs/live.md) or to a "
        "decision (ADR 0057), for the release installed."
    )
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the pages and decisions to link."""
        parser.add_argument(
            "references",
            nargs="*",
            metavar="reference",
            help="a page, docs/live.md, with its #anchor if any, or a decision, "
            "'ADR 0057' (default: the first page)",
        )

    def handle(self, *, references: list[str], **options: object) -> None:
        """Print one link per reference, after checking every one."""
        links = [link(reference, __version__) for reference in references or [HOME]]
        self.write("\n".join(links))

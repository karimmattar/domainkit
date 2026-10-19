"""``squashmigrations``: replace a run of one target's revisions with one."""

from argparse import ArgumentParser

from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db.squashing import SquashRefused, squash
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Write one revision doing what the run did, and delete the run's files.

    The squashed revision keeps the newest revision's id, so databases at
    or past it are unaffected. Every database must be at or past it first;
    the command checks those in ``DATABASES`` (see ``domainkit.db.squashing``
    for what else it refuses).
    """

    help = (
        "Squash a target's revisions, START (default: the first) to END "
        "(default: the head), into one revision that keeps END's id."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the run's ends, a target, and a message."""
        parser.add_argument(
            "end", nargs="?", help="the newest revision to squash (default: head)"
        )
        parser.add_argument(
            "--start", help="the oldest revision to squash (default: the first)"
        )
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="the target whose history to squash (default: default)",
        )
        parser.add_argument(
            "-m", "--message", default="squashed", help="the squashed revision's name"
        )

    def handle(
        self,
        *,
        end: str | None,
        start: str | None,
        database: str,
        message: str,
        **options: object,
    ) -> None:
        """Squash, or explain why not."""
        try:
            path, replaced = squash(database, start=start, end=end, message=message)
        except (SquashRefused, ValueError) as exc:
            raise CommandError(f"Not squashed: {exc}") from None
        self.write(f"Created {path}, replacing {len(replaced)} revisions:")
        for revision in replaced:
            self.write(f"  {revision}")

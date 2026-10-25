"""``auditforget``: make people's pseudonyms in the audit trail unlinkable."""

from argparse import ArgumentParser
from datetime import timedelta

from domainkit import clock
from domainkit.contrib.audit.personal import forget, forget_anonymous
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Delete the keys of subjects, or of anonymous callers' old days.

    Each run is recorded in the audit trail (``operator.auditforget``) with
    how many subjects and keys, not which subjects.
    """

    help = (
        "Forget subjects (user ids) in the audit trail, in every audited "
        "database: their pseudonyms can no longer be linked to them."
    )
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept subjects and a retention period for anonymous callers."""
        parser.add_argument("subjects", nargs="*", metavar="subject")
        parser.add_argument(
            "--anonymous-days",
            type=int,
            metavar="DAYS",
            help="keep anonymous callers' addresses for DAYS days (UTC) and "
            "forget older ones",
        )

    async def handle(
        self, *, subjects: list[str], anonymous_days: int | None, **options: object
    ) -> None:
        """Delete the keys and say how many there were."""
        if not subjects and anonymous_days is None:
            raise CommandError("Name subjects to forget, or --anonymous-days.")
        if anonymous_days is not None and anonymous_days < 0:
            raise CommandError("--anonymous-days cannot be negative.")
        async with self.recorded(
            "auditforget", subjects=len(subjects), anonymous_days=anonymous_days
        ) as action:
            forgotten = await forget(*subjects)
            if anonymous_days is not None:
                before = clock.today() - timedelta(days=anonymous_days)
                forgotten += await forget_anonymous(before)
            action["keys"] = forgotten
        self.write(f"Forgot {forgotten} key{'' if forgotten == 1 else 's'}.")

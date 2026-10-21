"""``auditseal``: seal new audit rows into the hash chain, and anchor its head."""

from argparse import ArgumentParser

from domainkit.contrib.audit.seals import seal_all
from domainkit.management import BaseCommand


class Command(BaseCommand):
    """Seal every audited database; anchor each head when ``AUDIT.LEDGER`` is set."""

    help = (
        "Seal audit rows not sealed yet into each database's hash chain, and "
        "anchor its head on AUDIT.LEDGER. The worker does this every "
        "AUDIT.SEAL_EVERY; run this on a schedule without one."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept ``--late``."""
        parser.add_argument(
            "--late",
            action="store_true",
            help=(
                "also seal rows that committed later than AUDIT.SEAL_LATE_WINDOW, "
                "flagged as sealed late (reads the whole trail)"
            ),
        )

    async def handle(self, *, late: bool, **options: object) -> None:
        """Print what each database got."""
        for database, (sealed, anchored) in (await seal_all(late=late)).items():
            line = f"{database}: sealed {sealed} row{'' if sealed == 1 else 's'}"
            if anchored is not None:
                line += f", anchored #{anchored.seq}"
            self.write(f"{line}.")

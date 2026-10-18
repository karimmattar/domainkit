"""``auditseal``: seal new audit rows into the hash chain, and anchor its head."""

from domainkit.contrib.audit.seals import seal_all
from domainkit.management import BaseCommand


class Command(BaseCommand):
    """Seal every audited database; anchor each head when ``AUDIT.LEDGER`` is set."""

    help = (
        "Seal audit rows not sealed yet into each database's hash chain, and "
        "anchor its head on AUDIT.LEDGER. The worker does this every "
        "AUDIT.SEAL_EVERY; run this on a schedule without one."
    )

    async def handle(self, **options: object) -> None:
        """Print what each database got."""
        for database, (sealed, anchored) in (await seal_all()).items():
            line = f"{database}: sealed {sealed} row{'' if sealed == 1 else 's'}"
            if anchored is not None:
                line += f", anchored #{anchored.seq}"
            self.write(f"{line}.")

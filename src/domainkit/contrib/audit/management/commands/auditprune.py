"""``auditprune``: delete old audit rows below a signed checkpoint the chain keeps."""

from argparse import ArgumentParser
from datetime import timedelta

from domainkit import clock
from domainkit.apps import registry
from domainkit.contrib.audit.personal import audited_databases
from domainkit.contrib.audit.pruning import PruneRefused, prune
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.ledger import ledgers
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Prune each audited database's trail, keeping ``--keep-days`` of it.

    Each run is recorded in the audit trail (``operator.auditprune``), and
    each database pruned too (``audit.pruned``, with the checkpoint's seal
    and the rows deleted).
    """

    help = (
        "Delete audit rows sealed more than --keep-days ago, below a checkpoint "
        "signed with AUDIT.CHECKPOINT_KEY that auditverify then starts from. "
        "On PostgreSQL it runs as the tables' owner (MIGRATION_URL)."
    )
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept how long to keep, and one database."""
        parser.add_argument(
            "--keep-days",
            type=int,
            required=True,
            metavar="DAYS",
            help="keep the rows sealed in the last DAYS days (at least 1)",
        )
        parser.add_argument(
            "--database", help="the alias to prune (default: every audited one)"
        )

    async def handle(
        self, *, keep_days: int, database: str | None, **options: object
    ) -> None:
        """Prune, and print what each database lost."""
        if keep_days < 1:
            raise CommandError("--keep-days is at least 1.")
        config = registry.settings(AuditSettings)
        ledger = ledgers[config.LEDGER] if config.LEDGER is not None else None
        before = clock.now() - timedelta(days=keep_days)
        async with self.recorded("auditprune", keep_days=keep_days) as action:
            action["rows"] = 0
            for alias in (database,) if database else audited_databases():
                try:
                    pruned = await prune(alias, before=before, ledger=ledger)
                except PruneRefused as exc:
                    raise CommandError(str(exc)) from None
                action["rows"] = int(str(action["rows"])) + pruned.rows
                if pruned.checkpoint is None:
                    self.write(f"{alias}: nothing to prune.")
                else:
                    self.write(
                        f"{alias}: pruned {pruned.rows} "
                        f"row{'' if pruned.rows == 1 else 's'} below seal "
                        f"#{pruned.checkpoint.seq}."
                    )

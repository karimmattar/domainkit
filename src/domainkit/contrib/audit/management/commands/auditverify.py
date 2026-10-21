"""``auditverify``: check the audit trail against its hash chain and anchors."""

from argparse import ArgumentParser

from domainkit.apps import registry
from domainkit.contrib.audit.personal import audited_databases
from domainkit.contrib.audit.seals import Report, verify
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.ledger import ledgers
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Verify every audited database, or one; fail when anything is wrong."""

    help = (
        "Check each audited database's rows against their seals, the seals' "
        "links, and the heads anchored on AUDIT.LEDGER."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept one database to check."""
        parser.add_argument(
            "--database", help="the alias to check (default: every audited one)"
        )

    async def handle(self, *, database: str | None, **options: object) -> None:
        """Print a line per database, and the problems found."""
        config = registry.settings(AuditSettings)
        ledger = ledgers[config.LEDGER] if config.LEDGER is not None else None
        failed = []
        for alias in (database,) if database else audited_databases():
            report = await verify(alias, ledger)
            self.write(_summary(report))
            if not report.intact:
                failed.append(alias)
        if failed:
            raise CommandError(f"The audit trail failed verification: {failed}.")


def _summary(report: Report) -> str:
    """One line on the report, then one per kind of problem."""
    lines = [
        f"{report.database}: {report.sealed} seals, {report.anchors} anchors: "
        + ("intact." if report.intact else "NOT INTACT.")
    ]
    for name, found in (
        ("changed rows", report.changed),
        ("missing rows", report.missing),
        ("broken links at", report.broken),
        ("rewritten anchors at", report.rewritten),
    ):
        if found:
            lines.append(f"  {name}: {', '.join(str(each) for each in found)}")
    if report.unanchored:
        lines.append(f"  seals left unanchored: {report.unanchored}")
    if report.unsealed:
        lines.append(f"  unsealed rows: {report.unsealed}")
    if report.late:
        lines.append(f"  rows sealed late (open to change for longer): {report.late}")
    return "\n".join(lines)

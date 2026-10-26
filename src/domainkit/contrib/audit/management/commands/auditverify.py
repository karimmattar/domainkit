"""``auditverify``: check the audit trail against its hash chain and anchors.

``--export FILE`` also writes signed checkpoints of every trail that
verifies, and ``--check-export FILE --public-key PEM`` checks such a file
offline, with nothing but the public key.
"""

import json
import os
from argparse import ArgumentParser
from pathlib import Path

from domainkit.apps import registry
from domainkit.contrib.audit.checkpoints import CheckpointInvalid, check_export
from domainkit.contrib.audit.personal import audited_databases
from domainkit.contrib.audit.pruning import ExportRefused, export
from domainkit.contrib.audit.seals import Report, verify
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.ledger import Ledger, ledgers
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Verify every audited database, or one; fail when anything is wrong."""

    help = (
        "Check each audited database's rows against their seals, the seals' "
        "links, and the heads anchored on AUDIT.LEDGER. --export writes signed "
        "checkpoints for a verifier holding only the public key; --check-export "
        "checks such a file."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept one database to check, or a file to export to or check."""
        parser.add_argument(
            "--database", help="the alias to check (default: every audited one)"
        )
        parser.add_argument(
            "--export",
            type=Path,
            metavar="FILE",
            help="then write signed checkpoints of every trail to FILE, a new "
            "file only its owner can read (needs AUDIT.CHECKPOINT_KEY)",
        )
        parser.add_argument(
            "--check-export",
            type=Path,
            metavar="FILE",
            help="check an export offline instead, with --public-key",
        )
        parser.add_argument(
            "--public-key",
            type=Path,
            metavar="PEM",
            help="the Ed25519 public key that signed the export",
        )

    async def handle(
        self,
        *,
        database: str | None,
        export: Path | None,
        check_export: Path | None,
        public_key: Path | None,
        **options: object,
    ) -> None:
        """Print a line per database, and the problems found."""
        if check_export is not None:
            self._check(check_export, public_key)
            return
        if export is not None and database is not None:
            raise CommandError("--export covers every audited database.")
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
        if export is not None:
            await self._export(export, ledger)

    async def _export(self, path: Path, ledger: Ledger | None) -> None:
        """Write the export to ``path``, a new file only its owner can read."""
        try:
            document = await export(ledger)
        except ExportRefused as exc:
            raise CommandError(str(exc)) from None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CommandError(f"{path} exists; name a new file.") from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(document, indent=2) + "\n")
        count = sum(
            len(found["checkpoints"]) for found in document["databases"].values()
        )
        self.write(
            f"Wrote {count} signed checkpoint{'' if count == 1 else 's'} to {path} "
            f"(key {document['key_id']})."
        )

    def _check(self, path: Path, public_key: Path | None) -> None:
        """Check an export offline; print each database's checkpoints."""
        if public_key is None:
            raise CommandError("--check-export needs --public-key.")
        try:
            checked = check_export(path.read_bytes(), public_key.read_bytes())
        except CheckpointInvalid as exc:
            raise CommandError(f"{path} does not verify: {exc}") from None
        for database, chain in checked.items():
            newest = f", newest at seal #{chain[-1].seq}" if chain else ""
            self.write(
                f"{database}: {len(chain)} checkpoint{'' if len(chain) == 1 else 's'}"
                f" verified{newest}."
            )


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
        ("checkpoints that do not verify at", report.forged),
    ):
        if found:
            lines.append(f"  {name}: {', '.join(str(each) for each in found)}")
    if report.unanchored:
        lines.append(f"  seals left unanchored: {report.unanchored}")
    if report.unsealed:
        lines.append(f"  unsealed rows: {report.unsealed}")
    if report.late:
        lines.append(f"  rows sealed late (open to change for longer): {report.late}")
    if report.pruned:
        lines.append(f"  seals pruned below checkpoints: {report.pruned}")
    return "\n".join(lines)

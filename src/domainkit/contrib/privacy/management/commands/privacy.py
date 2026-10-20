"""``privacy``: export or erase a person's data, or purge what is kept too long."""

import json
import os
from argparse import ArgumentParser
from pathlib import Path

from domainkit.contrib.privacy import (
    check_reason,
    check_subject,
    erase_subject,
    export_subject,
    purge,
)
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Answer privacy requests across installed domains, and apply retention."""

    help = (
        "Export (export --subject ID) or erase (erase --subject ID) the "
        "personal data of one person in every installed domain, or delete "
        "and erase what each domain's RETENTION no longer keeps (purge)."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the three actions and their options."""
        actions = parser.add_subparsers(dest="action", required=True)
        export = actions.add_parser("export", help="write a person's data as JSON")
        export.add_argument("--subject", required=True, help="whose: their id")
        export.add_argument(
            "--output",
            type=Path,
            help="a new file to write it to (mode 0600); default: standard output",
        )
        erase = actions.add_parser("erase", help="erase a person's data")
        erase.add_argument("--subject", required=True, help="whose: their id")
        erase.add_argument(
            "--reason",
            default="request",
            help="recorded with each erasure (default: request), such as a ticket",
        )
        erase.add_argument(
            "--noinput", action="store_true", help="do not ask for confirmation"
        )
        purge_parser = actions.add_parser("purge", help="apply every RETENTION")
        purge_parser.add_argument(
            "--dry-run", action="store_true", help="only count what would go"
        )

    async def handle(
        self,
        *,
        action: str,
        subject: str | None = None,
        output: Path | None = None,
        reason: str = "request",
        noinput: bool = False,
        dry_run: bool = False,
        **options: object,
    ) -> None:
        """Run the action."""
        if action == "purge":
            await self._purge(dry_run=dry_run)
            return
        try:
            subject = check_subject(subject or "")
            check_reason(reason)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        if action == "export":
            await self._export(subject, output)
            return
        self._confirm(subject, noinput=noinput)
        erased = await erase_subject(subject, reason=reason)
        self.write(
            f"Erased the personal data of {subject} in {erased} "
            f"aggregate{'' if erased == 1 else 's'}, and forgot their audit keys."
        )

    async def _export(self, subject: str, output: Path | None) -> None:
        """Write the export to ``output``, a new private file, or standard output."""
        text = json.dumps(await export_subject(subject), indent=2, ensure_ascii=False)
        if output is None:
            self.write(text)
            return
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CommandError(f"{output} exists; name a new file.") from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text + "\n")
        self.write(f"Wrote the personal data of {subject} to {output}.")

    def _confirm(self, subject: str, *, noinput: bool) -> None:
        """Ask for the subject again before erasing, unless ``noinput``."""
        if noinput:
            return
        answer = input(
            f"This erases the personal data of {subject} in every installed "
            "domain, for good. Type the subject again to go on: "
        )
        if answer != subject:
            raise CommandError("Erasure cancelled.")

    async def _purge(self, *, dry_run: bool) -> None:
        """Purge, print a line per table and database; fail if any was refused."""
        results = await purge(dry_run=dry_run)
        for result in results:
            line = (
                f"{result.policy} on {result.database}: {result.action} "
                f"{result.count} row(s) from before {result.before.isoformat()}"
            )
            if result.refused:
                line += f"; refused, {result.refused}"
            self.write(line)
        if not results:
            self.write("No domain declares a RETENTION.")
        if refused := [result.policy for result in results if result.refused]:
            raise CommandError(f"Not purged: {', '.join(refused)}.")

"""``privacy``: export or erase a person's data, or purge what is kept too long."""

import json
import os
from pathlib import Path

from domainkit.contrib.privacy import (
    ErasureHeld,
    check_reason,
    check_subject,
    erase_subject,
    export_subject,
    purge,
)
from domainkit.management import ActionCommand, CommandError, action, option


class Command(ActionCommand):
    """Answer privacy requests across installed domains, and apply retention."""

    help = (
        "Export (export --subject ID) or erase (erase --subject ID) the "
        "personal data of one person in every installed domain, or delete "
        "and erase what each domain's retention no longer keeps (purge)."
    )
    operator_actions = True
    """Exports, erasures, and purges are audited as the operator who ran them."""

    @action(
        help="write a person's data as JSON",
        options=[
            option("--subject", required=True, help="whose: their id"),
            option(
                "--output",
                type=Path,
                help="a new file to write it to (mode 0600); default: standard output",
            ),
        ],
    )
    async def export(self, *, subject: str, output: Path | None) -> None:
        """Write the person's data."""
        await self._export(_subject(subject), output)

    @action(
        help="erase a person's data",
        options=[
            option("--subject", required=True, help="whose: their id"),
            option(
                "--reason",
                default="request",
                help="recorded with each erasure (default: request), such as a ticket",
            ),
            option(
                "--noinput", action="store_true", help="do not ask for confirmation"
            ),
        ],
    )
    async def erase(self, *, subject: str, reason: str, noinput: bool) -> None:
        """Erase the person's data, once confirmed.

        Raises:
            CommandError: A bad subject or reason, no confirmation, or a domain
                that keeps the data.
        """
        subject = _subject(subject)
        try:
            check_reason(reason)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self._confirm(subject, noinput=noinput)
        try:
            erased = await erase_subject(subject, reason=reason)
        except ErasureHeld as exc:
            reasons = "; ".join(f"{domain}: {why}" for domain, why in exc.held.items())
            raise CommandError(f"Nothing was erased. Kept by {reasons}") from None
        self.write(
            f"Erased the personal data of {subject} in {erased} "
            f"aggregate{'' if erased == 1 else 's'}, and forgot their audit keys."
        )

    @action(
        help="apply every domain's retention",
        options=[
            option("--dry-run", action="store_true", help="only count what would go")
        ],
    )
    async def purge(self, *, dry_run: bool) -> None:
        """Purge, and print a line per table and database."""
        await self._purge(dry_run=dry_run)

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
            self.write("No domain declares a retention.")
        if refused := [result.policy for result in results if result.refused]:
            raise CommandError(f"Not purged: {', '.join(refused)}.")


def _subject(text: str) -> str:
    """``text``, checked as a subject's id.

    Raises:
        CommandError: It is not one.
    """
    try:
        return check_subject(text)
    except ValueError as exc:
        raise CommandError(str(exc)) from None

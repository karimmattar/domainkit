"""``evidence``: list a subject's evidence, or verify one record."""

from argparse import ArgumentParser

from domainkit.contrib.evidence import EvidenceTampered, evidence_store
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Read evidence back from the ledger, verified."""

    help = "List a subject's evidence, or verify one record by key."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a subject to list, or a key to verify."""
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--subject", help="list the evidence about this subject")
        mode.add_argument("--verify", metavar="KEY", help="verify the record under KEY")
        parser.add_argument("--limit", type=int, default=50)

    async def handle(
        self,
        *,
        subject: str | None,
        verify: str | None,
        limit: int,
        **options: object,
    ) -> None:
        """Print one line per record, or the verified record."""
        store = evidence_store()
        if verify is not None:
            recorded = await store.get(verify)
            try:
                await store.verify(recorded.receipt)
            except EvidenceTampered as exc:
                raise CommandError(str(exc)) from exc
            evidence = recorded.evidence
            self.write(
                f"Intact: {evidence.kind} about {evidence.subject} at "
                f"{evidence.recorded_at:%Y-%m-%d %H:%M:%S}, {recorded.receipt.digest}, "
                f"transaction {recorded.receipt.transaction}."
            )
            return
        # The required option group guarantees a subject when not verifying.
        for recorded in await store.for_subject(subject or "", limit=limit):
            state = "intact" if recorded.intact else "WRITTEN AGAIN"
            self.write(
                f"{recorded.receipt.key}  {recorded.evidence.kind}  "
                f"{recorded.evidence.recorded_at:%Y-%m-%d %H:%M:%S}  {state}"
            )

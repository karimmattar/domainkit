"""``evidence``: list a subject's evidence, or verify one record."""

from domainkit.contrib.evidence import EvidenceTampered, evidence_store
from domainkit.management import ActionCommand, CommandError, action, option


class Command(ActionCommand):
    """Read evidence back from the ledger, verified."""

    help = "List a subject's evidence, or verify one record by key."

    @action(
        name="list",
        help="list the evidence about a subject",
        options=[
            option("subject"),
            option(
                "--limit", type=int, default=50, help="at most this many (default: 50)"
            ),
        ],
    )
    async def listed(self, *, subject: str, limit: int) -> None:
        """Print one line per record, and whether it is intact."""
        for recorded in await evidence_store().for_subject(subject, limit=limit):
            state = "intact" if recorded.intact else "WRITTEN AGAIN"
            self.write(
                f"{recorded.receipt.key}  {recorded.evidence.kind}  "
                f"{recorded.evidence.recorded_at:%Y-%m-%d %H:%M:%S}  {state}"
            )

    @action(help="verify the record under a key", options=[option("key")])
    async def verify(self, *, key: str) -> None:
        """Print the verified record.

        Raises:
            CommandError: The record was written again since it was recorded.
        """
        store = evidence_store()
        recorded = await store.get(key)
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

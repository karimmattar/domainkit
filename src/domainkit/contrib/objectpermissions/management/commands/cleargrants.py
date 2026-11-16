"""``cleargrants``: delete the grants on objects past their expiry."""

from domainkit.management.base import BaseCommand

from ...services import clear_expired_grants


class Command(BaseCommand):
    """Delete expired grants on objects, in every database that holds grants."""

    help = "Delete the grants on objects past their expiry."
    operator_actions = True

    async def handle(self) -> None:
        """Delete them and say how many."""
        count = await clear_expired_grants()
        self.write(f"Deleted {count} expired grant(s).")

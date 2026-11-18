"""Installation hooks for the wallets domain (Django's ``AppConfig``)."""

from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_seed

from domains.wallets import controllers


class WalletsConfig(DomainConfig):
    """Open the books with ``manage.py seed``."""

    requires = ("domainkit.contrib.accounting", "domainkit.contrib.idempotency")

    def ready(self) -> None:
        """Register the seed of the periods and the business's accounts.

        The worker opens them each day too (``open_books`` is ``@periodic``).
        """
        on_seed(controllers.open_books)

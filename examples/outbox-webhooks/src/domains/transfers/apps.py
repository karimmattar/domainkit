"""Installation hooks for the transfers domain (Django's ``AppConfig``)."""

from domainkit.apps import DomainConfig
from domainkit.contrib.webhooks import expose
from domainkit.ddd import event_bus

from domains.transfers.domain import TransferRequested, TransferSent
from domains.transfers.models import TransfersSettings, send_transfer, start_mappers


class TransfersConfig(DomainConfig):
    """Map the table, send requested transfers, and tell subscribers of sent ones."""

    requires = ("domainkit.contrib.messaging", "domainkit.contrib.webhooks")
    settings_class = TransfersSettings

    def ready(self) -> None:
        """Connect the layers once settings are loaded.

        Both reactions are durable: written to the outbox with the commit
        that recorded their event, run and retried by the worker.
        """
        start_mappers()
        event_bus.subscribe(
            TransferRequested,
            send_transfer,
            durable=True,
            name="transfers.send_to_bank",
        )
        # The body holds no personal data: a receiver keeps what it gets.
        expose(
            TransferSent,
            "transfer.sent",
            payload=lambda event: {
                "transfer_id": str(event.transfer_id),
                "amount": str(event.amount.amount),
                "currency": event.amount.currency.code,
                "bank_reference": event.bank_reference,
            },
        )

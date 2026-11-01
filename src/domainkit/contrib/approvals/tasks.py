"""The worker's part: running approved commands, and expiring requests.

``EXECUTE`` runs under the inbox (``@inbox``, ADR 0094): a message
delivered twice commits its command once. The approval that completes a
request queues it in its own transaction, so it is queued once.
"""

from typing import Final

from domainkit.contrib.messaging import inbox, periodic, task

from .exceptions import RequestNotFound
from .services import EXECUTE, execute_request, expire_requests
from .settings import ApprovalsSettings

EXPIRE: Final = "domainkit.approvals.expire"
"""The periodic task that expires the requests whose time is up."""


async def expire_due_requests() -> None:
    """Expire the requests whose time is up (the periodic task ``EXPIRE``)."""
    await expire_requests()


def register(config: ApprovalsSettings) -> None:
    """Register the run of approved commands, under the inbox, and the expiry.

    ``execute_request`` records a refused command as the request's failure
    itself; a request gone is dead-lettered at once, and anything else is
    retried with the worker's backoff.
    """
    inbox(execute_request)
    task(name=EXECUTE, dont_retry_on=(RequestNotFound,))(execute_request)
    periodic(config.EXPIRE_EVERY, name=EXPIRE)(expire_due_requests)

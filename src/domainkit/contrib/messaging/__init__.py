"""Transactional outbox, durable event handlers, and background tasks.

Install it, then run ``python manage.py runworker`` next to the web process::

    INSTALLED_DOMAINS = ["domainkit.contrib.messaging", ...]

    @task(max_attempts=5, backoff=Backoff(base=1, cap=60), timeout=30)
    async def send_welcome(user_id: UUID) -> None: ...

    async with uow:                       # in a use case
        ...
        uow.defer(send_welcome, user_id=user.id)
        await uow.commit()                # the task is stored with the change

    uow.defer(chain(charge, send_receipt), order_id=order.id)  # one after another

    event_bus.subscribe(UserCreated, handler, durable=True)   # in ready()

    @inbox                                # its database work commits once
    async def credit_account(event: PaymentReceived) -> None: ...

    @periodic(every=timedelta(hours=1))
    async def purge_expired() -> None: ...

    @periodic(cron="0 3 * * 1-5", tz="Africa/Cairo")
    async def nightly_report() -> None: ...
"""

from .budget import Budget
from .outbox import enqueue
from .registry import Chain, chain, periodic, task
from .retry import Backoff
from .runs import AlreadyHandled, LeaseLost, inbox
from .settings import MessagingSettings
from .worker import Attempt, Worker, current_attempt

__all__ = [
    "AlreadyHandled",
    "Attempt",
    "Backoff",
    "Budget",
    "Chain",
    "LeaseLost",
    "MessagingSettings",
    "Worker",
    "chain",
    "current_attempt",
    "enqueue",
    "inbox",
    "periodic",
    "task",
]

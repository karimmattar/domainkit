"""Transactional outbox, durable event handlers, and background tasks.

Install it, then run ``python manage.py runworker`` next to the web process::

    INSTALLED_DOMAINS = ["domainkit.contrib.messaging", ...]

    @task
    async def send_welcome(user_id: UUID) -> None: ...

    async with uow:                       # in a use case
        ...
        uow.defer(send_welcome, user_id=user.id)
        await uow.commit()                # the task is stored with the change

    event_bus.subscribe(UserCreated, handler, durable=True)   # in ready()

    @periodic(every=timedelta(hours=1))
    async def purge_expired() -> None: ...
"""

from .outbox import enqueue
from .registry import periodic, task
from .settings import MessagingSettings
from .worker import Worker

__all__ = ["MessagingSettings", "Worker", "enqueue", "periodic", "task"]

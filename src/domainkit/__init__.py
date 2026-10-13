"""domainkit: a Django-style framework for domain-driven FastAPI services.

Layers, lowest first; a module may import only from its own layer or below:

1. ``exceptions``, ``utils``, ``ddd``: no settings, no I/O. ``ddd`` holds the
   domain building blocks (entities, value objects, events, repositories).
2. ``conf``: settings loading and validation.
3. ``apps``, ``context``, ``log``, ``reporting``: the domain registry and
   observability.
4. ``db``, ``http``: SQLAlchemy and FastAPI integration.
5. ``management``: the ``manage.py`` commands.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("domainkit")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"


def setup() -> None:
    """Configure logging and load the installed domains (``django.setup()``).

    Safe to call more than once; later calls do nothing.
    """
    from domainkit.apps import registry
    from domainkit.conf import settings
    from domainkit.ddd import event_bus
    from domainkit.log import configure_logging
    from domainkit.reporting import report_error, reporters

    if registry.ready:
        return
    configure_logging(settings.LOGGING)
    reporters()
    event_bus.error_hook = lambda exc, event: report_error(
        exc, event=type(event).__qualname__, event_id=str(event.event_id)
    )
    registry.populate(settings.INSTALLED_DOMAINS)

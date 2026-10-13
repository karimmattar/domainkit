"""Events of the alpha sample domain."""

from domainkit.ddd import DomainEvent


class GreetingSent(DomainEvent):
    """An event whose action name comes from the domain label."""

    to: str

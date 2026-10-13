"""Commands and queries: the inputs of application use cases.

Both are frozen, keyword-only dataclasses that carry plain values. A command
asks the domain to change (``RenameProduct``); a query asks for data and
changes nothing (``ListProducts``).
"""

from domainkit.ddd._dataclass import MessageMeta


class Command(metaclass=MessageMeta):
    """Request to change state, named in the imperative."""


class Query(metaclass=MessageMeta):
    """Request for data that changes nothing."""

"""Commands and queries: the inputs of application use cases.

Both are frozen, keyword-only dataclasses that carry plain values. A command
asks the domain to change (``RenameProduct``); a query asks for data and
changes nothing (``ListProducts``).

A command that changes only some fields defaults the others to ``UNSET``,
so "leave it as it is" differs from "set it to None"::

    class UpdateProduct(Command):
        product_id: UUID
        name: str | Unset = UNSET
        description: str | None | Unset = UNSET

    if command.name is not UNSET:
        product.rename(ProductName(command.name))
"""

from domainkit.ddd._dataclass import MessageMeta
from domainkit.utils.unset import UNSET as UNSET
from domainkit.utils.unset import Unset as Unset


class Command(metaclass=MessageMeta):
    """Request to change state, named in the imperative."""


class Query(metaclass=MessageMeta):
    """Request for data that changes nothing."""

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

``fingerprint`` names a command by its action and arguments: the SHA-256 of
both as canonical JSON. Approvals (``domainkit.contrib.approvals``) and
action authorizations (``domainkit.contrib.auth.actions``) bind what they
approve to it, so both compute it one way.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from domainkit.ddd._dataclass import MessageMeta
from domainkit.utils.unset import UNSET as UNSET
from domainkit.utils.unset import Unset as Unset


class Command(metaclass=MessageMeta):
    """Request to change state, named in the imperative."""


class Query(metaclass=MessageMeta):
    """Request for data that changes nothing."""


def fingerprint(action: str, arguments: Mapping[str, Any]) -> str:
    """SHA-256 of the action and the arguments, as canonical JSON (64 hex digits).

    Keys sorted, no spaces, so the same command always gives the same
    fingerprint, and an approver's client can work it out from what it shows.
    """
    text = json.dumps(
        {"action": action, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

"""A reference to an aggregate of any kind (Django's ``GenericForeignKey``).

A comment may be about an invoice or a payment; a tag may be on anything.
``ObjectRef`` names the aggregate's model, as content types name it
(``billing.invoice``), and its id::

    class Comment(AggregateRoot[CommentId]):
        about: ObjectRef
        text: str

    Comment(id=..., about=ref_to(invoice), text="Paid late")  # domainkit.db.refs

It is one column, ``billing.invoice:<id>``: ``F("about") == ref`` finds the
comments on one aggregate. ``domainkit.db.refs`` makes a reference to an
aggregate, finds a reference's class, and loads what it names.
"""

import re
from typing import Final

from domainkit.ddd.model import ValueObject

__all__ = ["ObjectRef"]

MODEL: Final = re.compile(r"[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{0,63}")
"""A model's key: its domain's label and its name, lower case."""
MAX_ID: Final = 64
"""Characters an id may have: a UUID, a number, a slug."""


class ObjectRef(ValueObject):
    """An aggregate of any installed model: its key, and its id as text."""

    model: str
    id: str

    def __post_init__(self) -> None:
        """Refuse a key that is not ``domain.model``, or an id that does not fit.

        Raises:
            ValueError: Either is not one.
        """
        if not MODEL.fullmatch(self.model):
            raise ValueError("an ObjectRef's model is 'domain.model', lower case")
        if not 0 < len(self.id) <= MAX_ID or ":" in self.id:
            raise ValueError(f"an ObjectRef's id has 1 to {MAX_ID} characters, no ':'")

    def __str__(self) -> str:
        """``billing.invoice:<id>``: as the column holds it."""
        return f"{self.model}:{self.id}"

    @classmethod
    def parse(cls, text: str) -> "ObjectRef":
        """The reference ``str(ref)`` wrote.

        Raises:
            ValueError: ``text`` is not ``domain.model:id``.
        """
        model, sep, id_ = text.partition(":")
        if not sep:
            raise ValueError("an ObjectRef is written 'domain.model:id'")
        return cls(model=model, id=id_)

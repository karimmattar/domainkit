"""DSGN-19: a derived schema does not show an encrypted field unless asked to.

``Encrypted`` keeps a card number unreadable at rest, in replicas, backups,
and dumps (``dumpdata`` writes it sealed unless ``--decrypt``, audited).
``schema_for``, which ``ResourceRouter(crud=True)`` and the admin build
their bodies with, puts it in the read schema by default, so the first
generated ``GET`` serves it in clear to anyone with the view permission.
``Sensitive`` values are refused by ``schema_for``; an ``Encrypted`` field
should at least need naming in ``include`` before it leaves.
"""

from typing import Annotated
from uuid import UUID

from domainkit.ddd import AggregateRoot, Encrypted
from domainkit.resources import schema_for


class Card(AggregateRoot[UUID]):
    """A card whose number is sealed at rest."""

    holder_id: UUID
    number: Annotated[str | None, Encrypted(subject="holder_id", blind_index=True)] = (
        None
    )


def test_the_default_read_schema_leaves_an_encrypted_field_out() -> None:
    """``CardOut`` has the id, version, and holder; not the number."""
    assert "number" not in schema_for(Card).model_fields


def test_an_encrypted_field_named_in_include_is_shown() -> None:
    """Naming it is the deliberate way to serve it."""
    assert "number" in schema_for(Card, include=["id", "number"]).model_fields

"""``ObjectRef`` columns, and references to the aggregates they name.

A field annotated ``ObjectRef`` is one text column, ``domain.model:id``.
``ref_to(aggregate)`` makes the reference, ``model_of(ref)`` finds the class
it names among the installed models, and ``load(uow, ref)`` reads the
aggregate in the unit of work's transaction.
"""

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from domainkit.db.models import installed_models, model_key
from domainkit.ddd.refs import MAX_ID, ObjectRef

__all__ = ["ObjectRefType", "load", "model_of", "ref_to"]


class ObjectRefType(TypeDecorator[ObjectRef]):
    """An ``ObjectRef``, as ``domain.model:id`` text."""

    impl = String(128 + 1 + MAX_ID)
    cache_ok = True

    def process_bind_param(
        self, value: ObjectRef | None, dialect: Dialect
    ) -> str | None:
        """The text the column holds."""
        return None if value is None else str(value)

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> ObjectRef | None:
        """The reference the column holds."""
        return None if value is None else ObjectRef.parse(value)


def ref_to(aggregate: object) -> ObjectRef:
    """A reference to ``aggregate``, an instance of an installed model.

    Raises:
        LookupError: Its class is not mapped in an installed domain.
    """
    domain, model = model_key(type(aggregate))
    return ObjectRef(model=f"{domain}.{model}", id=str(aggregate.id))  # type: ignore[attr-defined]


def model_of(ref: ObjectRef) -> type:
    """The class ``ref`` names.

    Raises:
        LookupError: No installed model has that key.
    """
    domain, _, model = ref.model.partition(".")
    for installed in installed_models():
        if installed.key == (domain, model):
            return installed.cls
    raise LookupError(f"No installed model is {ref.model!r}.")


async def load(uow: Any, ref: ObjectRef) -> Any:  # noqa: ANN401 - any aggregate.
    """The aggregate ``ref`` names, read in ``uow``'s transaction; None if gone.

    Raises:
        LookupError: No installed model has the key.
    """
    cls = model_of(ref)
    column = cls.__mapper__.primary_key[0]  # type: ignore[attr-defined]
    key: Any = column.type.python_type(ref.id)
    return await uow.session.get(cls, key)

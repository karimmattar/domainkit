"""Removing the stored files a committed change dropped, once no row keeps them.

A storage never deletes a file by itself: a change that replaces an
aggregate's file, or deletes the aggregate, leaves the old file behind.
Deleting it in the use case is wrong twice over: the transaction may
still roll back, and another row may hold the same name. So the use case
defers its removal, to run once the change has committed::

    from domainkit.storage.cleanup import remove_unreferenced

    uow.defer(remove_unreferenced, names=[document.file])
    document.file = stored.name
    await uow.commit()

The worker (``domainkit.contrib.messaging``, which registers the task as
``REMOVE_TASK``) runs it after the commit, and again after a failure; a
change that rolled back removes nothing. Before it deletes a name, it
asks every column that keeps names of that storage whether a row still
holds it, on the primary database, never a replica that may not have
seen the latest commit. A file a row still holds stays.

The columns are declared beside the tables, in the infrastructure layer,
once per column::

    keeps_files(DocumentRow.file, TemplateRow.preview)  # storage="default"

With no column declared for a storage, ``remove_unreferenced`` refuses to
delete anything. Like django-cleanup, it removes a file after the
commit; unlike it, it checks other rows first, as Active Storage's
attachments keep a blob (ADR 0169).
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Final

from domainkit.exceptions import ImproperlyConfigured

__all__ = ["REMOVE_TASK", "forget_files", "keeps_files", "remove_unreferenced"]

REMOVE_TASK: Final = "domainkit.storage.remove_unreferenced"
"""The name ``domainkit.contrib.messaging`` registers ``remove_unreferenced`` under."""
DEFAULT_STORAGE: Final = "default"

_kept: dict[str, list[Any]] = defaultdict(list)
"""The columns that keep names of each storage alias's files."""


def keeps_files(*columns: Any, storage: str = DEFAULT_STORAGE) -> None:  # noqa: ANN401 - mapped attributes of any model.
    """Declare that ``columns`` hold names of ``storage``'s files, one per row.

    Each is a mapped attribute (``DocumentRow.file``): its model's module
    says which database it is read from, as for a unit of work. A column
    declared again is kept once.

    Raises:
        TypeError: A column is not an attribute of a mapped class.
    """
    for column in columns:
        if not isinstance(getattr(column, "class_", None), type):
            raise TypeError(
                f"keeps_files takes mapped attributes, such as DocumentRow.file, "
                f"not {column!r}."
            )
        held = _kept[storage]
        if not any(column is each for each in held):
            held.append(column)


def forget_files(storage: str | None = None) -> None:
    """Forget the columns declared for ``storage``, or for every storage (tests)."""
    if storage is None:
        _kept.clear()
    else:
        _kept.pop(storage, None)


async def remove_unreferenced(names: list[str], storage: str = DEFAULT_STORAGE) -> None:
    """Delete each of ``names`` from ``storage`` that no row keeps.

    Every column ``keeps_files`` declared for ``storage`` is read on the
    primary database of its model. A name a row holds stays, and so does
    one already gone.

    Raises:
        ImproperlyConfigured: No column keeps names of ``storage``: nothing
            says which rows to ask, so nothing is deleted.
    """
    from domainkit.storage.handler import storages

    columns = _kept.get(storage)
    if not columns:
        raise ImproperlyConfigured(
            f"No column keeps names of the {storage!r} storage: declare them with "
            "keeps_files(), so that a file a row still holds is never removed."
        )
    held = await _held(names, columns)
    target = storages[storage]
    for name in dict.fromkeys(names):
        if name not in held:
            await target.delete(name)


async def _held(names: Sequence[str], columns: Iterable[Any]) -> set[str]:
    """The ``names`` a row of ``columns`` holds, read on each primary."""
    from sqlalchemy import select

    from domainkit.db.routing import router
    from domainkit.db.uow import SqlAlchemyUnitOfWork

    by_target: dict[str, list[Any]] = defaultdict(list)
    for column in columns:
        by_target[router.target_for_module(column.class_.__module__)].append(column)
    held: set[str] = set()
    for target, read in by_target.items():
        async with SqlAlchemyUnitOfWork(
            database=target, readonly=True, replica=False
        ) as uow:
            for column in read:
                found = await uow.session.scalars(
                    select(column).where(column.in_(names)).distinct()
                )
                held.update(found)
    return held

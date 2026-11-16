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
once per column: a table's own column, where ``table_for`` (or
``Table``) makes it, the framework's layout, a link table with no class
included; or a mapped attribute, where a declarative model defines it::

    documents = table_for(Document, "documents", domain="documents")
    keeps_files(documents.c.file, templates.c.preview)  # storage="default"

With no column declared for a storage, ``remove_unreferenced`` refuses to
delete anything. Like django-cleanup, it removes a file after the
commit; unlike it, it checks other rows first, as Active Storage's
attachments keep a blob (ADR 0169).
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Final

from domainkit.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from sqlalchemy import Column

__all__ = ["REMOVE_TASK", "forget_files", "keeps_files", "remove_unreferenced"]

REMOVE_TASK: Final = "domainkit.storage.remove_unreferenced"
"""The name ``domainkit.contrib.messaging`` registers ``remove_unreferenced`` under."""
DEFAULT_STORAGE: Final = "default"

type _Kept = tuple["Column[Any]", str | None]
"""A column that keeps names, and the module of the class mapped onto it, if any."""

_kept: dict[str, list[_Kept]] = defaultdict(list)
"""The columns that keep names of each storage alias's files."""


def keeps_files(*columns: Any, storage: str = DEFAULT_STORAGE) -> None:  # noqa: ANN401 - columns or mapped attributes.
    """Declare that ``columns`` hold names of ``storage``'s files, one per row.

    Each is a table's column (``documents.c.file``), or an attribute of a
    class mapped onto one (``DocumentRow.file``). Its table's domain
    (``info["domain"]``, which ``table_for(domain=...)`` sets) says which
    database it is read from; an untagged table is read where its mapped
    class's domain is, or on ``default``, as migrations place it. A column
    declared again is kept once.

    Raises:
        TypeError: A column is neither a table's column nor a mapped
            attribute: an entity's attribute before ``start_mappers()``
            mapped it, say.
    """
    for given in columns:
        kept = _column_of(given)
        held = _kept[storage]
        if not any(kept[0] is column for column, _ in held):
            held.append(kept)


def _column_of(given: object) -> _Kept:
    """The table's column ``given`` is, or is mapped onto, and the class's module.

    Raises:
        TypeError: ``given`` is neither.
    """
    from sqlalchemy import Column, Table

    if isinstance(given, Column) and isinstance(getattr(given, "table", None), Table):
        return given, None
    owner = getattr(given, "class_", None)
    mapped = getattr(getattr(given, "property", None), "columns", None) or [None]
    column = mapped[0]
    if (
        isinstance(owner, type)
        and isinstance(column, Column)
        and isinstance(getattr(column, "table", None), Table)
    ):
        return column, owner.__module__
    raise TypeError(
        "keeps_files takes a table's column, such as documents.c.file, or a "
        "mapped attribute once start_mappers() has run, not "
        f"{given!r}."
    )


def forget_files(storage: str | None = None) -> None:
    """Forget the columns declared for ``storage``, or for every storage (tests)."""
    if storage is None:
        _kept.clear()
    else:
        _kept.pop(storage, None)


async def remove_unreferenced(
    names: list[str | None], storage: str = DEFAULT_STORAGE
) -> None:
    """Delete each of ``names`` from ``storage`` that no row keeps.

    Every column ``keeps_files`` declared for ``storage`` is read on the
    primary database. A name a row holds stays; one already gone, and an
    empty one (a nullable column's ``None``), are passed over.

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
    wanted = [name for name in dict.fromkeys(names) if name]
    if not wanted:
        return
    held = await _held(wanted, columns)
    target = storages[storage]
    for name in wanted:
        if name not in held:
            await target.delete(name)


async def _held(names: Sequence[str], columns: Iterable[_Kept]) -> set[str]:
    """The ``names`` a row of ``columns`` holds, read on each primary."""
    from sqlalchemy import select

    from domainkit.conf.global_settings import DEFAULT_DB_ALIAS
    from domainkit.db.routing import router
    from domainkit.db.uow import SqlAlchemyUnitOfWork

    by_target: dict[str, list[Column[Any]]] = defaultdict(list)
    for column, module in columns:
        label = column.table.info.get("domain")
        if label is not None:
            target = router.target_for(label)
        elif module is not None:
            target = router.target_for_module(module)
        else:
            target = DEFAULT_DB_ALIAS
        by_target[target].append(column)
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

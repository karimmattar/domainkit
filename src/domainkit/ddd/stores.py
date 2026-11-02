"""Storage declared, not written: a unit of work's repositories, from its annotations.

A domain's unit of work names its repositories once, as annotations::

    class CatalogUnitOfWork(UnitOfWork, ABC):
        products: ProductRepository        # a port: Repository[Product, ProductId]
        brands: Repository[Brand, BrandId]  # or no port of its own

and that is enough for both ends:

- ``SqlAlchemyUnitOfWork`` makes, for each, a SQLAlchemy repository that
  implements the port, bound to its session; a class of its own, written
  for queries the port declares, is named by annotating it on the
  SQLAlchemy unit of work (``products: SqlAlchemyProductRepository``).
- ``in_memory(CatalogUnitOfWork)`` is the unit of work tests use: each
  repository in memory, a rollback putting them back as they were, and
  ``uow.committed`` counting its commits, as on any unit of work.

Plain Python, as the domain layer needs.
"""

import inspect
from collections.abc import Callable
from functools import cache
from types import GenericAlias, new_class
from typing import Any, cast, get_args, get_origin, get_type_hints

from domainkit.ddd.events import EventBus
from domainkit.ddd.repository import InMemoryRepository, Repository
from domainkit.ddd.uow import UnitOfWork

type Declared = type[Repository[Any, Any]] | GenericAlias
"""A repository annotation: a port class, or ``Repository[Aggregate, Id]``."""


@cache
def repositories_of(uow_class: type[UnitOfWork]) -> dict[str, Declared]:
    """The repositories ``uow_class`` annotates, by attribute name.

    The most derived annotation of a name wins, so an implementation can
    name a repository class of its own for a port's attribute.
    """
    declared: dict[str, Declared] = {}
    for name, hint in get_type_hints(uow_class).items():
        if name.startswith("_"):
            continue
        origin = get_origin(hint) or hint
        if isinstance(origin, type) and issubclass(origin, Repository):
            declared[name] = hint
    return declared


def implementation[RepositoryT: Repository[Any, Any]](
    declared: Declared, base: type[RepositoryT]
) -> type[RepositoryT]:
    """A concrete class of ``base`` (an adapter) that implements ``declared``.

    ``declared`` itself if it already is one; else a subclass of ``base``
    and the port, named after both (``SqlAlchemyProductRepository``).

    Raises:
        TypeError: ``declared`` is a parametrized port other than
            ``Repository[...]``, or leaves methods abstract that ``base``
            does not write, such as a query of the port's own.
    """
    return cast("type[RepositoryT]", _implementation(declared, base))


@cache
def _implementation(
    declared: Declared, base: type[Repository[Any, Any]]
) -> type[Repository[Any, Any]]:
    """``implementation``, made once per port and adapter."""
    if isinstance(declared, type) and issubclass(declared, base):
        return declared
    origin, arguments = get_origin(declared), get_args(declared)
    if origin is Repository:
        name = arguments[0].__name__ + "Repository"
        bases: tuple[Any, ...] = (base[arguments],)  # type: ignore[index]
    elif isinstance(declared, type) and origin is None:
        name, bases = declared.__name__, (base, declared)
    else:
        raise TypeError(
            f"{declared}: annotate a repository with its port class or with "
            "Repository[Aggregate, Id]."
        )
    prefix = base.__name__.removesuffix("Repository")
    made: type[Repository[Any, Any]] = new_class(
        f"{prefix}{name}",
        bases,
        exec_body=lambda namespace: namespace.update(__module__=base.__module__),
    )
    if inspect.isabstract(made):
        missing = ", ".join(sorted(made.__abstractmethods__))
        raise TypeError(
            f"{prefix}{name} cannot be made: {name} declares {missing}, which "
            f"{base.__name__} does not write. Write the class, and name it: "
            "annotate it on the unit of work, or pass it to in_memory()."
        )
    return made


def in_memory[UnitOfWorkT: UnitOfWork](
    uow_class: Callable[..., UnitOfWorkT],
    /,
    *,
    events: EventBus | None = None,
    **repositories: Repository[Any, Any],
) -> UnitOfWorkT:
    """A ``uow_class`` that keeps everything in memory, for tests and scripts.

    Its repositories are ``InMemoryRepository`` classes that implement the
    ports ``uow_class`` annotates, unless ``repositories`` gives some (such
    as one filled in advance, or one writing a query of the port's own).
    A rollback puts the repositories back.
    Committed events go to ``events``, a bus of its own by default.

    Raises:
        TypeError: ``uow_class`` already stores its work somewhere, or
            ``repositories`` names one it does not annotate.
    """
    if not (isinstance(uow_class, type) and issubclass(uow_class, UnitOfWork)):
        raise TypeError(f"in_memory takes a UnitOfWork class, not {uow_class!r}.")
    declared = repositories_of(uow_class)
    unknown = sorted(set(repositories) - set(declared))
    if unknown:
        raise TypeError(
            f"{uow_class.__name__} has no repository {', '.join(unknown)}; "
            f"it annotates {', '.join(declared) or 'none'}."
        )
    uow: UnitOfWorkT = _in_memory_class(uow_class)(events=events or EventBus())
    for name, port in declared.items():
        given = repositories.get(name)
        setattr(
            uow,
            name,
            given if given is not None else implementation(port, InMemoryRepository)(),
        )
    return uow


@cache
def _in_memory_class[UnitOfWorkT: UnitOfWork](
    uow_class: type[UnitOfWorkT],
) -> type[UnitOfWorkT]:
    """``uow_class`` with nothing to commit or roll back but its repositories."""
    if "_commit" not in getattr(uow_class, "__abstractmethods__", ()):
        raise TypeError(
            f"{uow_class.__name__} already stores its work: pass the domain's "
            "unit of work (the port, as CatalogUnitOfWork) to in_memory()."
        )

    async def _commit(self: Any) -> None:  # noqa: ANN401 - the class made here.
        """Nothing to write: the repositories are the state."""

    async def _rollback(self: Any) -> None:  # noqa: ANN401 - the class made here.
        """Nothing to undo: ``rollback()`` puts the repositories back."""

    return type(
        f"InMemory{uow_class.__name__}",
        (uow_class,),
        {
            "__module__": uow_class.__module__,
            "__doc__": f"{uow_class.__name__}, in memory (``in_memory``).",
            "_commit": _commit,
            "_rollback": _rollback,
        },
    )

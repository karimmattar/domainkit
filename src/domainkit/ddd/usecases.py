"""A use case written once: one async function, its command derived from it.

Without it, one use case is a ``Command`` class, a service that opens the
unit of work and commits, and a route that names both. With it, the function
is all there is::

    @use_case
    async def restock(
        uow: CatalogUnitOfWork,
        product_id: UUID,
        quantity: Annotated[int, Range(min=1)],
    ) -> Product:
        \"\"\"Add items to a product's stock.\"\"\"
        product = await uow.products.get(ProductId(product_id))
        product.restock(quantity)
        return product

- ``restock.command`` is the ``Command`` its parameters describe after the
  unit of work (``Restock(product_id=..., quantity=...)``), with their
  ``Annotated`` limits, which request schemas check; its docstring is the
  function's.
- ``await restock(uow, product_id=..., quantity=...)`` runs it: the unit of
  work is opened, the function called, and the work committed, or rolled
  back if it raises.
- ``await restock.run(command, uow)`` runs it from a command: the
  ``service(command, uow)`` shape ``ResourceRouter.command_route``, tasks,
  and tests already call. ``ResourceRouter.use_case(restock)`` declares its
  route from all this.

Plain Python, as the domain and application layers need.
"""

import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, get_type_hints, overload

from domainkit.ddd._dataclass import MessageMeta
from domainkit.ddd.messages import Command
from domainkit.ddd.uow import UnitOfWork
from domainkit.utils.text import camel_case


class UseCase[ResultT]:
    """A use case: its command, and how to run it in a unit of work."""

    def __init__(
        self,
        function: Callable[..., Awaitable[ResultT]],
        *,
        name: str | None = None,
    ) -> None:
        """Derive the command from ``function``'s parameters after the first.

        Raises:
            TypeError: ``function`` is not async, takes no unit of work first,
                or has a parameter without a type hint, or one that is
                positional-only, ``*args``, or ``**kwargs``.
        """
        if not inspect.iscoroutinefunction(function):
            raise TypeError(f"use_case needs an async function, not {function!r}.")
        parameters = list(inspect.signature(function).parameters.values())
        if not parameters:
            raise TypeError(
                f"{function.__qualname__} takes no unit of work: name one first."
            )
        hints = get_type_hints(function, include_extras=True)
        self.function = function
        self.name = name or function.__name__
        # A ``Command`` made at run time: typed ``Any`` so that its fields,
        # which static checkers cannot see, may be passed by name.
        self.command: type[Any] = _command_of(
            function, name or function.__name__, parameters[1:], hints
        )
        self.fields: tuple[str, ...] = tuple(
            field.name for field in dataclasses.fields(self.command)
        )
        self.returns_nothing: bool = hints.get("return") is type(None)
        """Whether it is declared ``-> None``: its route answers ``204``."""
        self.__doc__ = function.__doc__
        self.__name__ = self.name
        self.__qualname__ = function.__qualname__
        self.__module__ = function.__module__

    async def __call__(self, uow: UnitOfWork, /, **fields: Any) -> ResultT:  # noqa: ANN401 - the command's fields.
        """Run it with these fields, as its command would carry them."""
        return await self.run(self.command(**fields), uow)

    async def run(self, command: Command, uow: UnitOfWork) -> ResultT:
        """Open ``uow``, call the function with ``command``'s fields, and commit.

        Raises:
            TypeError: ``command`` is not this use case's command.
        """
        if not isinstance(command, self.command):
            raise TypeError(
                f"{self.name} runs {self.command.__name__}, not "
                f"{type(command).__name__}."
            )
        arguments = {name: getattr(command, name) for name in self.fields}
        async with uow:
            result = await self.function(uow, **arguments)
            await uow.commit()
        return result

    def __repr__(self) -> str:
        """Name the use case and its command."""
        return f"<use case {self.name} ({self.command.__name__})>"


def _command_of(
    function: Callable[..., object],
    name: str,
    parameters: list[inspect.Parameter],
    hints: dict[str, Any],
) -> type[Command]:
    """A ``Command`` class whose fields are ``parameters``, with their hints."""
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise TypeError(
                f"{function.__qualname__}: {parameter.name} must be an ordinary "
                "parameter, given by name."
            )
        if parameter.name not in hints:
            raise TypeError(
                f"{function.__qualname__}: give {parameter.name} a type hint; "
                "the command and its request schema are made from it."
            )
        annotations[parameter.name] = hints[parameter.name]
        if parameter.default is not inspect.Parameter.empty:
            defaults[parameter.name] = parameter.default
    namespace = {
        "__annotations__": annotations,
        "__module__": function.__module__,
        "__qualname__": camel_case(name),
        "__doc__": function.__doc__ or f"Run {name}.",
        **defaults,
    }
    return MessageMeta(camel_case(name), (Command,), namespace)  # type: ignore[return-value]


@overload
def use_case[**P, ResultT](
    function: Callable[P, Awaitable[ResultT]], /
) -> UseCase[ResultT]: ...
@overload
def use_case[**P, ResultT](
    *, name: str | None = None
) -> Callable[[Callable[P, Awaitable[ResultT]]], UseCase[ResultT]]: ...
def use_case[**P, ResultT](
    function: Callable[P, Awaitable[ResultT]] | None = None,
    /,
    *,
    name: str | None = None,
) -> UseCase[ResultT] | Callable[[Callable[P, Awaitable[ResultT]]], UseCase[ResultT]]:
    """Make ``function`` a use case: ``@use_case``, or ``@use_case(name=...)``.

    ``name`` names its command (``camel_case(name)``) and its route;
    default: the function's name.
    """
    if function is not None:
        return UseCase(function, name=name)

    def decorate(inner: Callable[P, Awaitable[ResultT]]) -> UseCase[ResultT]:
        return UseCase(inner, name=name)

    return decorate

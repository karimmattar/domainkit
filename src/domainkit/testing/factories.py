"""Factories: aggregates for tests, with defaults each test overrides.

A factory names what it makes in ``Meta`` and declares a default for each
field, as factory_boy does::

    class ProductFactory(Factory[Product]):
        class Meta:
            model = Product
            repository = "products"  # the unit of work's attribute
            unit_of_work = ShopUnitOfWork  # for create() without one

        id = LazyFunction(lambda: ProductId(uuid4()))
        name = Sequence(lambda n: f"Product {n}")
        slug = LazyAttribute(lambda product: product.name.lower())
        price = SubFactory(MoneyFactory, currency="EUR")

    product = ProductFactory.build(name="Tea")  # in memory only
    product = await ProductFactory.create(uow)  # stored through uow
    product = await ProductFactory.create(price__amount=Decimal(5))

A default is a plain value (copied for each object), a ``Sequence`` (from
the object's number: each factory counts its objects from 0), a
``LazyFunction`` (called for each object), a ``LazyAttribute`` (computed
from the other fields, in any order), or a ``SubFactory`` (another
factory's object; ``price__amount=`` overrides its fields). Keyword
arguments replace defaults, and may set fields the factory does not
declare. ``Meta.exclude`` names fields computed only for others to use
(``customer``, for ``customer_id``), which the model does not receive.

``create`` stores the object as an application service would: it enters the
unit of work, adds the object to the repository ``Meta.repository`` names,
and commits, so the events it recorded are published. Objects of
sub-factories that name a repository are added through the same unit of
work, in the same transaction; the others are only built. It works the
same with an in-memory unit of work and a SQL one.
"""

import copy
import itertools
from collections.abc import Callable, Mapping
from types import FunctionType
from typing import Any, ClassVar, Final

from domainkit.ddd import UnitOfWork

SEPARATOR: Final = "__"
"""Joins a sub-factory's field to its own: ``price__amount``."""


class Declaration:
    """A field default computed for each object a factory makes."""

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Return the value of field ``name`` of the object ``fields`` describes."""
        raise NotImplementedError


class Sequence(Declaration):
    """``Sequence(lambda n: f"user-{n}")``: the object's number, from 0.

    Each object a factory makes takes the factory's next number, which all
    its ``Sequence`` fields share.
    """

    def __init__(self, function: Callable[[int], Any]) -> None:
        """Compute each value from the object's number."""
        self.function = function

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Call the function with the object's number."""
        return self.function(fields._number)


class LazyFunction(Declaration):
    """``LazyFunction(uuid4)``: the function's result, called for each object."""

    def __init__(self, function: Callable[[], Any]) -> None:
        """Call ``function`` for each object."""
        self.function = function

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Call the function."""
        return self.function()


class LazyAttribute(Declaration):
    """``LazyAttribute(lambda o: o.name.lower())``: computed from other fields."""

    def __init__(self, function: Callable[["Fields"], Any]) -> None:
        """Compute each value from the object's other fields."""
        self.function = function

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Call the function with the object's fields."""
        return self.function(fields)


class SubFactory(Declaration):
    """``SubFactory(MoneyFactory, currency="EUR")``: another factory's object."""

    def __init__(
        self,
        factory: type["Factory[Any]"],
        **defaults: Any,  # noqa: ANN401 - the other factory's fields.
    ) -> None:
        """Make it with ``factory``; ``defaults`` replace that factory's own."""
        self.factory = factory
        self.defaults = defaults

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Build the other object, with the ``name__field`` overrides given."""
        return self.factory.build(**self.overrides(name, fields))

    def overrides(self, name: str, fields: "Fields") -> dict[str, Any]:
        """The other factory's overrides: these defaults, then those given."""
        return {**self.defaults, **fields._nested.get(name, {})}


class Fields:
    """The fields of the object being made, each computed when first read.

    ``LazyAttribute`` functions get it: ``fields.name`` is the object's
    ``name``, as given or as its default.
    """

    def __init__(
        self,
        factory: type["Factory[Any]"],
        given: Mapping[str, Any],
        nested: Mapping[str, dict[str, Any]],
    ) -> None:
        """Compute ``factory``'s fields, with ``given`` values first."""
        self._factory = factory
        self._number = next(factory._sequence)
        self._given = given
        self._nested = nested
        self._values: dict[str, Any] = {}
        self._computing: list[str] = []

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - any field.
        """Return field ``name``, computing it on first read."""
        if name not in self._values:
            self._values[name] = self._compute(name)
        return self._values[name]

    def values(self) -> dict[str, Any]:
        """Every field: the declared ones in order, then the others given."""
        names = dict.fromkeys([*self._factory._declarations, *self._given])
        return {name: getattr(self, name) for name in names}

    def _compute(self, name: str) -> Any:  # noqa: ANN401 - any field.
        """Take the value given, or evaluate the default."""
        if name in self._given:
            return self._given[name]
        factory = self._factory.__name__
        declared = self._factory._declarations
        if name not in declared:
            raise AttributeError(f"{factory} has no field {name!r}.")
        if name in self._computing:
            cycle = " -> ".join([*self._computing, name])
            raise ValueError(f"{factory}'s fields depend on each other: {cycle}.")
        self._computing.append(name)
        try:
            default = declared[name]
            if isinstance(default, Declaration):
                return default.evaluate(name, self)
            return copy.deepcopy(default)
        finally:
            self._computing.pop()


class Factory[T]:
    """Make ``T`` objects for tests: ``build`` keeps them in memory, ``create`` stores.

    Subclasses set ``Meta.model`` (a class, or any callable returning a
    ``T``), and to ``create``, ``Meta.repository`` and, optionally,
    ``Meta.unit_of_work``. Public class attributes are the fields'
    defaults; methods and classes are not.
    """

    _declarations: ClassVar[dict[str, Any]] = {}
    _sequence: ClassVar["itertools.count[int]"] = itertools.count()

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401 - other bases'.
        """Collect the fields' defaults, a subclass's after its bases'."""
        super().__init_subclass__(**kwargs)
        cls._sequence = itertools.count()
        cls._declarations = {
            name: value
            for klass in reversed(cls.__mro__)
            for name, value in vars(klass).items()
            if not name.startswith("_") and not _is_behaviour(value)
        }

    @classmethod
    def build(cls, **overrides: Any) -> T:  # noqa: ANN401 - the model's fields.
        """Return a new object, in memory only."""
        return cls._make(cls._fields(overrides))

    @classmethod
    def build_batch(cls, size: int, /, **overrides: Any) -> list[T]:  # noqa: ANN401 - the model's fields.
        """Return ``size`` new objects, in memory only."""
        return [cls.build(**overrides) for _ in range(size)]

    @classmethod
    async def create(cls, uow: UnitOfWork | None = None, /, **overrides: Any) -> T:  # noqa: ANN401 - the model's fields.
        """Store a new object through ``uow`` (default: ``Meta.unit_of_work()``).

        Enter the unit of work, add the object, and the objects of
        sub-factories that name a repository, and commit.
        """
        uow = uow if uow is not None else cls._unit_of_work()
        async with uow:
            made = await cls._add(uow, cls._fields(overrides))
            await uow.commit()
        return made

    @classmethod
    async def create_batch(
        cls,
        size: int,
        uow: UnitOfWork | None = None,
        /,
        **overrides: Any,  # noqa: ANN401 - the model's fields.
    ) -> list[T]:
        """Store ``size`` new objects, each committed on its own."""
        return [await cls.create(uow, **overrides) for _ in range(size)]

    @classmethod
    def reset_sequence(cls, value: int = 0) -> None:
        """Number the next object ``value``."""
        cls._sequence = itertools.count(value)

    @classmethod
    def _fields(cls, overrides: Mapping[str, Any]) -> Fields:
        """The new object's fields, ``overrides`` (``name__field`` too) first."""
        given: dict[str, Any] = {}
        nested: dict[str, dict[str, Any]] = {}
        for key, value in overrides.items():
            name, separator, field = key.partition(SEPARATOR)
            if not separator:
                given[name] = value
            elif not isinstance(cls._declarations.get(name), SubFactory):
                raise TypeError(f"{cls.__name__}.{name} is not a SubFactory: {key}=")
            elif name in overrides:
                raise TypeError(f"Give {name}= or {name}{SEPARATOR}...=, not both.")
            else:
                nested.setdefault(name, {})[field] = value
        return Fields(cls, given, nested)

    @classmethod
    async def _add(cls, uow: UnitOfWork, fields: Fields) -> T:
        """Add the new object, after the sub-factories' that have a repository."""
        for name, default in cls._declarations.items():
            if (
                isinstance(default, SubFactory)
                and name not in fields._given
                and default.factory._option("repository") is not None
            ):
                overrides = default.overrides(name, fields)
                sub = default.factory._fields(overrides)
                fields._values[name] = await default.factory._add(uow, sub)
        made = cls._make(fields)
        await cls._repository(uow).add(made)
        return made

    @classmethod
    def _make(cls, fields: Fields) -> T:
        """Call the model with every field it takes."""
        model = cls._option("model")
        if model is None:
            raise TypeError(f"{cls.__name__} needs Meta.model.")
        excluded = cls._option("exclude") or ()
        values = {k: v for k, v in fields.values().items() if k not in excluded}
        made: T = model(**values)
        return made

    @classmethod
    def _repository(cls, uow: UnitOfWork) -> Any:  # noqa: ANN401 - a repository.
        """The repository of ``uow`` that ``Meta.repository`` names."""
        name = cls._option("repository")
        if name is None:
            raise TypeError(f"{cls.__name__} needs Meta.repository to create.")
        repository = getattr(uow, name, None)
        if repository is None:
            raise TypeError(f"{type(uow).__name__} has no repository {name!r}.")
        return repository

    @classmethod
    def _unit_of_work(cls) -> UnitOfWork:
        """A new ``Meta.unit_of_work()``, for ``create`` without a unit of work."""
        make = cls._option("unit_of_work")
        if make is None:
            raise TypeError(
                f"Give {cls.__name__}.create() a unit of work, or set "
                f"Meta.unit_of_work."
            )
        uow: UnitOfWork = make()
        return uow

    @classmethod
    def _option(cls, name: str) -> Any:  # noqa: ANN401 - any Meta option.
        """``Meta.<name>`` of the nearest class that sets it, or None."""
        for klass in cls.__mro__:
            meta = vars(klass).get("Meta")
            if meta is not None and hasattr(meta, name):
                return getattr(meta, name)
        return None


def _is_behaviour(value: object) -> bool:
    """Whether a class attribute is a method or a class, not a field's default."""
    return isinstance(value, (FunctionType, classmethod, staticmethod, property, type))

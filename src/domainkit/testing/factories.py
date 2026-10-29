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

As in factory_boy, a field can also copy another (``SelfAttribute``, with
``..`` for the factory that made this one through a ``SubFactory``), or
take one of two defaults (``Maybe``). ``class Params`` declares fields the
model does not receive, and ``Trait``s among them: ``shipped=True`` turns a
set of defaults on. After the object is made, ``post_generation`` methods
run, and ``RelatedFactory`` makes objects that point back at it; with
``create`` both run before the commit, through the same unit of work.
``stub()`` gives the fields as a plain namespace, with no model called.
"""

import copy
import itertools
from collections.abc import Callable, Mapping
from types import FunctionType, SimpleNamespace
from typing import Any, ClassVar, Final

from domainkit.ddd import UnitOfWork

SEPARATOR: Final = "__"
"""Joins a sub-factory's field to its own: ``price__amount``."""
PARAMS: Final = "Params"
"""The inner class whose attributes are parameters and traits, not the model's."""
_MISSING: Final = object()


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
        """Build (or stub) the other object, with the ``name__field`` overrides."""
        made = self.factory._fields(self.overrides(name, fields), parent=fields)
        if fields._stub:
            return self.factory._stubbed(made)
        return self.factory._built(made)

    def overrides(self, name: str, fields: "Fields") -> dict[str, Any]:
        """The other factory's overrides: these defaults, then those given."""
        return {**self.defaults, **fields._nested.get(name, {})}


class SelfAttribute(Declaration):
    """``SelfAttribute("customer.id")``: another field's value, or its attribute's.

    Each leading dot past the first climbs to the factory whose
    ``SubFactory`` made this object: ``SelfAttribute("..currency")`` is the
    parent's ``currency``. ``default`` is taken when the path is missing.
    """

    def __init__(self, path: str, default: Any = _MISSING) -> None:  # noqa: ANN401 - any field.
        """Read ``path``: dotted names, after the dots that climb."""
        self.path = path
        self.default = default

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Follow the path from the object, or from the factory that made it."""
        names = self.path.lstrip(".")
        target: Any = fields
        for _ in range(len(self.path) - len(names) - 1):
            target = target._parent
            if target is None:
                raise ValueError(
                    f"{fields._factory.__name__}.{name}: {self.path!r} climbs "
                    f"past the first factory."
                )
        try:
            for part in names.split("."):
                target = getattr(target, part)
        except AttributeError:
            if self.default is _MISSING:
                raise
            return self.default
        return target


class Maybe(Declaration):
    """``Maybe("paid", yes, no)``: ``yes`` when the field ``paid`` is true, else ``no``.

    ``decider`` is a field's name, or a declaration computed for the object;
    ``yes`` and ``no`` are defaults of any kind, a ``SubFactory`` too.
    """

    def __init__(
        self,
        decider: "str | Declaration",
        yes: Any,  # noqa: ANN401 - any default.
        no: Any = None,  # noqa: ANN401 - any default.
    ) -> None:
        """Choose between ``yes`` and ``no`` by ``decider``."""
        self.decider = decider
        self.yes = yes
        self.no = no

    def chosen(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any default.
        """The default the decider picks for this object."""
        if isinstance(self.decider, Declaration):
            decided = self.decider.evaluate(name, fields)
        else:
            decided = getattr(fields, self.decider)
        return self.yes if decided else self.no

    def evaluate(self, name: str, fields: "Fields") -> Any:  # noqa: ANN401 - any field.
        """Compute the chosen default."""
        return _value(self.chosen(name, fields), name, fields)


class Trait:
    """``shipped = Trait(status="shipped")`` in ``class Params``: defaults to turn on.

    ``build(shipped=True)`` applies them, in the order the traits are
    declared; arguments given beside it still win. ``name__field`` keys
    reach a sub-factory, as given arguments do.
    """

    def __init__(self, **overrides: Any) -> None:  # noqa: ANN401 - the model's fields.
        """Keep the defaults the trait turns on."""
        self.overrides = overrides


class PostDeclaration:
    """Work run on the object once it is made: ``post_generation``, ``RelatedFactory``.

    It receives what was given under its name (``extracted``), and the
    ``name__key`` arguments as keywords; the model receives neither.
    """

    def build(self, made: Any, extracted: Any, options: dict[str, Any]) -> None:  # noqa: ANN401 - any object.
        """Run on an object ``build`` made."""
        raise NotImplementedError

    async def create(
        self,
        made: Any,  # noqa: ANN401 - any object.
        extracted: Any,  # noqa: ANN401 - what was given.
        options: dict[str, Any],
        uow: UnitOfWork,
    ) -> None:
        """Run on an object ``create`` added to ``uow``, before the commit."""
        raise NotImplementedError


class PostGeneration(PostDeclaration):
    """A ``post_generation`` method: ``function(made, create, extracted, **kw)``."""

    def __init__(self, function: Callable[..., object]) -> None:
        """Call ``function`` once the object is made."""
        self.function = function

    def build(self, made: Any, extracted: Any, options: dict[str, Any]) -> None:  # noqa: ANN401 - any object.
        """Call the function with ``create=False``."""
        self.function(made, False, extracted, **options)

    async def create(
        self,
        made: Any,  # noqa: ANN401 - any object.
        extracted: Any,  # noqa: ANN401 - what was given.
        options: dict[str, Any],
        uow: UnitOfWork,
    ) -> None:
        """Call the function with ``create=True``; the commit follows."""
        self.function(made, True, extracted, **options)


def post_generation(function: Callable[..., object]) -> PostGeneration:
    """Run the decorated method on each object made: ``(made, create, extracted)``.

    ``create`` is whether ``create`` made it (it is in the unit of work, not
    yet committed), ``extracted`` what was given under the method's name
    (None if nothing), and ``options`` the ``name__key`` arguments. What it
    changes on the object is committed with it.
    """
    return PostGeneration(function)


class RelatedFactory(PostDeclaration):
    """``RelatedFactory(LineFactory, "invoice")``: an object that points at this one.

    Made after this one, with this one as its field ``related_name`` (none
    when empty); ``create`` adds it through the same unit of work, in the
    same transaction, when its factory names a repository. ``defaults``,
    then the ``name__field`` arguments, are its overrides; giving a value
    under its name makes none.
    """

    def __init__(
        self,
        factory: type["Factory[Any]"],
        related_name: str = "",
        **defaults: Any,  # noqa: ANN401 - the other factory's fields.
    ) -> None:
        """Make it with ``factory``, ``related_name`` set to this object."""
        self.factory = factory
        self.related_name = related_name
        self.defaults = defaults

    def build(self, made: Any, extracted: Any, options: dict[str, Any]) -> None:  # noqa: ANN401 - any object.
        """Build the other object, unless a value was given."""
        if extracted is None:
            self.factory._built(self.factory._fields(self._overrides(made, options)))

    async def create(
        self,
        made: Any,  # noqa: ANN401 - any object.
        extracted: Any,  # noqa: ANN401 - what was given.
        options: dict[str, Any],
        uow: UnitOfWork,
    ) -> None:
        """Add the other object through ``uow`` if it is stored, else build it."""
        if self.factory._option("repository") is None:
            self.build(made, extracted, options)
        elif extracted is None:
            fields = self.factory._fields(self._overrides(made, options))
            await self.factory._add(uow, fields)

    def _overrides(self, made: Any, options: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401 - any object.
        """The other factory's overrides, this object under ``related_name``."""
        pointer = {self.related_name: made} if self.related_name else {}
        return {**self.defaults, **pointer, **options}


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
        *,
        parent: "Fields | None" = None,
        stub: bool = False,
    ) -> None:
        """Compute ``factory``'s fields, with ``given`` values first.

        ``parent`` is the object whose ``SubFactory`` made this one; a stub's
        sub-factories make stubs.
        """
        self._factory = factory
        self._number = next(factory._sequence)
        self._given = given
        self._nested = nested
        self._parent = parent
        self._stub: bool = stub or (parent is not None and parent._stub)
        self._values: dict[str, Any] = {}
        self._computing: list[str] = []

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - any field.
        """Return field ``name``, computing it on first read."""
        if name not in self._values:
            self._values[name] = self._compute(name)
        return self._values[name]

    def values(self) -> dict[str, Any]:
        """The model's fields: the declared ones in order, then the others given.

        Parameters (``Params``), ``Meta.exclude`` and post declarations are
        left out; parameters are computed all the same, as fields read them.
        """
        factory = self._factory
        left_out = {*(factory._option("exclude") or ()), *factory._post}
        names = dict.fromkeys([*factory._declarations, *self._given])
        values = {name: getattr(self, name) for name in names if name not in left_out}
        return {
            name: value for name, value in values.items() if name not in factory._params
        }

    def _compute(self, name: str) -> Any:  # noqa: ANN401 - any field.
        """Take the value given, or evaluate the default.

        A declaration given (``SubFactory(..., currency=SelfAttribute(...))``)
        is evaluated, as a default would be.
        """
        if name in self._given:
            given = self._given[name]
            if isinstance(given, Declaration):
                return given.evaluate(name, self)
            return given
        factory = self._factory.__name__
        declared = self._factory._declarations
        if name not in declared:
            raise AttributeError(f"{factory} has no field {name!r}.")
        if name in self._computing:
            cycle = " -> ".join([*self._computing, name])
            raise ValueError(f"{factory}'s fields depend on each other: {cycle}.")
        self._computing.append(name)
        try:
            return _value(declared[name], name, self)
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
    _params: ClassVar[frozenset[str]] = frozenset()
    _traits: ClassVar[dict[str, Trait]] = {}
    _post: ClassVar[dict[str, PostDeclaration]] = {}
    _sequence: ClassVar["itertools.count[int]"] = itertools.count()

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401 - other bases'.
        """Collect the fields' defaults, a subclass's after its bases'.

        ``Params``' attributes join them as parameters, which the model does
        not receive; a ``Trait`` is a parameter that defaults to False. Post
        declarations are kept apart, in the order they are declared.
        """
        super().__init_subclass__(**kwargs)
        cls._sequence = itertools.count()
        declared: dict[str, Any] = {}
        params: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            declared.update(_public(vars(klass)))
            params.update(_public(vars(vars(klass).get(PARAMS, object))))
        cls._traits = {
            name: trait for name, trait in params.items() if isinstance(trait, Trait)
        }
        cls._post = {
            name: value
            for name, value in declared.items()
            if isinstance(value, PostDeclaration)
        }
        cls._params = frozenset(params)
        cls._declarations = {
            **{
                name: value for name, value in declared.items() if name not in cls._post
            },
            **{
                name: False if name in cls._traits else value
                for name, value in params.items()
            },
        }

    @classmethod
    def build(cls, **overrides: Any) -> T:  # noqa: ANN401 - the model's fields.
        """Return a new object, in memory only; post declarations run on it."""
        return cls._built(cls._fields(overrides))

    @classmethod
    def build_batch(cls, size: int, /, **overrides: Any) -> list[T]:  # noqa: ANN401 - the model's fields.
        """Return ``size`` new objects, in memory only."""
        return [cls.build(**overrides) for _ in range(size)]

    @classmethod
    def stub(cls, **overrides: Any) -> SimpleNamespace:  # noqa: ANN401 - the model's fields.
        """Return the fields the model would receive, as attributes; no model is called.

        Sub-factories give stubs too, and post declarations do not run: a
        stub is data, for code that reads fields only.
        """
        return cls._stubbed(cls._fields(overrides, stub=True))

    @classmethod
    def stub_batch(cls, size: int, /, **overrides: Any) -> list[SimpleNamespace]:  # noqa: ANN401 - the model's fields.
        """Return ``size`` stubs."""
        return [cls.stub(**overrides) for _ in range(size)]

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
    def _fields(
        cls,
        overrides: Mapping[str, Any],
        *,
        parent: Fields | None = None,
        stub: bool = False,
    ) -> Fields:
        """The new object's fields: ``overrides`` (``name__field`` too) first.

        Then the defaults of the traits turned on, then the declared ones.
        """
        given: dict[str, Any] = {}
        nested: dict[str, dict[str, Any]] = {}
        for key, value in cls._with_traits(overrides).items():
            name, separator, field = key.partition(SEPARATOR)
            if not separator:
                given[name] = value
            elif not cls._takes_nested(name):
                raise TypeError(
                    f"{cls.__name__}.{name} is not a SubFactory, a "
                    f"RelatedFactory, or a post_generation: {key}="
                )
            elif name not in cls._post and (name in given or name in overrides):
                raise TypeError(f"Give {name}= or {name}{SEPARATOR}...=, not both.")
            else:
                nested.setdefault(name, {})[field] = value
        return Fields(cls, given, nested, parent=parent, stub=stub)

    @classmethod
    def _with_traits(cls, overrides: Mapping[str, Any]) -> dict[str, Any]:
        """``overrides`` over the defaults of the traits they turn on.

        A trait's default yields to any given argument for the same field,
        its ``name__field`` keys too.
        """
        given = {key.partition(SEPARATOR)[0] for key in overrides}
        turned_on: dict[str, Any] = {}
        for name, trait in cls._traits.items():
            if overrides.get(name):
                turned_on.update(trait.overrides)
        kept = {
            key: value
            for key, value in turned_on.items()
            if key.partition(SEPARATOR)[0] not in given
        }
        return {**kept, **overrides}

    @classmethod
    def _takes_nested(cls, name: str) -> bool:
        """Whether ``name__key=`` arguments reach the declaration ``name``."""
        declared = cls._declarations.get(name)
        if isinstance(declared, Maybe):
            return any(isinstance(d, SubFactory) for d in (declared.yes, declared.no))
        return isinstance(declared, SubFactory) or name in cls._post

    @classmethod
    def _built(cls, fields: Fields) -> T:
        """Make the object and run its post declarations, as ``build`` does."""
        made = cls._make(fields)
        for name, post in cls._post.items():
            post.build(made, fields._given.get(name), fields._nested.get(name, {}))
        return made

    @classmethod
    def _stubbed(cls, fields: Fields) -> SimpleNamespace:
        """The fields the model would receive, as a namespace."""
        return SimpleNamespace(**fields.values())

    @classmethod
    async def _add(cls, uow: UnitOfWork, fields: Fields) -> T:
        """Add the new object, after the sub-factories' that have a repository.

        Its post declarations run after it is added, through ``uow``.
        """
        for name, declared in cls._declarations.items():
            default = declared
            if isinstance(declared, Maybe) and name not in fields._given:
                default = declared.chosen(name, fields)
            if (
                isinstance(default, SubFactory)
                and name not in fields._given
                and default.factory._option("repository") is not None
            ):
                overrides = default.overrides(name, fields)
                sub = default.factory._fields(overrides, parent=fields)
                fields._values[name] = await default.factory._add(uow, sub)
        made = cls._make(fields)
        await cls._repository(uow).add(made)
        for name, post in cls._post.items():
            extracted = fields._given.get(name)
            await post.create(made, extracted, fields._nested.get(name, {}), uow)
        return made

    @classmethod
    def _make(cls, fields: Fields) -> T:
        """Call the model with every field it takes."""
        model = cls._option("model")
        if model is None:
            raise TypeError(f"{cls.__name__} needs Meta.model.")
        made: T = model(**fields.values())
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


def _public(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """A class body's defaults: its public attributes that are not behaviour."""
    return {
        name: value
        for name, value in attributes.items()
        if not name.startswith("_") and not _is_behaviour(value)
    }


def _value(default: Any, name: str, fields: Fields) -> Any:  # noqa: ANN401 - any field.
    """A default's value for this object: computed, or a copy of a plain one."""
    if isinstance(default, Declaration):
        return default.evaluate(name, fields)
    return copy.deepcopy(default)

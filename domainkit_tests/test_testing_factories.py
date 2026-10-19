"""``domainkit.testing`` factories: aggregates built in memory, or stored."""

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.ddd import (
    AggregateRoot,
    DomainEvent,
    EventBus,
    InMemoryRepository,
    UnitOfWork,
    ValueObject,
)
from domainkit.testing import (
    Factory,
    LazyAttribute,
    LazyFunction,
    Sequence,
    SubFactory,
)
from domainkit.testing.factories import Fields
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork
from domainkit_tests.test_db import test_metadata as notes_metadata


class Money(ValueObject):
    """An amount in a currency."""

    amount: Decimal
    currency: str


class Category(AggregateRoot[UUID]):
    """Where products are listed."""

    name: str


class Product(AggregateRoot[UUID]):
    """Something for sale, in a category it refers to by id."""

    name: str
    slug: str
    price: Money
    category_id: UUID
    tags: list[str]


class ProductListed(DomainEvent):
    """A product was listed."""

    product_id: UUID


def list_product(**fields: Any) -> Product:  # noqa: ANN401 - the product's fields.
    """Make a product the way the domain does: recording an event."""
    product = Product(**fields)
    product.record(ProductListed(product_id=product.id))
    return product


class ShopUnitOfWork(UnitOfWork):
    """In-memory unit of work that counts its commits."""

    def __init__(self, events: EventBus | None = None) -> None:
        """Start empty, publishing to a private bus."""
        super().__init__(events=events or EventBus())
        self.products = InMemoryRepository[Product, UUID]()
        self.categories = InMemoryRepository[Category, UUID]()
        self.commits = 0

    async def _commit(self) -> None:
        """Count the commit."""
        self.commits += 1

    async def _rollback(self) -> None:
        """Nothing to undo."""


class MoneyFactory(Factory[Money]):
    """Prices: a value object, so never stored on its own."""

    class Meta:
        """Build only."""

        model = Money

    amount = Decimal("9.99")
    currency = "EUR"


class CategoryFactory(Factory[Category]):
    """Categories, stored in the shop's ``categories``."""

    class Meta:
        """Stored."""

        model = Category
        repository = "categories"

    id = LazyFunction(uuid4)
    name = Sequence(lambda n: f"Category {n}")


class ProductFactory(Factory[Product]):
    """Products: every kind of default."""

    class Meta:
        """Made by the domain's function; stored in ``products``."""

        model = list_product
        repository = "products"
        unit_of_work = ShopUnitOfWork
        exclude = ("category",)

    id = LazyFunction(uuid4)
    name = Sequence(lambda n: f"Product {n}")
    slug = LazyAttribute(lambda product: product.name.lower().replace(" ", "-"))
    price = SubFactory(MoneyFactory, currency="USD")
    category = SubFactory(CategoryFactory)
    category_id = LazyAttribute(lambda product: product.category.id)
    tags: ClassVar[list[str]] = ["new"]

    @classmethod
    def cheap(cls) -> Product:
        """Methods are behaviour, not fields."""
        return cls.build(price__amount=Decimal(1))

    @staticmethod
    def helper() -> None:
        """Neither are static methods."""

    @property
    def nothing(self) -> None:
        """Nor properties."""


@pytest.fixture(autouse=True)
def numbered_from_zero() -> None:
    """Each test numbers products and categories from 0."""
    ProductFactory.reset_sequence()
    CategoryFactory.reset_sequence()


def test_build_fills_every_field() -> None:
    """Sequences count, lazy fields follow others, sub-factories build."""
    first, second = ProductFactory.build_batch(2)

    assert (first.name, first.slug) == ("Product 0", "product-0")
    assert second.name == "Product 1"
    assert first.price == Money(amount=Decimal("9.99"), currency="USD")
    assert first.id != second.id
    assert first.category_id != second.category_id
    assert first.tags == ["new"]
    first.tags.append("sale")
    assert second.tags == ["new"]  # Each object gets a copy of a plain default.
    assert ProductFactory.tags == ["new"]
    assert [type(event) for event in first.pending_events] == [ProductListed]


def test_overrides_replace_defaults_and_reach_sub_factories() -> None:
    """Given fields win; ``name__field`` reaches the sub-factory."""
    category = CategoryFactory.build(name="Tea")
    product = ProductFactory.build(
        name="Green Tea", price__amount=Decimal(3), category=category, version=4
    )

    assert (product.name, product.slug) == ("Green Tea", "green-tea")
    assert product.price == Money(amount=Decimal(3), currency="USD")
    assert product.category_id == category.id
    assert product.version == 4  # Fields the factory does not declare pass through.
    assert ProductFactory.build(
        price=Money(amount=Decimal(2), currency="GBP")
    ).price == (Money(amount=Decimal(2), currency="GBP"))
    assert ProductFactory.cheap().price.amount == Decimal(1)
    assert ProductFactory.build(price__currency="CHF").price.amount == Decimal("9.99")


def test_override_mistakes_are_refused() -> None:
    """A ``__`` override needs a sub-factory, and cannot meet its field's value."""
    with pytest.raises(TypeError, match=r"ProductFactory.name is not a SubFactory"):
        ProductFactory.build(name__first="x")
    with pytest.raises(TypeError, match=r"Give price= or price__...=, not both"):
        ProductFactory.build(price=None, price__amount=Decimal(1))


def test_sequences_belong_to_each_factory() -> None:
    """Subclasses count on their own; ``reset_sequence`` sets the next number."""

    class OnSaleFactory(ProductFactory):
        tags: ClassVar[list[str]] = ["sale"]

    ProductFactory.reset_sequence(10)
    assert ProductFactory.build().name == "Product 10"
    assert OnSaleFactory.build().name == "Product 0"
    on_sale = OnSaleFactory.build()
    assert (on_sale.name, on_sale.tags) == ("Product 1", ["sale"])
    assert ProductFactory.build().name == "Product 11"


@pytest.mark.anyio
async def test_meta_options_are_inherited_and_overridden() -> None:
    """A subclass's ``Meta`` replaces only the options it sets."""

    class Named(Factory[Category]):
        class Meta:
            model = Category

        name = "Named"

    class Identified(Named):
        class Meta:
            repository = "categories"

        id = LazyFunction(uuid4)

    uow = ShopUnitOfWork()
    category = await Identified.create(uow)
    assert uow.categories.items == {category.id: category}
    assert category.name == "Named"
    with pytest.raises(TypeError, match=r"Named needs Meta\.repository"):
        await Named.create(uow, id=uuid4())


def test_fields_are_computed_once_and_cycles_are_refused() -> None:
    """A field read twice is computed once; fields defined by each other fail."""
    calls: list[str] = []

    def named(fields: Fields) -> str:
        calls.append("name")
        return "x"

    class Counted(Factory[Category]):
        class Meta:
            model = Category
            exclude = ("upper",)

        id = LazyFunction(uuid4)
        name = LazyAttribute(named)
        upper = LazyAttribute(lambda fields: fields.name.upper())

        class Shelf:
            """Classes in the body are not fields either."""

    assert Counted.build().name == "x"
    assert calls == ["name"]

    class Circular(Factory[Category]):
        class Meta:
            model = Category

        id = LazyAttribute(lambda fields: fields.name)
        name = LazyAttribute(lambda fields: fields.id)

    with pytest.raises(ValueError, match="id -> name -> id"):
        Circular.build()

    class Missing(Factory[Category]):
        class Meta:
            model = Category

        name = LazyAttribute(lambda fields: fields.title)

    with pytest.raises(AttributeError, match="Missing has no field 'title'"):
        Missing.build()
    with pytest.raises(AttributeError):
        Fields(Missing, {}, {})._private  # noqa: B018 - reading it raises.


def test_a_factory_needs_a_model() -> None:
    """Without ``Meta.model`` there is nothing to call."""

    class Nameless(Factory[Category]):
        name = "x"

    with pytest.raises(TypeError, match=r"Nameless needs Meta\.model"):
        Nameless.build()


@pytest.mark.anyio
async def test_create_stores_through_the_unit_of_work() -> None:
    """The product and its category are added, committed once, and announced."""
    published: list[DomainEvent] = []
    bus = EventBus()

    async def collect(event: ProductListed) -> None:
        published.append(event)

    bus.subscribe(ProductListed, collect)
    uow = ShopUnitOfWork(bus)

    product = await ProductFactory.create(uow, name="Tea", category__name="Drinks")

    assert uow.commits == 1
    assert uow.products.items == {product.id: product}
    (category,) = uow.categories.items.values()
    assert (category.id, category.name) == (product.category_id, "Drinks")
    assert [event.product_id for event in published] == [product.id]  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_given_sub_objects_are_not_stored_again() -> None:
    """A category passed in is used as it is; batches commit one by one."""
    uow = ShopUnitOfWork()
    category = await CategoryFactory.create(uow)

    products = await ProductFactory.create_batch(2, uow, category=category)

    assert uow.commits == 3
    assert list(uow.categories.items.values()) == [category]
    assert {product.category_id for product in products} == {category.id}
    assert [product.name for product in products] == ["Product 0", "Product 1"]


@pytest.mark.anyio
async def test_create_makes_its_own_unit_of_work() -> None:
    """Without one, ``Meta.unit_of_work`` makes one."""
    made: list[ShopUnitOfWork] = []

    class Recorded(ShopUnitOfWork):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

    class Stored(ProductFactory):
        class Meta:
            unit_of_work = Recorded

    product = await Stored.create()

    assert [uow.products.items for uow in made] == [{product.id: product}]


@pytest.mark.anyio
async def test_create_needs_a_unit_of_work_and_a_repository() -> None:
    """Each missing piece is named."""
    with pytest.raises(TypeError, match=r"Give MoneyFactory.create\(\) a unit of work"):
        await MoneyFactory.create()
    with pytest.raises(TypeError, match=r"MoneyFactory needs Meta\.repository"):
        await MoneyFactory.create(ShopUnitOfWork())

    class Elsewhere(CategoryFactory):
        class Meta:
            repository = "shelves"

    with pytest.raises(TypeError, match="ShopUnitOfWork has no repository 'shelves'"):
        await Elsewhere.create(ShopUnitOfWork())


class NoteFactory(Factory[Note]):
    """Notes, stored in SQL."""

    class Meta:
        """Through the SQL unit of work."""

        model = Note
        repository = "notes"
        unit_of_work = NotesUnitOfWork

    id = LazyFunction(lambda: NoteId(uuid4()))
    text = Sequence(lambda n: f"note {n}")


@pytest.fixture
async def notes_database(database_url: str) -> AsyncIterator[None]:
    """An empty database with the notes table, in the running loop."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(notes_metadata.create_all)
        yield
        await connections.close_all()


@pytest.mark.anyio
async def test_create_stores_in_sql(notes_database: None) -> None:
    """The same factory stores through a SQL unit of work, and reads back."""
    NoteFactory.reset_sequence()
    note = await NoteFactory.create(text="first")
    others = await NoteFactory.create_batch(2, NotesUnitOfWork())

    async with NotesUnitOfWork() as uow:
        stored = [await uow.notes.get(item.id) for item in (note, *others)]
    assert [item.text for item in stored] == ["first", "note 1", "note 2"]
    assert [item.version for item in stored] == [1, 1, 1]


def test_plain_defaults_are_copied_all_the_way_down() -> None:
    """Nested lists and dicts are never shared between objects."""

    class Options(Factory[dict[str, Any]]):
        class Meta:
            model = dict

        sizes: ClassVar[dict[str, list[str]]] = {"shirt": ["S", "M"]}

    first, second = Options.build_batch(2)
    first["sizes"]["shirt"].append("L")

    assert second["sizes"] == {"shirt": ["S", "M"]}
    assert Options.sizes == {"shirt": ["S", "M"]}

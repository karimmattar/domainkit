"""``domainkit.testing`` factories: aggregates built in memory, or stored."""

from collections.abc import AsyncIterator
from dataclasses import field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
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
    Maybe,
    RelatedFactory,
    SelfAttribute,
    Sequence,
    SubFactory,
    Trait,
    post_generation,
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


# Traits, post-generation, related factories, SelfAttribute, Maybe, stub ----------


class OrderNoted(DomainEvent):
    """Someone wrote on an order."""

    order_id: UUID
    text: str


class Order(AggregateRoot[UUID]):
    """An order: lines refer to it by id, as aggregates refer to each other."""

    number: str
    status: str
    paid_at: datetime | None
    receipt: str
    total: Money
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        """Write on the order, recording it."""
        self.notes.append(text)
        self.record(OrderNoted(order_id=self.id, text=text))


class OrderLine(AggregateRoot[UUID]):
    """One thing ordered, in the order's currency."""

    order_id: UUID
    sku: str
    price: Money


class OrdersUnitOfWork(ShopUnitOfWork):
    """The shop's unit of work, with orders and their lines."""

    def __init__(self, events: EventBus | None = None) -> None:
        """Start empty."""
        super().__init__(events)
        self.orders = InMemoryRepository[Order, UUID]()
        self.lines = InMemoryRepository[OrderLine, UUID]()


BUILT_LINES: list[OrderLine] = []


class LineFactory(Factory[OrderLine]):
    """Lines, each pointing at the order ``RelatedFactory`` gives it."""

    class Meta:
        """Stored in ``lines``; ``order`` only gives the id and currency."""

        model = OrderLine
        repository = "lines"
        exclude = ("order",)

    class Params:
        """The order's currency, not a field of the line."""

        currency = SelfAttribute("order.total.currency", default="EUR")

    id = LazyFunction(uuid4)
    order = None
    order_id = LazyAttribute(lambda line: line.order.id)
    sku = Sequence(lambda n: f"SKU-{n}")
    price = SubFactory(MoneyFactory, currency=SelfAttribute("..currency"))

    @post_generation
    def built(line: OrderLine, create: bool, extracted: object) -> None:
        """Keep the lines that were only built."""
        if not create:
            BUILT_LINES.append(line)


class OrderFactory(Factory[Order]):
    """Orders, open unless a trait says otherwise."""

    class Meta:
        """Stored in ``orders``."""

        model = Order
        repository = "orders"
        unit_of_work = OrdersUnitOfWork

    class Params:
        """What the order's defaults follow, not its fields."""

        currency = "EUR"
        paid = Trait(status="paid", paid_at=datetime(2026, 1, 1, tzinfo=UTC))
        free = Trait(status="free", total__amount=Decimal(0))

    id = LazyFunction(uuid4)
    number = Sequence(lambda n: f"ORD-{n}")
    status = "open"
    paid_at = None
    receipt = Maybe("paid_at", "sent", no="none")
    total = SubFactory(MoneyFactory, currency=SelfAttribute("..currency"))
    line = RelatedFactory(LineFactory, "order", sku="FIRST")

    @post_generation
    def noted(
        order: Order, create: bool, extracted: list[str] | None, **options: object
    ) -> None:
        """Write the notes given, louder when asked."""
        for text in extracted or ():
            order.note(text.upper() if options.get("loud") else text)


@pytest.fixture(autouse=True)
def no_lines_built() -> None:
    """Each test sees only the lines it built, and numbers orders from 0."""
    BUILT_LINES.clear()
    OrderFactory.reset_sequence()


def test_traits_turn_defaults_on_and_given_arguments_still_win() -> None:
    """``paid=True`` sets the status and the time; ``status=`` beside it wins."""
    assert OrderFactory.build().status == "open"
    paid = OrderFactory.build(paid=True)
    assert (paid.status, paid.paid_at) == ("paid", datetime(2026, 1, 1, tzinfo=UTC))
    refunded = OrderFactory.build(paid=True, status="refunded")
    assert (refunded.status, refunded.paid_at) == ("refunded", paid.paid_at)
    both = OrderFactory.build(paid=True, free=True)
    assert both.status == "free"  # The trait declared later wins.
    assert both.total.amount == Decimal(0)
    priced = OrderFactory.build(free=True, total__amount=Decimal(7))
    assert (priced.status, priced.total.amount) == ("free", Decimal(7))
    whole = Money(amount=Decimal(3), currency="GBP")
    assert OrderFactory.build(free=True, total=whole).total == whole
    merged = OrderFactory.build(free=True, total__currency="GBP").total
    assert merged == Money(amount=Decimal(0), currency="GBP")  # Both keys hold.


def test_traits_declared_in_a_test_default_to_off() -> None:
    """A trait is a parameter that is False until given; two may not collide."""

    class Labelled(Factory[dict[str, Any]]):
        class Meta:
            model = dict

        class Params:
            loud = Trait(label="LOUD")
            priced = Trait(price=Money(amount=Decimal(1), currency="EUR"))
            cheap = Trait(price__amount=Decimal(0))

        label = LazyAttribute(lambda fields: f"loud={fields.loud}")
        price = SubFactory(MoneyFactory)

    assert Labelled.build()["label"] == "loud=False"
    assert Labelled.build(loud=True)["label"] == "LOUD"
    with pytest.raises(TypeError, match=r"Give price= or price__\.\.\.=, not both"):
        Labelled.build(priced=True, cheap=True)
    with pytest.raises(TypeError, match=r"Give price= or price__\.\.\.=, not both"):
        Labelled.build(price__amount=Decimal(1), price=None)


def test_params_are_read_by_fields_and_never_reach_the_model() -> None:
    """``currency`` and the traits feed other fields; ``Order`` takes neither."""
    order = OrderFactory.build(currency="CHF")
    assert order.total.currency == "CHF"
    assert BUILT_LINES[-1].price.currency == "CHF"
    with pytest.raises(TypeError, match="unexpected keyword argument 'paid'"):
        Order(**{**vars(OrderFactory.stub()), "paid": True})


def test_maybe_picks_by_a_field_or_a_declaration() -> None:
    """``receipt`` follows ``paid_at``; a decider may be computed too."""
    assert OrderFactory.build().receipt == "none"
    assert OrderFactory.build(paid=True).receipt == "sent"

    class Shelf(Factory[dict[str, Any]]):
        class Meta:
            model = dict

        size = 3
        label = Maybe(LazyAttribute(lambda shelf: shelf.size > 2), "big")
        kind = Maybe("size", "sized", "empty")
        category = Maybe("size", SubFactory(CategoryFactory, name="Shelf"))

    assert Shelf.build(category=None) == {
        "size": 3,
        "label": "big",
        "kind": "sized",
        "category": None,
    }
    small = Shelf.build(size=0)
    assert (small["label"], small["kind"], small["category"]) == (None, "empty", None)
    assert Shelf.build(category__name="Hats")["category"].name == "Hats"


@pytest.mark.anyio
async def test_maybe_adds_the_sub_factory_it_picks() -> None:
    """A stored sub-factory a ``Maybe`` picks is added with the object."""

    class MaybeListed(Factory[Product]):
        class Meta:
            model = list_product
            repository = "products"
            exclude = ("category",)

        class Params:
            listed = False

        id = LazyFunction(uuid4)
        name = "Tea"
        slug = "tea"
        price = SubFactory(MoneyFactory)
        category = Maybe("listed", SubFactory(CategoryFactory), None)
        category_id = LazyAttribute(
            lambda product: product.category.id if product.category else uuid4()
        )
        tags: ClassVar[list[str]] = []

    uow = ShopUnitOfWork()
    unlisted = await MaybeListed.create(uow)
    listed = await MaybeListed.create(uow, listed=True, category__name="Tea")

    assert [category.name for category in uow.categories.items.values()] == ["Tea"]
    assert listed.category_id in uow.categories.items
    assert unlisted.category_id not in uow.categories.items


def test_self_attribute_follows_paths_and_climbs_to_the_parent() -> None:
    """Dotted paths read attributes; each extra dot climbs one factory up."""

    class Tagged(Factory[dict[str, Any]]):
        class Meta:
            model = dict

        price = SubFactory(MoneyFactory)
        currency = SelfAttribute("price.currency")
        missing = SelfAttribute("price.colour", default="none")
        broken = SelfAttribute("...far")

    with pytest.raises(ValueError, match=r"Tagged.broken: '...far' climbs past"):
        Tagged.build()
    tagged = Tagged.build(broken=1)
    assert (tagged["currency"], tagged["missing"]) == ("EUR", "none")

    class Strict(Tagged):
        missing = SelfAttribute("price.colour")

    with pytest.raises(AttributeError, match="colour"):
        Strict.build(broken=1)


def test_hooks_run_on_built_objects_with_what_was_given() -> None:
    """``build`` calls the hook with ``create=False``, the value, and the keywords."""
    calls: list[tuple[object, ...]] = []

    class Hooked(Factory[dict[str, Any]]):
        class Meta:
            model = dict

        name = Sequence(lambda n: f"hooked-{n}")

        @post_generation
        def tags(
            made: dict[str, Any], create: bool, extracted: object, **kw: object
        ) -> None:
            calls.append((made["name"], create, extracted, kw))

    Hooked.build(tags=["a"], tags__loud=True)
    Hooked.build()
    assert calls == [
        ("hooked-0", False, ["a"], {"loud": True}),
        ("hooked-1", False, None, {}),
    ]
    order = OrderFactory.build(noted=["fragile"], noted__loud=True)
    assert order.notes == ["FRAGILE"]


def test_related_factories_build_after_the_object() -> None:
    """Built: a line pointing at the order; ``line__`` reaches it; a value skips it."""
    order = OrderFactory.build(line__sku="TEA")
    (line,) = BUILT_LINES
    assert (line.order_id, line.sku, line.price.currency) == (order.id, "TEA", "EUR")
    OrderFactory.build(line="none, thanks")
    assert len(BUILT_LINES) == 1


@pytest.mark.anyio
async def test_related_factories_and_hooks_commit_with_the_object() -> None:
    """Created: the line and the notes are in the order's one commit, announced."""
    published: list[DomainEvent] = []
    bus = EventBus()

    async def collect(event: OrderNoted) -> None:
        published.append(event)

    bus.subscribe(OrderNoted, collect)
    uow = OrdersUnitOfWork(bus)

    order = await OrderFactory.create(uow, noted=["fragile"], noted__loud=True)

    assert uow.commits == 1
    assert uow.orders.items == {order.id: order}
    (line,) = uow.lines.items.values()
    assert (line.order_id, line.sku) == (order.id, "FIRST")
    assert BUILT_LINES == []
    assert order.notes == ["FRAGILE"]
    assert [event.text for event in published] == ["FRAGILE"]  # type: ignore[attr-defined]
    await OrderFactory.create(uow, line=None)
    assert len(uow.lines.items) == 2  # None is no value: the line is made.
    await OrderFactory.create(uow, line="none, thanks")
    assert len(uow.lines.items) == 2


@pytest.mark.anyio
async def test_a_related_factory_without_a_repository_only_builds() -> None:
    """Its objects are built during ``create``, never added."""
    made: list[dict[str, Any]] = []

    def remember(**fields: Any) -> dict[str, Any]:  # noqa: ANN401 - any field.
        made.append(fields)
        return fields

    class Plain(Factory[dict[str, Any]]):
        class Meta:
            model = remember

        kind = "plain"

    class WithPlain(CategoryFactory):
        extra = RelatedFactory(Plain, "made_for", kind="extra")
        skipped = RelatedFactory(Plain)

    uow = ShopUnitOfWork()
    category = await WithPlain.create(uow)
    assert list(uow.categories.items.values()) == [category]
    assert made == [{"kind": "extra", "made_for": category}, {"kind": "plain"}]
    await WithPlain.create(uow, extra="given")
    assert uow.commits == 2
    assert [fields["kind"] for fields in made[2:]] == ["plain"]


@pytest.mark.anyio
async def test_a_stored_sub_factory_reads_its_parent_during_create() -> None:
    """``create`` adds a stored sub-factory's object knowing what made it."""

    class Shelved(ProductFactory):
        class Params:
            shelf = "Teas"

        category = SubFactory(CategoryFactory, name=SelfAttribute("..shelf"))

    uow = ShopUnitOfWork()
    product = await Shelved.create(uow)

    assert uow.categories.items[product.category_id].name == "Teas"


def test_a_declaration_given_as_an_argument_knows_its_field() -> None:
    """Computed as a default would be, under its own name."""
    with pytest.raises(ValueError, match=r"OrderFactory.receipt: '...x' climbs"):
        OrderFactory.build(receipt=SelfAttribute("...x"))
    # The refused build took ORD-0.
    assert OrderFactory.build(receipt=SelfAttribute("number")).receipt == "ORD-1"


def test_stubs_are_plain_data() -> None:
    """No model is called, sub-factories give stubs, and no hook runs."""
    stub = OrderFactory.stub(paid=True, noted=["x"])

    assert isinstance(stub, SimpleNamespace)
    assert (stub.status, stub.receipt) == ("paid", "sent")
    assert isinstance(stub.total, SimpleNamespace)
    assert stub.total.currency == "EUR"
    assert not hasattr(stub, "paid")
    assert not hasattr(stub, "noted")
    assert BUILT_LINES == []
    assert [s.number for s in OrderFactory.stub_batch(2)] == ["ORD-1", "ORD-2"]


def test_nested_arguments_need_a_declaration_that_takes_them() -> None:
    """``number__x=`` is refused, naming what takes them."""
    with pytest.raises(
        TypeError, match="not a SubFactory, a RelatedFactory, or a post_generation"
    ):
        OrderFactory.build(number__x=1)
    with pytest.raises(TypeError, match="not a SubFactory"):
        OrderFactory.build(receipt__x=1)

"""The generators: aggregates, commands, queries, and events added to a domain."""

import ast
import gc
import importlib
import io
import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

import domainkit
from domainkit.architecture import check_domain
from domainkit.management import CommandError, call_command, load_command
from domainkit.management import generators as generators_module
from domainkit.management.commands import startdomain
from domainkit.management.commands.addcommand import past_tense, verb_of
from domainkit.management.commands.addquery import default_path
from domainkit.management.generators import (
    FIELD_TYPES,
    SETTINGS_FILE,
    URLS_FILE,
    AggregateGenerator,
    Domain,
    FieldSpec,
    FieldType,
    Generator,
    Names,
    Source,
    enumeration,
    install,
    package_directory,
    register_field_type,
    top_level_names,
)
from domainkit_tests.conftest import MakePackage


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


class Shop:
    """A domain ``startdomain`` made: ``gen.shop``, with its tests."""

    def __init__(self, root: Path) -> None:
        """The domain's files under ``root``."""
        self.root = root
        self.package = root / "gen" / "shop"
        self.tests = root / "tests" / "shop"

    def __call__(self, command: str, *args: str, **options: Any) -> str:  # noqa: ANN401 - options.
        """Run a generator on the domain."""
        return run(
            command, "shop", *args, parent="gen", tests_dir=self.tests, **options
        )

    def read(self, relative: str) -> str:
        """A file of the domain package."""
        return (self.package / relative).read_text(encoding="utf-8")

    def test(self, name: str) -> str:
        """A test module of the domain."""
        return (self.tests / name).read_text(encoding="utf-8")


@pytest.fixture
def shop(
    make_package: MakePackage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Shop:
    """``gen.shop``, with the aggregate ``Product``, in a project at ``tmp_path``."""
    make_package("gen", {})
    monkeypatch.setattr(generators_module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(startdomain, "project_root", lambda: tmp_path)
    domain = Shop(tmp_path)
    run(
        "startdomain",
        "shop",
        aggregate="Product",
        parent="gen",
        tests_dir=domain.tests.parent,
    )
    return domain


def test_startaggregate_adds_every_layer(shop: Shop) -> None:
    """The aggregate, its use cases, table, resource, fakes, and tests."""
    output = shop(
        "startaggregate",
        "Category",
        fields=["title:str:100", "parent:ref:Category?", "price:decimal"],
    )

    assert output.startswith("Added Category to gen.shop:\n")
    assert output.endswith(
        'Next: python manage.py makemigrations -m "create categories", then migrate.\n'
    )
    model = shop.read("domain/model.py")
    assert "class Category(AggregateRoot[CategoryId]):" in model
    assert "    title: Annotated[str, MaxLength(100)]\n" in model
    assert "    parent: Reference[CategoryId] | None = None\n" in model
    assert "    price: Annotated[Decimal, Digits(12, 2)]\n" in model
    assert "categories: CategoryRepository" in shop.read("application/unit_of_work.py")
    assert 'table_for(Category, "shop_categories", domain="shop")' in shop.read(
        "infrastructure/orm.py"
    )
    assert "parent=None if command.parent is None else CategoryId(command.parent)" in (
        shop.read("application/services.py")
    )
    assert "categories = ResourceRouter(" in shop.read("api/routes.py")
    fakes = shop.test("fakes.py")
    assert "def new_category() -> Category:" in fakes
    assert '"price": "1.50"' in fakes
    assert (shop.tests / "test_categories.py").is_file()
    assert check_domain("gen.shop") == []


def test_addcommand_adds_a_method_a_use_case_and_a_route(shop: Shop) -> None:
    """``PublishProduct``: method, event, command, service, body, route, tests."""
    shop(
        "addcommand",
        "Product",
        "PublishProduct",
        fields=["at:datetime", "owner:ref:Owner", "note:str:50?"],
    )
    shop("addcommand", "Product", "CopyProduct", event="Copied")

    model = shop.read("domain/model.py")
    assert "def publish(self, *, at: datetime, owner: OwnerId, note: str" in model
    assert "self.record(ProductPublished(product_id=self.id, at=at," in model
    assert '"""A product was published."""' in shop.read("domain/events.py")
    assert '"""Copied."""' in shop.read("domain/events.py")
    assert "owner=OwnerId(command.owner)" in shop.read("application/services.py")
    routes = shop.read("api/routes.py")
    assert "class PublishProductIn(BaseModel):" in routes
    assert "    at: AwareDatetime\n" in routes
    assert "note: str | None = Field(default=None, max_length=50)" in routes
    assert "service=services.publish_product," in routes
    assert 'path="/publish"' in routes
    assert 'CopyProduct, service=services.copy_product, path="/copy"' in routes
    assert "def test_copy_product_route" in shop.test("test_copy_product.py")
    assert check_domain("gen.shop") == []


def test_addquery_adds_a_query_its_handler_and_route(shop: Shop) -> None:
    """``ProductsByOwner``: types as the aggregate has them, a route, tests."""
    shop("addquery", "Product", "ProductsByOwner", by=["owner_id", "name"])
    shop("addquery", "Product", "EveryProduct", path="/all")

    queries = shop.read("application/queries.py")
    assert queries.startswith('"""Queries of the shop domain')
    assert "    owner_id: Reference[OwnerId]\n    name: ProductName\n" in queries
    assert "from domainkit.ddd import Query, Reference" in queries
    assert '(F("owner_id") == query.owner_id) & (F("name") == query.name)' in queries
    assert "with the query's owner_id and name" in queries
    assert "paginate(None, limit=100)" in queries
    routes = shop.read("api/routes.py")
    assert 'handler=queries.products_by_owner, path="/by-owner"' in routes
    assert 'path="/all"' in routes
    assert "params={" in shop.test("test_products_by_owner.py")
    assert "params=" not in shop.test("test_every_product.py")
    assert check_domain("gen.shop") == []


def test_addquery_imports_what_the_annotation_names(shop: Shop) -> None:
    """Builtins need nothing; the model's own names and imports come along."""
    (shop.package / "domain" / "model.py").write_text(
        shop.read("domain/model.py").replace(
            "    archived: bool = False\n",
            "    archived: bool = False\n"
            "    code: Annotated[str, MaxLength(NAME_LENGTH)] = ''\n",
        ),
        encoding="utf-8",
    )

    shop("addquery", "Product", "ProductsCoded", by=["code", "archived"])

    queries = shop.read("application/queries.py")
    assert "from typing import Annotated" in queries
    assert "from domainkit.ddd import MaxLength, Query" in queries
    assert "NAME_LENGTH" in queries


def test_addevent_adds_an_event_and_a_durable_handler(shop: Shop) -> None:
    """The event, its handler, its subscription, and a test of the handler."""
    shop(
        "addevent",
        "Product",
        "ProductRestocked",
        fields=["quantity:int", "at:datetime"],
    )
    shop("addevent", "Product", "ProductNoted", handler=False)

    events = shop.read("domain/events.py")
    assert "class ProductRestocked(DomainEvent):" in events
    assert "    product_id: UUID\n    quantity: int\n    at: datetime\n" in events
    assert "from datetime import datetime" in events
    assert "class ProductNoted(DomainEvent):" in events
    assert "async def on_product_restocked(event: ProductRestocked)" in shop.read(
        "application/handlers.py"
    )
    apps = shop.read("apps.py")
    assert (
        "event_bus.subscribe(ProductRestocked, on_product_restocked, durable=True)"
        in apps
    )
    assert "on_product_noted" not in apps
    assert not (shop.tests / "test_on_product_noted.py").exists()
    assert "quantity=1" in shop.test("test_on_product_restocked.py")


def test_addevent_adds_a_logger_where_there_is_none(shop: Shop) -> None:
    """A handlers module without logging gets it."""
    (shop.package / "application" / "handlers.py").write_text(
        '"""Reactions."""\n', encoding="utf-8"
    )

    shop("addevent", "Product", "ProductRestocked")

    handlers = shop.read("application/handlers.py")
    assert "import logging\n" in handlers
    assert "logger = logging.getLogger(__name__)\n" in handlers


@pytest.mark.parametrize(
    ("command", "args", "options", "message"),
    [
        ("startaggregate", ["Product"], {}, "gen.shop already has Product"),
        ("startaggregate", ["category"], {}, "not a valid CamelCase aggregate name"),
        ("startaggregate", ["Sheep"], {"plural": "sheep"}, "not a plural of 'sheep'"),
        (
            "startaggregate",
            ["Box"],
            {"plural": "Boxes"},
            "not a valid snake_case plural",
        ),
        ("startaggregate", ["Box"], {"fields": ["a", "a:int"]}, "Name each field once"),
        ("startaggregate", ["Box"], {"fields": ["id:int"]}, "set by domainkit"),
        (
            "startaggregate",
            ["Box"],
            {"fields": ["Big"]},
            "not a valid snake_case field",
        ),
        (
            "startaggregate",
            ["Box"],
            {"fields": ["a:blob"]},
            "'blob' is not a field kind",
        ),
        ("startaggregate", ["Box"], {"fields": ["a:int:3"]}, "more options than int"),
        (
            "startaggregate",
            ["Box"],
            {"fields": ["a:ref"]},
            "needs the aggregate it refers",
        ),
        (
            "startaggregate",
            ["Box"],
            {"fields": ["a:ref:lower"]},
            "valid CamelCase aggregate",
        ),
        (
            "startaggregate",
            ["Box"],
            {"fields": ["a:ref:Crate"]},
            "Crate is not an aggregate",
        ),
        ("startaggregate", ["Box"], {"fields": ["a:str:x"]}, "'x' is not a number"),
        ("startaggregate", ["Box"], {"fields": ["a:str:0"]}, "at least 1"),
        ("startaggregate", ["Box"], {"fields": ["a:decimal:4:5"]}, "cannot exceed"),
        ("startaggregate", ["Box"], {"fields": ["a:decimal:39"]}, "from 0 to 38"),
        ("addcommand", ["Product", "Publish"], {}, "<Verb>Product"),
        (
            "addcommand",
            ["Product", "ShipProduct"],
            {"fields": ["to:ref:Port"]},
            "Port is not an aggregate of gen.shop",
        ),
        ("addcommand", ["Product", "Product"], {}, "<Verb>Product"),
        ("addcommand", ["Crate", "OpenCrate"], {}, "gen.shop has no aggregate Crate"),
        (
            "addcommand",
            ["Product", "ArchiveProduct"],
            {},
            "already defines Product.archive",
        ),
        (
            "addcommand",
            ["Product", "ShipProduct"],
            {"plural": "goods"},
            "no resource goods",
        ),
        (
            "addcommand",
            ["Product", "ShipProduct"],
            {"fields": ["product_id:uuid"]},
            "from the path",
        ),
        (
            "addcommand",
            ["Product", "ShipProduct"],
            {"event": "ProductCreated"},
            "already defines ProductCreated",
        ),
        (
            "addquery",
            ["Product", "ProductsNamed"],
            {"by": ["nope"]},
            "declares no field 'nope'",
        ),
        (
            "addquery",
            ["Product", "ProductsNamed"],
            {"by": ["notes"]},
            "fields of one value",
        ),
        (
            "addquery",
            ["Product", "ProductsNamed"],
            {"by": ["name", "name"]},
            "Name each field once",
        ),
        (
            "addquery",
            ["Product", "ProductsNamed"],
            {"by": ["Name"]},
            "snake_case field name",
        ),
        ("addquery", ["Product", "ProductsNamed"], {"path": "/Named"}, "not a path"),
        ("addquery", ["Product", "productsNamed"], {}, "CamelCase query name"),
        ("addquery", ["Crate", "Crates"], {}, "gen.shop has no aggregate Crate"),
        ("addquery", ["Product", "Goods"], {"plural": "goods"}, "no resource goods"),
        (
            "addevent",
            ["Product", "ProductCreated"],
            {},
            "already defines ProductCreated",
        ),
        (
            "addevent",
            ["Product", "ProductShipped"],
            {"fields": ["product_id:uuid"]},
            "leave it out",
        ),
        ("addevent", ["Crate", "CrateShipped"], {}, "gen.shop has no aggregate Crate"),
        ("addevent", ["Product", "productShipped"], {}, "CamelCase event name"),
    ],
)
def test_generators_refuse_what_they_cannot_do(
    shop: Shop, command: str, args: list[str], options: dict[str, Any], message: str
) -> None:
    """Bad names, clashes, and unknown fields are refused before any write."""
    before = {path: path.read_bytes() for path in shop.root.rglob("*.py")}

    with pytest.raises(CommandError, match=message):
        shop(command, *args, **options)

    assert {path: path.read_bytes() for path in shop.root.rglob("*.py")} == before


def test_generators_refuse_clashes_with_what_they_made(shop: Shop) -> None:
    """A second command, query, or handler of one name is refused."""
    shop("addcommand", "Product", "ShipProduct", event="ProductSent")
    shop("addquery", "Product", "ProductsNamed", by=["name"])
    (shop.package / "application" / "handlers.py").write_text(
        shop.read("application/handlers.py")
        + "\n\nasync def on_product_shipped() -> None:\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match=r"already defines Product\.ship\."):
        shop("addcommand", "Product", "ShipProduct", event="ProductMoved")
    with pytest.raises(CommandError, match="already defines ProductsNamed"):
        shop("addquery", "Product", "ProductsNamed")
    with pytest.raises(CommandError, match="already defines on_product_shipped"):
        shop("addevent", "Product", "ProductShipped")
    (shop.tests / "test_hold_product.py").write_text("", encoding="utf-8")
    with pytest.raises(CommandError, match=r"test_hold_product\.py already exists"):
        shop("addcommand", "Product", "HoldProduct")


def test_generators_refuse_files_changed_beyond_recognition(shop: Shop) -> None:
    """Missing files, broken ones, and renamed classes stop them."""
    apps = shop.package / "apps.py"
    apps.write_text('"""No config."""\n', encoding="utf-8")
    with pytest.raises(CommandError, match="has no DomainConfig with ready"):
        shop("addevent", "Product", "ProductShipped")
    (shop.package / "api" / "routes.py").write_text("router = 1\n", encoding="utf-8")
    with pytest.raises(CommandError, match="has no get_unit_of_work"):
        shop("startaggregate", "Box")
    (shop.package / "domain" / "events.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(CommandError, match="is not valid Python"):
        shop("startaggregate", "Box")
    (shop.package / "domain" / "events.py").unlink()
    with pytest.raises(CommandError, match="does not exist; was the domain made"):
        shop("startaggregate", "Box")
    with pytest.raises(CommandError, match="is not a domain made by startdomain"):
        run("startaggregate", "nowhere", "Box", parent="gen")
    with pytest.raises(CommandError, match="not a valid snake_case domain name"):
        run("startaggregate", "No", "Box", parent="gen")


def test_resources_of_the_same_name_are_refused(shop: Shop) -> None:
    """A routes module that already uses the plural is left alone."""
    routes = shop.package / "api" / "routes.py"
    routes.write_text(shop.read("api/routes.py") + "boxes = None\n", encoding="utf-8")

    with pytest.raises(CommandError, match="already defines boxes or BoxIn"):
        shop("startaggregate", "Box")


def test_queries_need_the_model_to_say_where_names_come_from(shop: Shop) -> None:
    """A name the model neither defines nor imports cannot be imported."""
    (shop.package / "domain" / "model.py").write_text(
        shop.read("domain/model.py").replace(
            "    archived: bool = False\n",
            "    archived: bool = False\n    kind: Mystery | None = None\n"
            "    other: Mystery = None\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="does not say where Mystery comes from"):
        shop("addquery", "Product", "ProductsOfOther", by=["other"])
    with pytest.raises(CommandError, match="fields of one value"):
        shop("addquery", "Product", "ProductsOfKind", by=["kind"])


def test_domains_outside_the_project_are_refused(
    make_package: MakePackage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The domain must lie inside the project root."""
    make_package("elsewhere.shop.domain", {"model.py": ""})
    monkeypatch.setattr(generators_module, "project_root", lambda: tmp_path / "project")

    with pytest.raises(CommandError, match="is outside the project"):
        run("startaggregate", "shop", "Box", parent="elsewhere")


def test_new_python_files_are_planned_once(tmp_path: Path) -> None:
    """``start`` refuses a file that exists, or one already planned."""
    domain = Domain("shop", "shop", tmp_path, tmp_path)
    (tmp_path / "taken.py").write_text("", encoding="utf-8")

    domain.start(tmp_path / "new.py", '"""New."""\n')

    for path in (tmp_path / "taken.py", tmp_path / "new.py"):
        with pytest.raises(CommandError, match="already exists"):
            domain.start(path, "")


def test_sources_find_insert_and_answer(tmp_path: Path) -> None:
    """The edits generators make, on a small module."""
    path = tmp_path / "module.py"
    path.write_text(
        "class A:\n    x: int\n    y = 1\n\n    def f(self) -> None:\n        pass\n",
        encoding="utf-8",
    )
    source = Source.read(path)

    source.add_imports(["import os"])
    source.insert_after_imports(["", "LIMIT = 1"])
    source.insert_before("class A", ["# A comes next.", ""])

    assert source.text.startswith("import os\n\nLIMIT = 1\n# A comes next.\n\nclass A:")
    assert all(source.defines(f"A.{name}") for name in ("x", "y", "f"))
    assert not source.defines("A.z")
    assert source.annotation("A", "y") is None
    assert source.origin("os") is None
    assert source.classes() == ["A"]
    with pytest.raises(CommandError, match="has no line starting 'nothing'"):
        source.insert_before("nothing", [])
    with pytest.raises(CommandError, match="has no B; was it renamed"):
        source.defines("B.x")
    assert source.save()
    assert not source.save()


def test_imports_go_after_the_docstring_of_a_module_without_any(tmp_path: Path) -> None:
    """With no imports, after the docstring; with no docstring, at the top."""
    documented = Source(tmp_path / "a.py", '"""Doc."""\n')
    bare = Source(tmp_path / "b.py", "x = 1\n")

    documented.insert_after_imports(["import os"])
    bare.insert_after_imports(["import os"])

    assert documented.text == '"""Doc."""\nimport os\n'
    assert bare.text == "import os\nx = 1\n"


def test_field_kinds_can_be_registered() -> None:
    """A project's own kind generates as the built-in ones do."""
    register_field_type("email", FieldType("Annotated[str, Email]", "String"))
    try:
        spec = FieldSpec.parse("contact:email?")
        assert spec.annotation() == "Annotated[str, Email] | None"
        assert spec.plain() == "Annotated[str, Email] | None"
        assert spec.body() == "Annotated[str, Email] | None = None"
        with pytest.raises(CommandError, match="not a valid snake_case field kind"):
            register_field_type("E-mail", FieldType("str", "String"))
    finally:
        del FIELD_TYPES["email"]


def test_field_specs_write_examples_and_conversions() -> None:
    """Examples at the field's scale; references converted to their ids."""
    amount = FieldSpec.parse("price:decimal:6:0")
    reference = FieldSpec.parse("owner:ref:Owner")
    optional = FieldSpec.parse("owner:ref:Owner?")
    when = FieldSpec.parse("at:datetime")

    assert (amount.example(), amount.request()) == ('Decimal("1")', '"1"')
    assert amount.body() == "Decimal = Field(max_digits=6, decimal_places=0)"
    assert reference.to_domain("v") == "OwnerId(v)"
    assert optional.to_domain("v") == "None if v is None else OwnerId(v)"
    assert (reference.domain_type(), optional.domain_type()) == (
        "OwnerId",
        "OwnerId | None",
    )
    assert reference.domain_example() == "OwnerId(UUID(int=1))"
    assert when.domain_example() == "datetime(2026, 1, 2, tzinfo=UTC)"
    assert when.body() == "AwareDatetime"
    assert FieldSpec.parse("n:int?").body() == (
        "int | None = Field(default=None, ge=-(2**63), lt=2**63)"
    )


def test_names_and_words() -> None:
    """Plurals, verbs, tenses, paths, and lists of names."""
    names = Names.of("OrderLine", None)

    assert (names.snake, names.plural, names.title) == (
        "order_line",
        "order_lines",
        "order line",
    )
    assert verb_of("ShipOrderLine", "OrderLine") == "Ship"
    assert [
        past_tense(verb) for verb in ("Publish", "Archive", "Copy", "Play", "Destroy")
    ] == ["Published", "Archived", "Copied", "Played", "Destroyed"]
    assert default_path("OrderLinesByOwner", names) == "/by-owner"
    assert default_path("OrderLines", names) == "/order-lines"
    assert default_path("EveryOrderLine", names) == "/every-order-line"
    assert [
        enumeration(items) for items in ([], ["a"], ["a", "b"], ["a", "b", "c"])
    ] == [
        "",
        "a",
        "a and b",
        "a, b and c",
    ]


GENERATE = [
    ["startdomain", "catalog", "--aggregate", "Product"],
    [
        "startaggregate",
        "catalog",
        "Category",
        *("--field", "title:str:100", "--field", "parent:ref:Category?"),
        *("--field", "rank:int", "--field", "price:decimal:10:2"),
        *("--field", "due:date?", "--field", "at:datetime", "--field", "flag:bool"),
        *("--field", "ratio:float", "--field", "code:uuid", "--field", "body:text"),
    ],
    [
        "addcommand",
        "catalog",
        "Product",
        "PublishProduct",
        *("--field", "at:datetime", "--field", "note:str:50?"),
    ],
    [
        "addcommand",
        "catalog",
        "Category",
        "RetitleCategory",
        *("--field", "title:str:100", "--field", "parent:ref:Category?"),
        *("--field", "price:decimal:10:2"),
    ],
    ["addcommand", "catalog", "Category", "ArchiveCategory"],
    ["addquery", "catalog", "Product", "ProductsByOwner", "--by", "owner_id"],
    [
        "addquery",
        "catalog",
        "Product",
        "ProductsNamed",
        "--by",
        "name",
        "--by",
        "archived",
    ],
    [
        "addquery",
        "catalog",
        "Category",
        "CategoriesByTitle",
        "--by",
        "title",
        "--by",
        "code",
    ],
    ["addquery", "catalog", "Category", "EveryCategory", "--path", "/every"],
    [
        "addevent",
        "catalog",
        "Product",
        "ProductRestocked",
        *("--field", "quantity:int", "--field", "at:datetime"),
    ],
    ["addevent", "catalog", "Category", "CategoryNoted", "--no-handler"],
    ["makemigrations", "-m", "catalog"],
    ["migrate"],
    ["check"],
]


@pytest.mark.skipif(
    find_spec("mypy") is None or find_spec("ruff") is None,
    reason="needs mypy and ruff",
)
def test_generated_code_passes_its_tests_ruff_and_mypy(tmp_path: Path) -> None:
    """Every generator in a new project; then its tests, ruff, and mypy --strict."""
    source = str(Path(domainkit.__file__).parents[1])
    env = {
        **{
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("DOMAINKIT_", "SHOP_"))
        },
        "PYTHONPATH": source,
    }

    def python(*args: str) -> str:
        done = subprocess.run(  # noqa: S603 - the test interpreter and generated files.
            [sys.executable, *args],
            cwd=tmp_path / "shop",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"{args}:\n{done.stdout}{done.stderr}"
        return done.stdout

    run("init", "shop", str(tmp_path / "shop"))
    for arguments in GENERATE:
        python("manage.py", *arguments)
    tested = python("-m", "pytest", "-q", "-p", "no:cacheprovider")
    python("-m", "ruff", "check", "--no-cache", ".")
    python("-m", "ruff", "format", "--check", "--no-cache", ".")
    python("-m", "mypy", "--strict", "--no-incremental", "src", "tests")

    assert " passed" in tested
    assert "failed" not in tested


SETTINGS = 'INSTALLED_DOMAINS = [\n    "domains.shop",\n]\n'
URLS = '"""Routes."""\n\nfrom fastapi import APIRouter\n\nrouter = APIRouter()\n'


def project(root: Path, settings: str = SETTINGS, urls: str = URLS) -> Path:
    """A project laid out as ``init`` makes it, with these two files."""
    for path, text in ((SETTINGS_FILE, settings), (URLS_FILE, urls)):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    return root


def test_install_adds_the_domain_and_mounts_its_router(tmp_path: Path) -> None:
    """Both files edited, and formatted."""
    root = project(tmp_path)

    done = install("domains.catalog", "catalog", root)

    assert done == [
        "Added 'domains.catalog' to INSTALLED_DOMAINS in src/config/settings/base.py.",
        'Mounted its router at "/api/v1/catalog" in src/config/urls.py.',
    ]
    assert (root / SETTINGS_FILE).read_text(encoding="utf-8") == (
        'INSTALLED_DOMAINS = [\n    "domains.shop",\n    "domains.catalog",\n]\n'
    )
    urls = (root / URLS_FILE).read_text(encoding="utf-8")
    assert "from domains.catalog.api.routes import router as catalog_router\n" in urls
    assert urls.endswith(
        'router.include_router(catalog_router, prefix="/api/v1/catalog")\n'
    )


@pytest.mark.parametrize(
    ("settings", "urls"),
    [
        ("INSTALLED_DOMAINS = DOMAINS\n", URLS),
        ("INSTALLED_DOMAINS = [NAME]\n", URLS),
        ('INSTALLED_DOMAINS = [\n    "domains.catalog",\n]\n', URLS),
        ('INSTALLED_DOMAINS = [\n    "a",  # the first\n]\n', URLS),
        ("OTHER = []\n", URLS),
        ("INSTALLED_DOMAINS = [\n", URLS),
        (SETTINGS, "app = None\n"),
        (SETTINGS, URLS + "catalog_router = None\n"),
        (SETTINGS, URLS + "if True:\n    from domains.catalog.api.routes import x\n"),
    ],
)
def test_install_leaves_projects_it_does_not_recognise_alone(
    tmp_path: Path, settings: str, urls: str
) -> None:
    """Anything but the literal list and a plain router: no edit at all."""
    root = project(tmp_path, settings, urls)

    assert install("domains.catalog", "catalog", root) == []
    assert (root / SETTINGS_FILE).read_text(encoding="utf-8") == settings
    assert (root / URLS_FILE).read_text(encoding="utf-8") == urls


def test_install_needs_both_files(tmp_path: Path) -> None:
    """A project without its settings or URLs is not one ``init`` made."""
    root = project(tmp_path)
    (root / URLS_FILE).unlink()

    assert install("domains.catalog", "catalog", root) == []


MINIMAL = {
    "__init__.py": "",
    "domain/__init__.py": "",
    "domain/model.py": (
        '"""Things."""\n\nimport typing\nimport uuid\n\nimport domainkit.ddd\n\n'
        'ThingId = typing.NewType("ThingId", uuid.UUID)\n'
        'BoxId = typing.NewType("BoxId", uuid.UUID)\n\n\n'
        "class Thing(domainkit.ddd.AggregateRoot[ThingId]):\n"
        '    """A thing."""\n\n    name: str\n\n\n'
        "class Box(domainkit.ddd.AggregateRoot[BoxId]):\n"
        '    """What things go in."""\n'
    ),
    "domain/events.py": '"""Events."""\n',
    "application/__init__.py": "",
    "application/commands.py": '"""Commands."""\n',
    "application/services.py": '"""Services."""\n',
    "application/handlers.py": '"""Handlers."""\n',
    "application/unit_of_work.py": (
        '"""Port."""\n\n\nclass ThingsUnitOfWork:\n    """Port."""\n'
    ),
    "infrastructure/__init__.py": "",
    "infrastructure/orm.py": (
        '"""Tables."""\n\n\ndef start_mappers() -> None:\n    """Map."""\n'
    ),
    "infrastructure/unit_of_work.py": (
        '"""Adapter."""\n\n\nclass SqlAlchemyThingsUnitOfWork:\n'
        '    """Adapter."""\n\n'
        "    def create_repositories(self, session: object) -> None:\n"
        '        """Repositories."""\n'
    ),
    "api/__init__.py": "",
    "api/routes.py": (
        '"""Routes."""\n\nimport fastapi\n\n'
        "from domainkit.resources import ResourceRouter\n\n"
        "router = fastapi.APIRouter()\n\n\n"
        'def get_unit_of_work() -> None:\n    """The unit of work."""\n\n\n'
        'things = ResourceRouter("/things")\n'
        "router.include_router(things)\n"
    ),
    "apps.py": (
        '"""Apps."""\n\n\nclass Helper:\n    """Not the domain\'s config."""\n\n'
        '    def ready(self) -> None:\n        """Not called."""\n\n\n'
        'class ThingsConfig:\n    """Config."""\n\n'
        '    def ready(self) -> None:\n        """Ready."""\n'
    ),
}
"""A domain the generators did not make: it imports nothing they need by name."""


def make_minimal(root: Path) -> Path:
    """Write ``minimal.things`` under ``root``; return the ``minimal`` directory."""
    for relative, text in MINIMAL.items():
        path = root / "minimal" / "things" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "minimal" / "__init__.py").write_text("", encoding="utf-8")
    (root / "minimal" / "tests").mkdir()
    (root / "minimal" / "tests" / "fakes.py").write_text(
        '"""Fakes."""\n\n\nclass FakeThingsUnitOfWork:\n    """Fake."""\n\n'
        '    def __init__(self) -> None:\n        """Fake."""\n',
        encoding="utf-8",
    )
    return root / "minimal"


def contents(directory: Path) -> dict[Path, str]:
    """Every Python file under ``directory``, by path."""
    return {path: path.read_text(encoding="utf-8") for path in directory.rglob("*.py")}


@pytest.mark.skipif(find_spec("ruff") is None, reason="needs ruff to check")
@pytest.mark.parametrize(
    ("command", "args", "options"),
    [
        (
            "startaggregate",
            ("Widget",),
            {"fields": ["title:str:100", "box:ref:Box", "at:datetime"]},
        ),
        ("startaggregate", ("Tag",), {}),
        (
            "addcommand",
            ("Thing", "BoxThing"),
            {"fields": ["into:ref:Box", "cost:decimal:8:2"]},
        ),
        ("addcommand", ("Thing", "PackUpThing"), {"event": "ThingPacked"}),
        ("addquery", ("Thing", "ThingsNamed"), {"by": ["name"]}),
        ("addevent", ("Thing", "ThingSeen"), {"fields": ["at:datetime"]}),
    ],
)
def test_each_generator_imports_what_it_writes(
    shop: Shop, command: str, args: tuple[str, ...], options: dict[str, Any]
) -> None:
    """Alone, on a domain it did not make; it lists exactly the files it wrote."""
    minimal = make_minimal(shop.root)
    before = contents(minimal)

    output = run(
        command,
        "things",
        *args,
        parent="minimal",
        tests_dir=minimal / "tests",
        **options,
    )

    after = contents(minimal)
    changed = sorted(str(path) for path in after if after[path] != before.get(path))
    assert output.splitlines()[0] == f"Added {args[-1]} to minimal.things:"
    listed = sorted(
        line.strip() for line in output.splitlines() if line.startswith("  ")
    )
    assert listed == changed
    undefined = subprocess.run(  # noqa: S603 - this interpreter's ruff.
        [
            *(sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated"),
            *("--select", "F821,F811", str(minimal)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert undefined.returncode == 0, undefined.stdout
    # The modules they import from exist: the layers that only declare things.
    try:
        for layer in ("domain.events", "domain.model", "application.commands"):
            importlib.import_module(f"minimal.things.{layer}")
    finally:
        for name in [name for name in sys.modules if name.startswith("minimal")]:
            del sys.modules[name]
        gc.collect()  # Its event classes leave DomainEvent.__subclasses__().


def test_startaggregate_refuses_a_name_half_taken(shop: Shop) -> None:
    """An id type alone is enough to refuse: the aggregate would redefine it."""
    minimal = make_minimal(shop.root)
    model = minimal / "things" / "domain" / "model.py"
    model.write_text(
        model.read_text(encoding="utf-8")
        + 'CrateId = typing.NewType("CrateId", uuid.UUID)\n',
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="Crate"):
        run("startaggregate", "things", "Crate", parent="minimal")


SNAPSHOTS = Path(__file__).parent / "snapshots" / "generators"
"""What the generators write, file by file; regenerate with UPDATE_SNAPSHOTS=1."""


@pytest.mark.skipif(find_spec("ruff") is None, reason="needs ruff to format")
def test_generators_write_what_their_snapshots_hold(shop: Shop) -> None:
    """Every generated file, and every generator's arguments, as reviewed."""
    shop(
        "startaggregate",
        "Category",
        fields=["title:str:100", "parent:ref:Category?", "price:decimal:10:2"],
    )
    shop(
        "addcommand",
        "Product",
        "PublishProduct",
        fields=["at:datetime", "note:str:50?"],
    )
    shop(
        "addcommand",
        "Category",
        "RetitleCategory",
        fields=["title:str:100", "parent:ref:Category?", "rank:int"],
    )
    shop("addquery", "Product", "ProductsByOwner", by=["owner_id", "name"])
    shop("addquery", "Category", "EveryCategory", path="/every")
    shop("addquery", "Category", "CategoriesTitled", by=["title"])
    shop("addcommand", "Product", "LockProduct")
    shop(
        "addevent",
        "Product",
        "ProductRestocked",
        fields=["quantity:int", "at:datetime"],
    )
    minimal = make_minimal(shop.root)
    run(
        "startaggregate",
        "things",
        "Widget",
        fields=["title:str:100", "owner:ref:Thing", "body:text?"],
        parent="minimal",
        tests_dir=minimal / "tests",
    )
    run(
        "startaggregate", "things", "Tag", parent="minimal", tests_dir=minimal / "tests"
    )
    run(
        "addcommand",
        "things",
        "Thing",
        "ShipThing",
        fields=["at:datetime", "cost:decimal:8:2", "to:ref:Thing", "code:uuid"],
        event="ThingSent",
        parent="minimal",
        tests_dir=minimal / "tests",
    )
    run(
        "addcommand",
        "things",
        "Thing",
        "PackUpThing",
        event="ThingPacked",
        parent="minimal",
        tests_dir=minimal / "tests",
    )
    run(
        "addquery",
        "things",
        "Thing",
        "ThingsNamed",
        by=["name"],
        parent="minimal",
        tests_dir=minimal / "tests",
    )
    run(
        "addevent",
        "things",
        "Thing",
        "ThingShipped",
        fields=["at:datetime", "code:uuid", "cost:decimal"],
        parent="minimal",
        tests_dir=minimal / "tests",
    )
    written = {
        path.relative_to(shop.root).as_posix(): path.read_text(encoding="utf-8")
        for path in [
            *shop.package.rglob("*.py"),
            *shop.tests.rglob("*.py"),
            *minimal.rglob("*.py"),
        ]
    }
    for name in ("startaggregate", "addcommand", "addquery", "addevent"):
        command = load_command(name)
        parser = command.create_parser("manage.py", name)
        written[f"help/{name}"] = json.dumps(
            {
                "help": command.help,
                "arguments": [
                    [
                        each.option_strings,
                        each.dest,
                        type(each).__name__,
                        each.help,
                        str(each.default),
                    ]
                    for each in parser._actions
                    if each.dest != "help"
                ],
            },
            indent=1,
        )

    if os.environ.get("UPDATE_SNAPSHOTS"):
        for relative, text in written.items():
            target = SNAPSHOTS / f"{relative}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    expected = {
        path.relative_to(SNAPSHOTS).as_posix().removesuffix(".txt"): path.read_text(
            encoding="utf-8"
        )
        for path in SNAPSHOTS.rglob("*.txt")
    }
    assert sorted(written) == sorted(expected)
    for relative, text in written.items():
        assert text == expected[relative], relative


def test_sources_edit_exactly_where_they_say(tmp_path: Path) -> None:
    """Imports after imports; classes and functions from their own scope."""
    source = Source(
        tmp_path / "module.py",
        '"""Doc."""\n\nimport os\nfrom x import y as z\n\nVALUE = 1\n\n\n'
        'class A:\n    """A."""\n\n    def f(self) -> None:\n        pass\n\n\n'
        "class B:\n    def f(self) -> None:\n        one = 1\n        two = 2\n\n\n"
        "class C:\n    pass\n",
    )

    source.add_imports(["import sys"])
    source.add_to_function("B.f", ["three = 3"])
    source.add_to_class("A", ["", "x = 1"])
    source.add_attribute("C", "y: int")
    source.insert_before_class("B", "D = 4")
    source.append("E = 5\n\n")

    assert source.text == (
        '"""Doc."""\n\nimport os\nfrom x import y as z\nimport sys\n\nVALUE = 1\n\n\n'
        'class A:\n    """A."""\n\n    def f(self) -> None:\n        pass\n\n'
        "    x = 1\n\n\n"
        "D = 4\n\n\n"
        "class B:\n    def f(self) -> None:\n        one = 1\n        two = 2\n"
        "        three = 3\n\n\n"
        "class C:\n    pass\n    y: int\n\n\nE = 5\n"
    )
    assert top_level_names(ast.parse("import os.path\nimport a.b as c")) == {"os", "c"}
    assert top_level_names(source.tree()) == {
        "os",
        "z",
        "sys",
        "VALUE",
        "A",
        "B",
        "C",
        "D",
        "E",
    }


def test_sources_place_attributes_after_a_docstring(tmp_path: Path) -> None:
    """A class without attributes gets one after its docstring; one statement bodies."""
    source = Source(
        tmp_path / "module.py",
        'class A:\n    """A."""\n\n\ndef g() -> None:\n    return None\n',
    )

    source.add_attribute("A", "x: int")
    source.add_to_function("g", ["# done"])

    assert source.text == (
        'class A:\n    """A."""\n    x: int\n\n\ndef g() -> None:\n    return None\n'
        "    # done\n"
    )
    empty = Source(tmp_path / "empty.py", "")
    empty.insert_after_imports(["import os"])
    assert empty.text == "import os\n"


def test_install_finds_the_list_after_other_settings(tmp_path: Path) -> None:
    """Other assignments come first and after; a same-looking list elsewhere stays."""
    before = 'import os\n\nos.environ["A"] = "b"\nOTHER = [\n    "domains.shop",\n]\n'
    root = project(
        tmp_path,
        f'{before}INSTALLED_DOMAINS = [\n    "domains.shop",\n]\nLAST = 1\n',
    )

    assert install("domains.catalog", "catalog", root)
    assert (root / SETTINGS_FILE).read_text(encoding="utf-8") == (
        f"{before}INSTALLED_DOMAINS = [\n"
        '    "domains.shop",\n    "domains.catalog",\n]\nLAST = 1\n'
    )


def test_parent_packages_must_be_importable() -> None:
    """A missing package, or one inside a missing package, is refused."""
    for name in ("no_such_parent", "no_such_parent.child", "os"):
        with pytest.raises(CommandError, match=f"Parent package '{name}' cannot be"):
            package_directory(name)


@pytest.mark.parametrize(
    "spec",
    ["price:decimal:38:2", "price:decimal:4:4", "code:str:1", "code:str:10000000"],
)
def test_field_options_reach_their_bounds(spec: str) -> None:
    """The bounds themselves are allowed."""
    assert FieldSpec.parse(spec).options


@pytest.mark.parametrize("spec", ["class", "class:int", "if:str"])
def test_keywords_are_not_field_names(spec: str) -> None:
    """A Python keyword cannot name a field."""
    with pytest.raises(CommandError, match="snake_case field name"):
        FieldSpec.parse(spec)


def test_generators_read_their_command_lines(
    shop: Shop, make_package: MakePackage
) -> None:
    """Repeated options, and paths given as text, as a shell passes them."""
    output = run(
        "startaggregate",
        "shop",
        "Box",
        "--field",
        "width:int",
        "--field",
        "depth:int",
        "--parent",
        "gen",
        "--tests-dir",
        str(shop.tests),
    )
    run(
        "addquery",
        "shop",
        "Box",
        "BoxesSized",
        "--by",
        "width",
        "--by",
        "depth",
        "--parent",
        "gen",
        "--tests-dir",
        str(shop.tests),
    )

    assert "    width: int\n    depth: int\n" in shop.read("domain/model.py")
    assert '(F("width") == query.width) & (F("depth") == query.depth)' in shop.read(
        "application/queries.py"
    )
    assert output.splitlines()[1:] == [
        f"  {shop.tests / 'test_boxes.py'}",
        *(
            f"  {shop.package / relative}"
            for relative in (
                "domain/model.py",
                "domain/events.py",
                "application/commands.py",
                "application/services.py",
                "application/unit_of_work.py",
                "infrastructure/orm.py",
                "infrastructure/unit_of_work.py",
                "api/routes.py",
            )
        ),
        f"  {shop.tests / 'fakes.py'}",
        'Next: python manage.py makemigrations -m "create boxes", then migrate.',
    ]


def test_tests_go_under_tests_by_default_and_their_directory_is_made(
    shop: Shop,
) -> None:
    """Without ``--tests-dir``, ``tests/<parent>/<domain>``, made if missing."""
    output = run("addcommand", "shop", "Product", "HoldProduct", parent="gen")

    assert (shop.root / "tests" / "gen" / "shop" / "test_hold_product.py").is_file()
    assert output.startswith("Added HoldProduct to gen.shop:\n")
    assert f"  {shop.package / 'domain' / 'model.py'}" in output.splitlines()


def test_tests_of_nested_packages_go_under_each_package(shop: Shop) -> None:
    """``tests/<parent>/<subpackage>/<domain>`` for a parent of several parts."""
    make_minimal(shop.root / "outer")
    (shop.root / "outer" / "__init__.py").write_text("", encoding="utf-8")

    run("addevent", "things", "Thing", "ThingSeen", parent="outer.minimal")

    tests = shop.root / "tests" / "outer" / "minimal" / "things"
    assert (tests / "test_on_thing_seen.py").is_file()


def test_queries_refuse_optional_annotations(shop: Shop) -> None:
    """``Optional[X]`` may be None as ``X | None`` may."""
    (shop.package / "domain" / "model.py").write_text(
        shop.read("domain/model.py").replace(
            "    archived: bool = False\n",
            "    archived: bool = False\n    hint: Optional[str] = None\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="fields of one value"):
        shop("addquery", "Product", "ProductsHinted", by=["hint"])


def test_generators_plan_everything_before_they_write() -> None:
    """``Generator`` runs ``plan``, then ``apply``, then prints (DSGN-10)."""
    steps: list[str] = []

    class Greeter(Generator[str]):
        """A generator that writes nothing, for the order of its steps."""

        def plan(self, *, name: str, **options: object) -> str:
            steps.append("plan")
            if name == "taken":
                raise CommandError("taken")
            return name

        def apply(self, plan: str) -> str:
            steps.append("apply")
            return f"Hello, {plan}."

    output = io.StringIO()
    Greeter(stdout=output).handle(name="world")
    with pytest.raises(CommandError, match="taken"):
        Greeter(stdout=io.StringIO()).handle(name="taken")

    assert output.getvalue() == "Hello, world.\n"
    assert steps == ["plan", "apply", "plan"]
    commands = ("startaggregate", "addcommand", "addquery", "addevent")
    assert all(
        isinstance(load_command(command), AggregateGenerator) for command in commands
    )
    assert isinstance(load_command("startdomain"), Generator)

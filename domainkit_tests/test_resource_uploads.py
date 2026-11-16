"""A resource takes a file: ``upload_route`` (user report U3).

The body is the file (its name in ``X-Filename``), so a phone can sign the
bytes it sends, or a form. The route checks what a command route checks,
receives the file as ``save_upload`` does, stores it before the service
runs, and deletes it when the request fails first. A route whose rule asks
for an authorization gets a twin that stores nothing (ADR 0167).
"""

import hashlib
import logging
from decimal import Decimal
from typing import IO, Any, Final
from urllib.parse import quote
from uuid import UUID

import anyio
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import AnyUser
from domainkit.ddd import Command, Conflict
from domainkit.ddd.query import F
from domainkit.http.access import BasePermission
from domainkit.http.errors import install_exception_handlers
from domainkit.http.middleware import RequestBodyLimitMiddleware
from domainkit.http.uploads import CHECKED_PDF, IMAGES, FileType
from domainkit.resources import ResourceRouter
from domainkit.resources.routing import uploading
from domainkit.storage import MemoryStorage, StoredUpload
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.support.documents import one_page
from domainkit_tests.test_resource_authorizations import AUTHORIZED, Asks
from domainkit_tests.test_resources import (
    LOCATION,
    Users,
    as_,
    everyone,
    gadgets,
)

PDF_FILE: Final = one_page()
SCRIPTED: Final = one_page(
    "", "/Annots [4 0 R]", "<< /A << /S /JavaScript /JS (x) >> >>"
)
PNG_FILE: Final = b"\x89PNG\r\n\x1a\n" + b"\0" * 100


class AttachManual(Command):
    """Attach a gadget's manual."""

    gadget_id: UUID
    manual: StoredUpload
    manual_sha256: str = ""


class AddManual(Command):
    """Make a gadget from its manual."""

    manual: StoredUpload


class Run:
    """The services of the tests: what they were given, and how they end."""

    def __init__(self) -> None:
        """Nothing run yet; every run succeeds."""
        self.given: list[object] = []
        self.fail: Exception | None = None

    async def attach(self, command: AttachManual, uow: FakeGadgetsUnitOfWork) -> Gadget:
        """Name the gadget after its manual."""
        self.given.append(command)
        if self.fail is not None:
            raise self.fail
        async with uow:
            gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
            gadget.rename(command.manual.filename)
            gadget.version += 1
            await uow.commit()
        return gadget

    async def add(self, command: AddManual, uow: FakeGadgetsUnitOfWork) -> Gadget:
        """A new gadget, named after its manual."""
        self.given.append(command)
        gadget = make_gadget(command.manual.filename)
        async with uow:
            await uow.gadgets.add(gadget)
            await uow.commit()
        return gadget


def app_of(resource: ResourceRouter) -> FastAPI:
    """An application of ``resource``, its body limits held."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(Users, users=everyone())
    return app


def client_of(resource: ResourceRouter) -> TestClient:
    """A test client of ``resource``'s application."""
    return TestClient(app_of(resource))


@pytest.fixture
def store() -> InMemoryGadgets:
    """Two gadgets: a (cheap), b (dear)."""
    return InMemoryGadgets(
        [make_gadget("a", price="1.00"), make_gadget("b", price="9.00")]
    )


def ids(store: InMemoryGadgets) -> dict[str, UUID]:
    """The gadgets' ids, by name."""
    return {gadget.name: gadget.id for gadget in store.items.values()}


def test_the_body_is_the_file_and_its_name_a_header(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``PUT /gadgets/{id}/manual``: the raw body, ``X-Filename`` percent-encoded."""
    use_auth()
    run = Run()
    storage = MemoryStorage()
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=run.attach,
        field="manual",
        digest="manual_sha256",
        types=[CHECKED_PDF],
        max_size=10_000,
        storage=storage,
        directory="manuals",
        method="PUT",
        path="/manual",
    )
    client = client_of(resource)
    gadget = ids(store)["a"]

    answer = client.put(
        f"{LOCATION}{gadget}/manual",
        content=PDF_FILE,
        headers=as_("changer", **{"X-Filename": quote("دليل.pdf")}),
    )

    assert answer.status_code == 200
    assert answer.json()["name"] == "دليل.pdf"
    (command,) = run.given
    assert isinstance(command, AttachManual)
    assert command.manual.name.startswith("manuals/")
    assert command.manual.content_type == "application/pdf"
    assert command.manual.size == len(PDF_FILE)
    assert command.manual_sha256 == hashlib.sha256(PDF_FILE).hexdigest()
    assert command.manual.sha256 == command.manual_sha256
    assert storage.files == {command.manual.name: PDF_FILE}


def test_a_form_works_too_and_a_collection_route_creates(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A form's part named after the field; ``POST`` on the collection is 201."""
    use_auth()
    run = Run()
    storage = MemoryStorage()
    resource = gadgets(store)
    resource.upload_route(
        AddManual,
        service=run.add,
        field="manual",
        types=IMAGES,
        max_size=1000,
        storage=storage,
    )
    client = client_of(resource)

    made = client.post(
        "/gadgets",
        files={"manual": ("cover.png", PNG_FILE, "text/html")},
        data={"note": "ignored"},
        headers=as_("adder"),
    )
    no_part = client.post(
        "/gadgets", files={"other": ("x.png", PNG_FILE)}, headers=as_("adder")
    )
    nameless = client.post("/gadgets", content=PNG_FILE, headers=as_("adder"))

    assert made.status_code == 201
    assert made.headers["location"].startswith(LOCATION)
    assert made.json()["name"] == "cover.png"
    assert (no_part.status_code, no_part.json()["code"]) == (400, "empty_file")
    assert nameless.json()["name"] == "file"
    assert len(storage.files) == 2


def test_a_file_refused_is_never_stored(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Its type, its check, its size, and the route's body limit."""
    use_auth()
    run = Run()
    storage = MemoryStorage()
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=run.attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=1000,
        storage=storage,
        path="/manual",
    )
    client = client_of(resource)
    act = f"{LOCATION}{ids(store)['a']}/manual"

    def sent(content: bytes) -> tuple[int, str]:
        answer = client.post(act, content=content, headers=as_("changer"))
        return answer.status_code, answer.json()["code"]

    assert sent(PNG_FILE) == (415, "unsupported_file_type")
    assert sent(SCRIPTED) == (422, "unsafe_file")
    assert sent(PDF_FILE + b"%" * 1000) == (413, "file_too_large")
    assert sent(PDF_FILE + b"%" * 70_000) == (413, "content_too_large")
    assert sent(b"") == (400, "empty_file")
    assert run.given == []
    assert storage.files == {}


def test_the_reach_is_checked_as_a_commands_and_a_failure_deletes_the_file(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Rules first (403, nothing read); out of scope (404), a conflict, 412: gone."""
    use_auth()
    run = Run()
    storage = MemoryStorage()

    async def cheap_only(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
        return F("price") < Decimal("2.50")

    resource = gadgets(store, scope=cheap_only)
    resource.upload_route(
        AttachManual,
        service=run.attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=10_000,
        storage=storage,
        path="/manual",
    )
    client = client_of(resource)
    found = ids(store)

    def sent(name: str, user: str = "changer", **headers: str) -> int:
        answer = client.post(
            f"{LOCATION}{found[name]}/manual",
            content=PDF_FILE,
            headers=as_(user, **headers),
        )
        return answer.status_code

    assert sent("a", user="viewer") == 403
    assert sent("b") == 404
    assert sent("a", **{"If-Match": '"99"'}) == 412
    run.fail = Conflict("Not now.")
    assert sent("a") == 409
    assert storage.files == {}
    run.fail = None
    assert sent("a") == 200
    assert len(storage.files) == 1


class SignedBody(BasePermission):
    """Reads the whole body before the route, as ``DeviceSigned`` does."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """``X-Body-Sha`` is the body's SHA-256."""
        body = await request.body()  # type: ignore[attr-defined]
        return request.headers.get("x-body-sha") == hashlib.sha256(body).hexdigest()


def test_a_rule_that_reads_the_body_first_leaves_it_whole(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A phone signs the bytes it sends; the route reads them after the rule."""
    use_auth()
    run = Run()
    storage = MemoryStorage()
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=run.attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=10_000,
        storage=storage,
        path="/manual",
        rule=SignedBody(),
    )
    client = client_of(resource)
    act = f"{LOCATION}{ids(store)['a']}/manual"
    signed = hashlib.sha256(PDF_FILE).hexdigest()

    refused = client.post(act, content=PDF_FILE, headers=as_("changer"))
    done = client.post(
        act, content=PDF_FILE, headers=as_("changer", **{"X-Body-Sha": signed})
    )

    assert refused.status_code == 403
    assert done.status_code == 200
    assert list(storage.files.values()) == [PDF_FILE]


def test_an_act_s_twin_takes_the_file_and_stores_nothing(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``POST .../manual/authorization``: the upload unnamed, its digest bound."""
    use_auth()
    run = Run()
    asks = Asks()
    storage = MemoryStorage()
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=run.attach,
        field="manual",
        digest="manual_sha256",
        types=[CHECKED_PDF],
        max_size=10_000,
        storage=storage,
        path="/manual",
        rule=asks,
    )
    client = client_of(resource)
    gadget = ids(store)["a"]
    act = f"{LOCATION}{gadget}/manual"
    named = {"X-Filename": "manual.pdf"}

    asked = client.post(
        f"{act}/authorization", content=PDF_FILE, headers=as_("changer", **named)
    )
    unsafe = client.post(
        f"{act}/authorization", content=SCRIPTED, headers=as_("changer")
    )
    stored_nothing = dict(storage.files)
    done = client.post(
        act, content=PDF_FILE, headers=as_("changer", **named, **AUTHORIZED)
    )

    assert asked.status_code == 201
    assert (unsafe.status_code, unsafe.json()["code"]) == (422, "unsafe_file")
    assert stored_nothing == {}
    digest = hashlib.sha256(PDF_FILE).hexdigest()
    unstored = StoredUpload("", "application/pdf", len(PDF_FILE), "manual.pdf")
    assert asks.asked == [
        AttachManual(gadget_id=gadget, manual=unstored, manual_sha256=digest)
    ]
    assert done.status_code == 200
    (command,) = run.given
    assert isinstance(command, AttachManual)
    assert command.manual_sha256 == digest
    assert command.manual.name in storage.files


def test_openapi_shows_the_file_as_the_body_of_the_route_and_its_twin(
    store: InMemoryGadgets,
) -> None:
    """Raw bytes or a form; ``X-Filename``; the refusals of a file."""
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=10_000,
        path="/manual",
        rule=Asks(),
    )
    paths = app_of(resource).openapi()["paths"]

    for path in (
        "/gadgets/{gadget_id}/manual",
        "/gadgets/{gadget_id}/manual/authorization",
    ):
        operation = paths[path]["post"]
        content = operation["requestBody"]["content"]
        assert set(content) == {"application/octet-stream", "multipart/form-data"}
        assert content["multipart/form-data"]["schema"]["required"] == ["manual"]
        headers = [each["name"] for each in operation["parameters"]]
        assert "X-Filename" in headers
        assert {"413", "415", "422"} <= set(operation["responses"])
    names = [route.name for route in resource.routes if isinstance(route, APIRoute)]
    assert names[-2:] == ["attach_manual", "attach_manual_authorization"]


def test_an_upload_route_is_declared_with_what_it_needs(store: InMemoryGadgets) -> None:
    """A field the command has, given by the file alone; types; a size."""
    resource = gadgets(store)
    options: dict[str, Any] = {
        "service": Run().attach,
        "types": [CHECKED_PDF],
        "max_size": 100,
        "path": "/manual",
    }

    with pytest.raises(TypeError, match="AttachManual has no field nothing"):
        resource.upload_route(AttachManual, field="nothing", **options)
    with pytest.raises(TypeError, match="come from the uploaded file"):
        resource.upload_route(
            AttachManual, field="manual", provide={"manual": lambda: None}, **options
        )
    with pytest.raises(ValueError, match="needs the types"):
        resource.upload_route(AttachManual, field="manual", **{**options, "types": []})
    with pytest.raises(ValueError, match="max_size is 1 byte or more"):
        resource.upload_route(
            AttachManual, field="manual", **{**options, "max_size": 0}
        )


class Unforgetting(MemoryStorage):
    """A storage whose deletions fail, or hang."""

    def __init__(self, hang: bool) -> None:
        """Fail each deletion, or hang in it."""
        super().__init__()
        self.hang = hang

    async def _delete(self, name: str) -> bool:
        """Refuse, or wait for ever."""
        if self.hang:
            await anyio.sleep_forever()
        raise OSError("The store is away.")


def test_a_file_is_kept_once_the_unit_of_work_committed(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A row may name the file once a commit kept it, whatever fails after.

    A service that commits and then raises keeps its file; one that fails
    before any commit, even after returning, has its file deleted.
    """
    use_auth()
    storage = MemoryStorage()
    named: list[str] = []

    async def attach_then_fail(
        command: AttachManual, uow: FakeGadgetsUnitOfWork
    ) -> Gadget:
        """Name the gadget after its file, commit, then fail."""
        async with uow:
            gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
            gadget.rename(command.manual.filename)
            await uow.commit()
        named.append(command.manual.name)
        raise RuntimeError("The notice after the commit failed.")

    async def nothing(command: AttachManual, uow: FakeGadgetsUnitOfWork) -> None:
        """Return nothing, where the route answers the aggregate; commit nothing."""

    gadget = ids(store)["a"]
    for service in (attach_then_fail, nothing):
        resource = gadgets(store)
        resource.upload_route(
            AttachManual,
            service=service,
            field="manual",
            types=[CHECKED_PDF],
            max_size=10_000,
            storage=storage,
            path="/manual",
            name=service.__name__,
        )
        with pytest.raises(RuntimeError):
            client_of(resource).post(
                f"{LOCATION}{gadget}/manual",
                content=PDF_FILE,
                headers=as_("changer", **{"X-Filename": "kept.pdf"}),
            )

    (name,) = named
    assert list(storage.files) == [name]
    assert "kept.pdf" in {gadget.name for gadget in store.items.values()}


class Counting(MemoryStorage):
    """Counts its saves."""

    def __init__(self) -> None:
        """Nothing saved yet."""
        super().__init__()
        self.saved = 0

    async def save(self, *args: Any, **options: Any) -> str:  # noqa: ANN401 - Storage's.
        """Count, then save."""
        self.saved += 1
        return await super().save(*args, **options)


class Only(BasePermission):
    """Looks at objects: allows gadget ``a`` only."""

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Gadget ``a``."""
        return getattr(obj, "name", "") == "a"


@pytest.mark.parametrize("require_if_match", [False, True])
def test_the_reach_is_checked_before_a_byte_of_the_file_is_read(
    use_auth: UseAuth, require_if_match: bool
) -> None:
    """Out of reach (404), refused by a rule on it (403), If-Match (412, 428).

    The act and its twin refuse before they receive, check, scan or store
    the file (A6 review).
    """
    use_auth()
    read: list[int] = []

    def counted(file: IO[bytes]) -> None:
        read.append(len(file.read()))

    pdf = FileType("application/pdf", ".pdf", CHECKED_PDF.matches, counted)
    storage = Counting()
    store = InMemoryGadgets(
        [
            make_gadget(name, price=price)
            for name, price in (("a", "1"), ("b", "9"), ("c", "2"))
        ]
    )
    resource = gadgets(store, scope=cheap_only, require_if_match=require_if_match)
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        types=[pdf],
        max_size=10_000,
        storage=storage,
        path="/manual",
        rule=Asks() & Only(),
    )
    client = client_of(resource)
    found = {gadget.name: gadget for gadget in store.items.values()}

    def sent(name: str, suffix: str = "", **headers: str) -> int:
        answer = client.post(
            f"{LOCATION}{found[name].id}/manual{suffix}",
            content=PDF_FILE,
            headers=as_("changer", **AUTHORIZED, **headers),
        )
        return answer.status_code

    refused = [
        sent("b"),
        sent("b", "/authorization"),
        sent("c"),
        sent("c", "/authorization"),
        sent("a", **{"If-Match": '"99"'}),
    ]
    if require_if_match:
        refused.append(sent("a"))

    assert refused == [404, 404, 403, 403, 412, *([428] if require_if_match else [])]
    assert read == []
    assert storage.saved == 0
    version = f'"{found["a"].version}"'
    assert sent("a", **{"If-Match": version}) == 200
    assert read == [len(PDF_FILE)]
    assert storage.saved == 1


async def cheap_only(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
    """Gadgets under 5.00."""
    return F("price") < Decimal("5.00")


@pytest.mark.parametrize("hang", [False, True])
def test_a_file_that_cannot_be_deleted_is_logged_and_the_refusal_kept(
    store: InMemoryGadgets,
    use_auth: UseAuth,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    hang: bool,
) -> None:
    """The client gets the request's own refusal; the log says the file stayed."""
    use_auth()
    monkeypatch.setattr(uploading, "FORGET_WITHIN", 0.05)
    run = Run()
    run.fail = Conflict("Not now.")
    storage = Unforgetting(hang)
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=run.attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=10_000,
        storage=storage,
        path="/manual",
    )
    client = client_of(resource)

    with caplog.at_level(logging.WARNING, logger=uploading.__name__):
        answer = client.post(
            f"{LOCATION}{ids(store)['a']}/manual",
            content=PDF_FILE,
            headers=as_("changer"),
        )

    assert answer.status_code == 409
    assert len(storage.files) == 1
    expected = "took too long" if hang else "could not be deleted"
    assert expected in caplog.text

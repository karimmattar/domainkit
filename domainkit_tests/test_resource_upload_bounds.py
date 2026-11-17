"""An upload route decides its bounds per request, and checks a file once (U3, U5).

``upload_route`` fixed its types, size and storage when it was declared,
and took no ``dependencies=``; and an act's twin checked the file, then
the act checked the same file again, each in a process of its own. The
bounds may now be callables of the request and the aggregate reached,
``dependencies=`` are the act's alone, and the act takes the verdict its
twin's check gave the same caller on the same content (ADR 0175).
"""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import IO, Any, Final
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from domainkit import signing
from domainkit.cache import Cache, CacheUnavailable, caches
from domainkit.contrib.auth import AnyUser
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import uploads
from domainkit.http.errors import install_exception_handlers
from domainkit.http.middleware import RequestBodyLimitMiddleware
from domainkit.http.uploads import CHECKED_PDF, IMAGES, PDF, FileType, receive_upload
from domainkit.inspection import PdfCheck, run_checks
from domainkit.resources import ResourceRouter, asking_authorization
from domainkit.storage import MemoryStorage
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, make_gadget
from domainkit_tests.support.documents import one_page
from domainkit_tests.test_resource_authorizations import AUTHORIZED, Asks
from domainkit_tests.test_resource_uploads import (
    PDF_FILE,
    PNG_FILE,
    SCRIPTED,
    AddManual,
    AttachManual,
    Run,
    ids,
)
from domainkit_tests.test_resources import LOCATION, Users, as_, everyone, gadgets

pytestmark = pytest.mark.anyio

OTHER: Final = one_page("/Lang (ar)")
SHORT_PDF: Final = FileType(
    "application/pdf", ".pdf", PDF.matches, PdfCheck(max_pages=1)
)


class Counted:
    """``run_checks``, counting the files it is handed, by their SHA-256."""

    def __init__(self) -> None:
        """Nothing checked yet."""
        self.files: list[str] = []

    async def __call__(self, checks: Any, file: IO[bytes], **limits: Any) -> int:  # noqa: ANN401 - run_checks's own.
        """Count, then check."""
        file.seek(0)
        self.files.append(hashlib.sha256(file.read()).hexdigest())
        return await run_checks(checks, file, **limits)


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> Counted:
    """The checks uploads run, counted."""
    found = Counted()
    monkeypatch.setattr(uploads, "run_checks", found)
    return found


def sha(data: bytes) -> str:
    """``data``'s SHA-256, in hex."""
    return hashlib.sha256(data).hexdigest()


class App:
    """A resource's application, its users known by name."""

    def __init__(self, resource: ResourceRouter) -> None:
        """Serve ``resource`` to the users ``everyone`` names."""
        self.users: dict[str, AnyUser] = everyone()
        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.include_router(resource)
        self.app.add_middleware(RequestBodyLimitMiddleware)
        self.app.add_middleware(Users, users=self.users)
        self.client = TestClient(self.app)


def asking(store: InMemoryGadgets, rule: Asks, **options: Any) -> App:  # noqa: ANN401 - upload_route's.
    """The gadgets' manual route, which asks for an authorization of its act."""
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        path="/manual",
        rule=rule,
        **{
            "types": [CHECKED_PDF],
            "max_size": 10_000,
            "storage": MemoryStorage(),
            **options,
        },
    )
    return App(resource)


# Checked once ------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryGadgets:
    """Two gadgets: a (cheap), b (dear)."""
    return InMemoryGadgets(
        [make_gadget("a", price="1.00"), make_gadget("b", price="9.00")]
    )


def test_the_act_does_not_check_again_what_its_twin_checked(
    store: InMemoryGadgets, use_auth: UseAuth, counted: Counted
) -> None:
    """The same content, for the same caller: the twin's verdict is the act's.

    Another file, another caller, or a file no twin checked is checked.
    """
    use_auth()
    storage = MemoryStorage()
    app = asking(store, Asks(), storage=storage)
    act = f"{LOCATION}{ids(store)['a']}/manual"

    def sent(user: str, data: bytes, path: str = "", **headers: str) -> int:
        answer = app.client.post(
            f"{act}{path}", content=data, headers=as_(user, **headers)
        )
        return answer.status_code

    assert sent("changer", PDF_FILE, "/authorization") == 201
    assert sent("changer", PDF_FILE, **AUTHORIZED) == 200
    assert counted.files == [sha(PDF_FILE)]
    assert list(storage.files.values()) == [PDF_FILE]
    assert sent("changer", OTHER, **AUTHORIZED) == 200
    assert sent("stocker", PDF_FILE, "/authorization") == 201
    assert sent("changer", SCRIPTED, **AUTHORIZED) == 422
    assert counted.files == [sha(PDF_FILE), sha(OTHER), sha(PDF_FILE), sha(SCRIPTED)]


def test_a_refused_file_is_checked_each_time(
    store: InMemoryGadgets, use_auth: UseAuth, counted: Counted
) -> None:
    """Only what the checks took is remembered."""
    use_auth()
    app = asking(store, Asks())
    act = f"{LOCATION}{ids(store)['a']}/manual"

    twin = app.client.post(
        f"{act}/authorization", content=SCRIPTED, headers=as_("changer")
    )
    done = app.client.post(act, content=SCRIPTED, headers=as_("changer", **AUTHORIZED))

    assert (twin.status_code, done.status_code) == (422, 422)
    assert counted.files == [sha(SCRIPTED)] * 2


def test_a_route_that_asks_nothing_checks_every_file(
    store: InMemoryGadgets, use_auth: UseAuth, counted: Counted
) -> None:
    """No twin, nothing remembered: each request's file is checked."""
    use_auth()
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=10_000,
        storage=MemoryStorage(),
        path="/manual",
    )
    client = App(resource).client
    act = f"{LOCATION}{ids(store)['a']}/manual"

    for _ in range(2):
        assert client.post(act, content=PDF_FILE, headers=as_("changer")).is_success

    assert counted.files == [sha(PDF_FILE)] * 2


async def test_a_verdict_is_taken_only_as_it_was_kept() -> None:
    """Signed for its own key, a candidate's index; anything else is checked."""
    candidates = [CHECKED_PDF]
    key = uploads._verdict_key("route\x1fcaller", sha(PDF_FILE), candidates)
    other = uploads._verdict_key("route\x1fanother", sha(PDF_FILE), candidates)
    signer = signing.Signer(salt=uploads.CHECKED_SALT)
    cache = caches["default"]

    assert await uploads._remembered(key) is None
    await uploads._remember(key, 0)
    assert await uploads._remembered(key) == 0
    for forged in (
        "0",
        7,
        signer.sign(f"{other}:0"),
        signer.sign(f"{key}:x"),
        signing.Signer(salt="elsewhere").sign(f"{key}:0"),
    ):
        await cache.set(key, forged)
        assert await uploads._remembered(key) is None


def test_a_verdict_is_kept_for_its_caller_content_and_checks() -> None:
    """Each changes where it is kept; and so does domainkit's version."""
    key = uploads._verdict_key("route\x1fcaller", sha(PDF_FILE), [CHECKED_PDF])

    assert key.startswith(uploads.REMEMBERED)
    assert key != uploads._verdict_key("route\x1fother", sha(PDF_FILE), [CHECKED_PDF])
    assert key != uploads._verdict_key("route\x1fcaller", sha(OTHER), [CHECKED_PDF])
    assert key != uploads._verdict_key("route\x1fcaller", sha(PDF_FILE), [SHORT_PDF])
    assert key == uploads._verdict_key("route\x1fcaller", sha(PDF_FILE), [CHECKED_PDF])


def test_a_cache_that_is_down_has_the_file_checked(
    store: InMemoryGadgets,
    use_auth: UseAuth,
    counted: Counted,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing kept, nothing taken, nothing refused for it."""
    use_auth()

    async def down(*args: Any, **options: Any) -> None:  # noqa: ANN401 - the cache's.
        raise CacheUnavailable("away")

    monkeypatch.setattr(Cache, "get", down)
    monkeypatch.setattr(Cache, "set", down)
    app = asking(store, Asks())
    act = f"{LOCATION}{ids(store)['a']}/manual"

    twin = app.client.post(
        f"{act}/authorization", content=PDF_FILE, headers=as_("changer")
    )
    done = app.client.post(act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED))

    assert (twin.status_code, done.status_code) == (201, 200)
    assert counted.files == [sha(PDF_FILE)] * 2


def test_a_verdict_lasts_as_long_as_an_authorization_may() -> None:
    """``AUTHORIZATIONS.TTL`` is at most 30 minutes."""
    from domainkit.contrib.auth.settings import MAX_AUTHORIZATION_TTL

    assert uploads.CHECKED_FOR == MAX_AUTHORIZATION_TTL == timedelta(minutes=30)


# Decided per request -----------------------------------------------------------------


def test_types_size_and_storage_are_decided_per_request(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """By the aggregate reached: a PDF for gadget a, an image for b; sync or async."""
    use_auth()
    cheap, dear = MemoryStorage(), MemoryStorage()
    seen: list[tuple[str, str | None]] = []

    def types(request: Request, gadget: Any) -> list[FileType]:  # noqa: ANN401 - the aggregate.
        seen.append(("types", gadget.name))
        return [CHECKED_PDF] if gadget.name == "a" else list(IMAGES)

    async def size(request: Request, gadget: Any) -> int:  # noqa: ANN401 - the aggregate.
        return 5_000 if gadget.name == "a" else 200

    def storage(request: Request, gadget: Any) -> MemoryStorage:  # noqa: ANN401
        return cheap if gadget.name == "a" else dear

    async def directory(request: Request, gadget: Any) -> str:  # noqa: ANN401
        return f"manuals/{gadget.name}"

    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        types=types,
        max_size=size,
        storage=storage,
        directory=directory,
        body_limit=100_000,
        path="/manual",
    )
    client = App(resource).client
    found = ids(store)

    def sent(name: str, data: bytes) -> tuple[int, str | None]:
        answer = client.post(
            f"{LOCATION}{found[name]}/manual", content=data, headers=as_("changer")
        )
        return answer.status_code, answer.json().get("code")

    assert sent("a", PNG_FILE) == (415, "unsupported_file_type")
    assert sent("b", PDF_FILE) == (415, "unsupported_file_type")
    assert sent("b", PNG_FILE + b"\0" * 200) == (413, "file_too_large")
    assert sent("a", PDF_FILE)[0] == 200
    assert sent("b", PNG_FILE)[0] == 200
    assert [name.split("/")[:2] for name in cheap.files] == [["manuals", "a"]]
    assert [name.split("/")[:2] for name in dear.files] == [["manuals", "b"]]
    assert seen == [("types", name) for name in "abbab"]


def test_bounds_are_decided_once_the_reach_holds_on_the_twin_too(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Out of reach: nothing decided. The twin decides as the act does."""
    use_auth()
    seen: list[str] = []

    def types(request: Request, gadget: Any) -> list[FileType]:  # noqa: ANN401 - the aggregate.
        seen.append(f"{gadget.name}{'?' if asking_authorization(request) else ''}")
        return [CHECKED_PDF]

    app = asking(store, Asks(), types=types)
    found = ids(store)
    missing = UUID(int=1)
    act = f"{LOCATION}{found['a']}/manual"

    assert (
        app.client.post(
            f"{LOCATION}{missing}/manual/authorization",
            content=PDF_FILE,
            headers=as_("changer"),
        ).status_code
        == 404
    )
    assert (
        app.client.post(
            f"{act}/authorization", content=PDF_FILE, headers=as_("changer")
        ).status_code
        == 201
    )
    assert (
        app.client.post(
            act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED)
        ).status_code
        == 200
    )
    assert seen == ["a?", "a"]


def test_a_collection_route_decides_without_an_aggregate(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``None`` stands for the aggregate where the route reaches none."""
    use_auth()
    given: list[object] = []

    def types(request: Request, aggregate: object) -> tuple[FileType, ...]:
        given.append(aggregate)
        return IMAGES

    resource = gadgets(store)
    resource.upload_route(
        AddManual,
        service=Run().add,
        field="manual",
        types=types,
        max_size=1000,
        storage=MemoryStorage(),
    )

    made = App(resource).client.post("/gadgets", content=PNG_FILE, headers=as_("adder"))

    assert made.status_code == 201
    assert given == [None]


def test_a_size_decided_per_request_needs_a_body_limit(store: InMemoryGadgets) -> None:
    """Without one, nothing bounds the body before the size is decided."""
    resource = gadgets(store)

    with pytest.raises(TypeError, match="give body_limit="):
        resource.upload_route(
            AttachManual,
            service=Run().attach,
            field="manual",
            types=[CHECKED_PDF],
            max_size=lambda request, gadget: 100,
            path="/manual",
        )
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        types=[CHECKED_PDF],
        max_size=lambda request, gadget: 100,
        body_limit=1000,
        path="/manual",
    )
    paths = App(resource).app.openapi()["paths"]
    body = paths["/gadgets/{gadget_id}/manual"]["post"]["requestBody"]
    assert "the size decided per request" in body["description"]


@pytest.mark.parametrize(
    ("option", "decided", "error"),
    [
        ("types", [], ValueError),
        ("max_size", 0, ValueError),
        ("max_size", True, ValueError),
        ("max_size", 2.5, ValueError),
        ("max_size", float("inf"), ValueError),
        ("storage", 7, TypeError),
        ("storage", "nowhere", ImproperlyConfigured),
        ("directory", None, TypeError),
        ("directory", "../up", ValueError),
        ("directory", "/root", ValueError),
        ("directory", "a//b", ValueError),
        ("directory", ".", ValueError),
    ],
)
def test_what_a_callable_decides_is_checked(
    store: InMemoryGadgets,
    use_auth: UseAuth,
    counted: Counted,
    option: str,
    decided: object,
    error: type[Exception],
) -> None:
    """A mistake of the route's own: a 500, before the body is read.

    An alias ``STORAGES`` lacks, and a directory a storage refuses, are
    found before the file is received or checked: no temporary file is
    left open.
    """
    use_auth()
    resource = gadgets(store)
    options: dict[str, Any] = {
        "types": [CHECKED_PDF],
        "max_size": 1000,
        "storage": MemoryStorage(),
        "body_limit": 10_000,
        option: lambda request, gadget: decided,
    }
    resource.upload_route(
        AttachManual, service=Run().attach, field="manual", path="/manual", **options
    )

    with pytest.raises(error):
        App(resource).client.post(
            f"{LOCATION}{ids(store)['a']}/manual",
            content=PDF_FILE,
            headers=as_("changer"),
        )
    assert counted.files == []


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"directory": "../up"}, ValueError),
        ({"storage": "nowhere"}, ImproperlyConfigured),
    ],
)
def test_a_declared_storage_and_directory_are_checked_before_the_body(
    store: InMemoryGadgets,
    use_auth: UseAuth,
    counted: Counted,
    options: dict[str, str],
    error: type[Exception],
) -> None:
    """As a decided one is: they refused only once the file was checked."""
    use_auth()
    resource = gadgets(store)
    declared: dict[str, Any] = {"types": [CHECKED_PDF], "max_size": 1000, **options}
    resource.upload_route(
        AttachManual, service=Run().attach, field="manual", path="/manual", **declared
    )

    with pytest.raises(error):
        App(resource).client.post(
            f"{LOCATION}{ids(store)['a']}/manual",
            content=PDF_FILE,
            headers=as_("changer"),
        )
    assert counted.files == []


def test_a_size_given_when_declaring_is_a_whole_number(
    store: InMemoryGadgets,
) -> None:
    """A float, infinity included, is refused as a decided one is."""
    resource = gadgets(store)

    for size in (2.5, float("inf"), True):
        with pytest.raises(ValueError, match="max_size is 1 byte or more"):
            resource.upload_route(
                AttachManual,
                service=Run().attach,
                field="manual",
                types=[CHECKED_PDF],
                max_size=size,  # type: ignore[arg-type]
                path="/manual",
            )


async def test_storing_to_an_alias_storages_lacks_drops_the_temporary_file() -> None:
    """The alias is looked up where the file is dropped whatever happens."""
    received = await receive_upload(PNG_FILE, types=IMAGES, max_size=1000)

    with pytest.raises(ImproperlyConfigured, match="nowhere"):
        await received.store("nowhere")
    assert received.file.closed


# Dependencies ------------------------------------------------------------------------


def test_dependencies_are_the_acts_and_not_the_twins(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A limit counts acts: the twin's asks run none of them."""
    use_auth()
    counted: list[str] = []

    async def limit(request: Request) -> None:
        counted.append(request.method)

    dependency: Callable[[Request], Awaitable[None]] = limit
    app = asking(store, Asks(), dependencies=[Depends(dependency)])
    act = f"{LOCATION}{ids(store)['a']}/manual"

    twin = app.client.post(
        f"{act}/authorization", content=PDF_FILE, headers=as_("changer")
    )
    done = app.client.post(act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED))
    refused = app.client.post(
        act, content=PDF_FILE, headers=as_("viewer", **AUTHORIZED)
    )

    assert (twin.status_code, done.status_code, refused.status_code) == (201, 200, 403)
    assert counted == ["POST"]

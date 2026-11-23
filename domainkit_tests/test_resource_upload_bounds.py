"""An upload route decides its bounds per request, and checks a file once (U3, U5, U7).

``upload_route`` fixed its types, size and storage when it was declared,
and took no ``dependencies=``; and an act's twin checked the file, then
the act checked the same file again, each in a process of its own. The
bounds may now be callables of the request and the aggregate reached,
``dependencies=`` are the act's alone, and the act takes the verdict its
twin's check gave the same caller on the same content (ADR 0175).

2.7 decided the body limit when the route was declared only, ran every
dependency of a route after its rules, and gave its service the stored
file only. The body limit may now be decided per request too,
``before_rules`` run before the rules, and a service that asks is given
the file received (ADR 0181).
"""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import timedelta
from typing import IO, Any, Final
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from domainkit import signing
from domainkit.cache import Cache, CacheUnavailable, caches
from domainkit.conf import override_settings
from domainkit.contrib.auth import AnyUser
from domainkit.contrib.devices import DeviceSigned
from domainkit.ddd.exceptions import ConcurrentUpdate, PermissionDenied
from domainkit.ddd.uow import Retry
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import uploads
from domainkit.http.access import AllowAny, BasePermission
from domainkit.http.errors import APIError, install_exception_handlers
from domainkit.http.middleware import (
    MAX_ROUTE_BODY_SIZE,
    RequestBodyLimitMiddleware,
    hold_body,
)
from domainkit.http.uploads import (
    CHECKED_PDF,
    IMAGES,
    PDF,
    FileType,
    ReceivedUpload,
    receive_upload,
)
from domainkit.inspection import PdfCheck, run_checks
from domainkit.resources import BeforeRules, ResourceRouter, asking_authorization
from domainkit.resources.routing import uploading
from domainkit.storage import MemoryStorage
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    InMemoryGadgets,
    make_gadget,
)
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
from domainkit_tests.test_resources import (
    LOCATION,
    MakeGadget,
    MakeIn,
    Users,
    as_,
    everyone,
    gadgets,
    make,
)

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


def asking(store: InMemoryGadgets, rule: BasePermission, **options: Any) -> App:  # noqa: ANN401 - upload_route's.
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


# A body limit decided per request (U7) -----------------------------------------------


class Limits:
    """A body limit decided per gadget, as a project's setting would be, recorded."""

    def __init__(self, **by_name: int) -> None:
        """The limit of each gadget, by its name."""
        self.by_name = by_name
        self.decided: list[str] = []

    async def __call__(self, request: Request, gadget: Any) -> int:  # noqa: ANN401 - the aggregate.
        """The gadget's limit; the twin's ask noted with ``?``."""
        self.decided.append(
            f"{gadget.name}{'?' if asking_authorization(request) else ''}"
        )
        return self.by_name[gadget.name]


def padded(size: int) -> bytes:
    """A PDF of ``size`` bytes: ``PDF`` reads its first bytes only."""
    return PDF_FILE + b"\0" * (size - len(PDF_FILE))


def limited(store: InMemoryGadgets, limits: Limits, **options: Any) -> App:  # noqa: ANN401 - upload_route's.
    """The gadgets' manual route, its body limit decided per request by ``limits``."""
    resource = gadgets(store, body_limits=options.pop("body_limits", None))
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        path="/manual",
        **{
            "types": [PDF],
            "max_size": 1_000_000,
            "storage": MemoryStorage(),
            "body_limit": limits,
            **options,
        },
    )
    return App(resource)


def chunked(data: bytes) -> Iterator[bytes]:
    """``data`` in parts of 1000 bytes, sent without ``Content-Length``."""
    for start in range(0, len(data), 1000):
        yield data[start : start + 1000]


def test_a_body_limit_is_decided_per_request(
    store: InMemoryGadgets, use_auth: UseAuth, counted: Counted
) -> None:
    """Over it, declared or streamed: 413 before the file is checked or stored.

    Gadget a takes 3,000 bytes, b 400; the route stores files up to 1 MB.
    """
    use_auth()
    limits = Limits(a=3_000, b=400)
    app = limited(store, limits, types=[CHECKED_PDF])
    found = ids(store)

    def sent(name: str, body: bytes | Iterator[bytes]) -> tuple[int, str | None]:
        answer = app.client.post(
            f"{LOCATION}{found[name]}/manual", content=body, headers=as_("changer")
        )
        return answer.status_code, answer.json().get("code")

    assert sent("b", padded(3_000)) == (413, "content_too_large")
    assert sent("b", chunked(padded(3_000))) == (413, "content_too_large")
    assert sent("a", padded(3_001)) == (413, "content_too_large")
    assert counted.files == []
    assert sent("b", PDF_FILE)[0] == 200
    assert sent("a", padded(3_000))[0] == 200
    assert limits.decided == ["b", "b", "a", "b", "a"]


def test_a_body_limit_decided_per_request_may_pass_the_global_one(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Before routing, a declared length is held to the most a route may decide.

    2.7 checked it against the largest limit declared: with none declared
    but the global, a body the route's own limit takes was refused before
    the route could decide it.
    """
    use_auth()
    resource = gadgets(store)
    resource.command_route(MakeGadget, service=make, body=MakeIn)
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        path="/manual",
        types=[PDF],
        max_size=1_000_000,
        storage=MemoryStorage(),
        body_limit=Limits(a=50_000),
    )
    with override_settings(MAX_REQUEST_BODY_SIZE=1_000):
        client = App(resource).client
        taken = client.post(
            f"{LOCATION}{ids(store)['a']}/manual",
            content=padded(20_000),
            headers=as_("changer"),
        )
        other = client.post(
            "/gadgets",
            content=b'{"name": "c", "price": "1"}' + b" " * 1_000,
            headers={"Content-Type": "application/json", **as_("adder")},
        )

    assert taken.status_code == 200
    assert (other.status_code, other.json()["code"]) == (413, "content_too_large")


def test_a_body_limit_decided_on_the_twin_too(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The twin decides it once the reach holds, as the act does."""
    use_auth()
    limits = Limits(a=3_000)
    app = asking(store, Asks(), types=[PDF], body_limit=limits)
    act = f"{LOCATION}{ids(store)['a']}/manual"

    refused = app.client.post(
        f"{act}/authorization", content=padded(3_001), headers=as_("changer")
    )
    asked = app.client.post(
        f"{act}/authorization", content=PDF_FILE, headers=as_("changer")
    )
    done = app.client.post(act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED))

    assert (refused.status_code, refused.json()["code"]) == (413, "content_too_large")
    assert (asked.status_code, done.status_code) == (201, 200)
    assert limits.decided == ["a?", "a?", "a"]


class ReadsBody(BasePermission):
    """A rule that reads the body, as ``DeviceSigned`` does, and allows; recorded."""

    def __init__(self, seen: list[str]) -> None:
        """Record in ``seen``."""
        self.seen = seen

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """Read the whole body; allow."""
        assert isinstance(request, Request)
        self.seen.append(f"rule {len(await request.body())}")
        return True


def test_a_body_read_before_the_limit_is_decided_is_held_as_a_commands(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The resource's limit for commands, or the global one; the file, to the decided.

    A rule that reads the body reads it before the route decides: the
    body is held then as a command route's without a limit of its own.
    The file is still never more than the limit decided: 2,000 bytes
    read under the resource's 10,000 are refused for gadget b's 1,000.
    """
    use_auth()
    seen: list[str] = []
    app = limited(
        store,
        Limits(a=50_000, b=1_000),
        rule=ReadsBody(seen),
        body_limits={"command": 10_000},
    )
    found = ids(store)

    def sent(name: str, size: int) -> tuple[int, str | None]:
        answer = app.client.post(
            f"{LOCATION}{found[name]}/manual",
            content=padded(size),
            headers=as_("changer"),
        )
        return answer.status_code, answer.json().get("code")

    assert sent("a", 10_001) == (413, "content_too_large")  # Before the decision.
    assert sent("b", 2_000) == (413, "file_too_large")
    assert sent("a", 2_000)[0] == 200
    assert seen == ["rule 2000", "rule 2000"]


class SaysItReads(ReadsBody):
    """``ReadsBody``, saying so, as ``DeviceSigned`` does."""

    reads_body = True

    def __init__(self, seen: list[str] | None = None) -> None:
        """Record in ``seen``, or nowhere."""
        super().__init__([] if seen is None else seen)


@pytest.mark.parametrize(
    "rule",
    [SaysItReads, SaysItReads() & Asks(), ~SaysItReads(), AllowAny() | SaysItReads()],
    ids=["class", "all of", "not", "any of"],
)
def test_a_rule_that_reads_the_body_is_refused_with_a_limit_decided(
    store: InMemoryGadgets,
    rule: Any,  # noqa: ANN401 - a rule, or its class.
) -> None:
    """It reads the body before the limit is decided: the limit never holds it.

    A number still goes with it, and a limit decided before the rules
    (``BeforeRules``, since 2.10), which the refusal names;
    ``DeviceSigned`` says it reads the body.
    """
    with pytest.raises(
        ImproperlyConfigured,
        match=r"reads the body before it is decided: give body_limit= a BeforeRules",
    ):
        limited(store, Limits(), rule=rule)
    limited(store, Limits(), rule=rule, body_limit=10_000)
    limited(store, Limits(), rule=rule, body_limit=BeforeRules(lambda request: 1))

    assert DeviceSigned.reads_body
    assert DeviceSigned().reads_the_body()
    assert not Asks().reads_the_body()


class Phones:
    """A body limit decided from the request alone, as a project's setting is read.

    Each decision is noted with the twin's ask marked ``?``; ``limit`` may
    change between requests, as a setting does without a restart.
    """

    def __init__(self, limit: int) -> None:
        """Decide ``limit`` until told otherwise."""
        self.limit = limit
        self.decided: list[str] = []

    async def __call__(self, request: Request) -> int:
        """The limit, noted in ``decided``."""
        self.decided.append(f"limit{'?' if asking_authorization(request) else ''}")
        return self.limit


def test_a_body_limit_decided_before_the_rules_holds_a_rule_that_reads_the_body(
    store: InMemoryGadgets, use_auth: UseAuth, counted: Counted
) -> None:
    """``BeforeRules``: decided first, it holds the rule's read too (user report U7).

    A rule that reads the body, as ``DeviceSigned`` does, read it under the
    resource's limit for commands, and the route could take no limit
    decided per request. Decided before the rules, from the request
    alone, the limit is held before a byte is read, the rule's read
    included: over it, 413, the rule having read nothing. It may pass the
    resource's limit, and it changes from one request to the next.
    """
    use_auth()
    seen: list[str] = []
    phones = Phones(3_000)
    app = limited(
        store,
        Limits(),
        types=[CHECKED_PDF],
        rule=SaysItReads(seen),
        body_limit=BeforeRules(phones),
        body_limits={"command": 1_000},
    )
    found = ids(store)

    def sent(body: bytes | Iterator[bytes]) -> tuple[int, str | None]:
        answer = app.client.post(
            f"{LOCATION}{found['a']}/manual", content=body, headers=as_("changer")
        )
        return answer.status_code, answer.json().get("code")

    assert sent(padded(3_001)) == (413, "content_too_large")
    assert sent(chunked(padded(3_001))) == (413, "content_too_large")
    assert (seen, counted.files) == ([], [])
    assert sent(padded(3_000))[0] == 200  # Past the resource's 1,000.
    phones.limit = 2_000
    assert sent(padded(3_000)) == (413, "content_too_large")
    assert seen == ["rule 3000"]
    assert phones.decided == ["limit"] * 4


def test_a_body_limit_decided_before_the_rules_comes_first_on_the_twin_too(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """It runs before ``before_rules`` and the rules, on the act and on its twin.

    The limit holds the rules' reads, and what the resource's own
    dependencies read before it is held to it as a file: a body read
    under the resource's limit for commands is refused past the decided
    one (``file_too_large``).
    """
    use_auth()
    order = Order()
    phones = Phones(3_000)

    async def read_first(request: Request) -> None:
        if request.headers.get("X-Read-First"):
            order.ran.append(f"read {len(await request.body())}")

    resource = gadgets(
        store,
        body_limits={"command": 10_000},
        dependencies=[Depends(read_first)],
    )
    resource.upload_route(
        AttachManual,
        service=Run().attach,
        field="manual",
        path="/manual",
        types=[PDF],
        max_size=1_000_000,
        storage=MemoryStorage(),
        rule=Asks() & ReadsBody(order.ran),
        body_limit=BeforeRules(phones),
        before_rules=[order.dependency("before")],
    )
    client = App(resource).client
    act = f"{LOCATION}{ids(store)['a']}/manual"

    asked = client.post(
        f"{act}/authorization", content=padded(3_000), headers=as_("changer")
    )
    done = client.post(act, content=padded(3_000), headers=as_("changer", **AUTHORIZED))
    refused = client.post(
        f"{act}/authorization", content=padded(3_001), headers=as_("changer")
    )
    read = client.post(
        f"{act}/authorization",
        content=padded(5_000),
        headers=as_("changer", **{"X-Read-First": "1"}),
    )

    assert (asked.status_code, done.status_code) == (201, 200)
    assert (refused.status_code, refused.json()["code"]) == (413, "content_too_large")
    assert (read.status_code, read.json()["code"]) == (413, "file_too_large")
    assert phones.decided == ["limit?", "limit", "limit?", "limit?"]
    assert order.ran == [
        "before?",
        "rule 3000",
        "before",
        "rule 3000",
        "before?",  # Its rule's read refused: nothing read.
        "read 5000",
        "before?",
        "rule 5000",
    ]


@pytest.mark.parametrize("decided", [0, True, 2.5, MAX_ROUTE_BODY_SIZE + 1, "10"])
def test_a_body_limit_decided_before_the_rules_is_checked(
    store: InMemoryGadgets, use_auth: UseAuth, decided: object
) -> None:
    """As one decided once the reach holds: a 500, the body unread by the rule."""
    use_auth()
    seen: list[str] = []
    app = limited(
        store,
        Limits(),
        rule=SaysItReads(seen),
        body_limit=BeforeRules(lambda request: decided),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(ValueError, match="A body limit is 1 to"):
        app.client.post(
            f"{LOCATION}{ids(store)['a']}/manual",
            content=PDF_FILE,
            headers=as_("changer"),
        )
    assert seen == []


def test_before_rules_decide_with_a_callable() -> None:
    """``BeforeRules`` takes a callable of the request: anything else is refused."""
    with pytest.raises(TypeError, match="takes a callable of the request"):
        BeforeRules(10_000)  # type: ignore[arg-type]


def test_a_probe_for_a_disconnect_fixes_no_limit(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``is_disconnected`` before the decision reads nothing, and refuses nothing.

    2.8.0's first cut fixed the limit at the first ``receive()``: a
    dependency that probed for a disconnect held a 50 KB body to the
    resource's 10 KB, under a decided limit of 500 KB.
    """
    use_auth()
    probed: list[bool] = []

    async def probe(request: Request) -> None:
        probed.append(await request.is_disconnected())

    app = limited(
        store,
        Limits(a=500_000, b=40_000),
        body_limits={"command": 10_000},
        before_rules=[Depends(probe)],
    )
    found = ids(store)

    taken = app.client.post(
        f"{LOCATION}{found['a']}/manual", content=padded(50_000), headers=as_("changer")
    )
    refused = app.client.post(
        f"{LOCATION}{found['b']}/manual", content=padded(50_000), headers=as_("changer")
    )

    assert taken.status_code == 200
    assert (refused.status_code, refused.json()["code"]) == (413, "content_too_large")
    assert probed == [False, False]


@pytest.mark.parametrize("decided", [0, True, 2.5, MAX_ROUTE_BODY_SIZE + 1, "10"])
def test_a_body_limit_decided_is_checked(
    store: InMemoryGadgets, use_auth: UseAuth, counted: Counted, decided: object
) -> None:
    """A whole number of bytes up to a route's most: else a 500, the body unread."""
    use_auth()
    app = limited(
        store, Limits(), types=[CHECKED_PDF], body_limit=lambda request, gadget: decided
    )

    with pytest.raises(ValueError, match="A body limit is 1 to"):
        app.client.post(
            f"{LOCATION}{ids(store)['a']}/manual",
            content=PDF_FILE,
            headers=as_("changer"),
        )
    assert counted.files == []


def test_hold_body_takes_a_routes_bounds() -> None:
    """As ``body_limit`` does: 1 byte to 1 GiB."""
    scope: dict[str, Any] = {}

    for size in (0, MAX_ROUTE_BODY_SIZE + 1):
        with pytest.raises(ValueError, match="1 to"):
            hold_body(scope, size)
    hold_body(scope, MAX_ROUTE_BODY_SIZE)

    assert scope == {"domainkit.body_limit": MAX_ROUTE_BODY_SIZE}


# Dependencies before the rules (U7) --------------------------------------------------


class Order:
    """What ran, in order: dependencies, and a rule that reads the body."""

    def __init__(self) -> None:
        """Nothing ran yet."""
        self.ran: list[str] = []
        self.refuse = False

    def dependency(self, name: str) -> Any:  # noqa: ANN401 - FastAPI's Depends.
        """A dependency that records ``name``, and refuses with 429 when told."""

        async def record(request: Request) -> None:
            ask = "?" if asking_authorization(request) else ""
            self.ran.append(f"{name}{ask}")
            if self.refuse and name == "before":
                raise APIError(429, "Slow down.", code="rate_limited")

        return Depends(record)


def test_before_rules_run_before_the_rules_and_on_the_twin(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A limit there counts a request before a rule reads its body.

    Requests the rules refuse are counted too, and the twin's asks: the
    twin runs the rules, so a rule reads its body too. ``dependencies``
    still run after the rules, on the act alone.
    """
    use_auth()
    order = Order()
    reads = ReadsBody(order.ran)
    app = asking(
        store,
        Asks() & reads,
        types=[PDF],
        before_rules=[order.dependency("before")],
        dependencies=[order.dependency("after")],
    )
    act = f"{LOCATION}{ids(store)['a']}/manual"

    twin = app.client.post(
        f"{act}/authorization", content=PDF_FILE, headers=as_("changer")
    )
    done = app.client.post(act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED))
    refused = app.client.post(act, content=PDF_FILE, headers=as_("viewer"))
    order.refuse = True
    limited_ = app.client.post(
        act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED)
    )

    assert (twin.status_code, done.status_code, refused.status_code) == (201, 200, 403)
    assert limited_.status_code == 429
    size = len(PDF_FILE)
    assert order.ran == [
        "before?",
        f"rule {size}",
        "before",
        f"rule {size}",
        "after",
        "before",
        "before",
    ]


def test_a_command_route_takes_before_rules(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """As an upload route does: before its ``Require``, then ``dependencies``."""
    use_auth()
    order = Order()
    resource = gadgets(store)
    resource.command_route(
        MakeGadget,
        service=make,
        body=MakeIn,
        rule=ReadsBody(order.ran),
        before_rules=[order.dependency("before")],
        dependencies=[order.dependency("after")],
    )
    client = App(resource).client
    body = b'{"name": "c", "price": "1"}'
    json = {"Content-Type": "application/json"}

    made = client.post("/gadgets", content=body, headers={**json, **as_("adder")})
    refused = client.post("/gadgets", content=body, headers={**json, **as_("viewer")})

    assert (made.status_code, refused.status_code) == (201, 403)
    assert order.ran == ["before", f"rule {len(body)}", "after", "before"]


# The file received, for the service (U7) ---------------------------------------------


class Reads:
    """A service given the file received: it reads it, as a page count would."""

    def __init__(self) -> None:
        """Nothing read yet; every run succeeds."""
        self.read: list[bytes] = []
        self.files: list[IO[bytes]] = []
        self.fail: Exception | None = None
        self.runs = 0

    async def attach(
        self,
        command: AttachManual,
        uow: FakeGadgetsUnitOfWork,
        *,
        received: ReceivedUpload,
    ) -> Gadget:
        """Read the file; name the gadget after it, as ``Run.attach`` does."""
        assert received.sha256 == command.manual.sha256
        self.files.append(received.file)
        self.read.append(received.file.read())
        self.runs += 1
        if self.fail is not None:
            raise self.fail
        return await Run().attach(command, uow)


def reading(store: InMemoryGadgets, reads: Reads, **options: Any) -> App:  # noqa: ANN401 - upload_route's.
    """The gadgets' manual route, its service given the file received."""
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=reads.attach,
        field="manual",
        path="/manual",
        **{
            "types": [PDF],
            "max_size": 4 * 1024 * 1024,
            "storage": MemoryStorage(),
            **options,
        },
    )
    return App(resource)


def test_a_service_that_asks_is_given_the_file_received(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Open at its start, as stored; closed once the service is done.

    A file large enough to be spooled to disk reads the same.
    """
    use_auth()
    reads = Reads()
    storage = MemoryStorage()
    app = reading(store, reads, storage=storage)
    act = f"{LOCATION}{ids(store)['a']}/manual"
    large = padded(2 * 1024 * 1024)

    small = app.client.post(act, content=PDF_FILE, headers=as_("changer"))
    big = app.client.post(act, content=large, headers=as_("changer"))

    assert (small.status_code, big.status_code) == (200, 200)
    assert reads.read == [PDF_FILE, large]
    assert sorted(storage.files.values(), key=len) == [PDF_FILE, large]
    assert all(each.closed for each in reads.files)


@pytest.mark.parametrize("failure", [PermissionDenied("No."), RuntimeError("A bug.")])
def test_the_file_received_is_closed_however_the_service_ends(
    store: InMemoryGadgets, use_auth: UseAuth, failure: Exception
) -> None:
    """A service that refuses, or fails as a bug does, leaves no file open."""
    use_auth()
    reads = Reads()
    reads.fail = failure
    app = reading(store, reads)
    act = f"{LOCATION}{ids(store)['a']}/manual"

    if isinstance(failure, PermissionDenied):
        answer = app.client.post(act, content=PDF_FILE, headers=as_("changer"))
        assert answer.status_code == 403
    else:
        with pytest.raises(RuntimeError, match="A bug"):
            app.client.post(act, content=PDF_FILE, headers=as_("changer"))

    assert reads.read == [PDF_FILE]
    assert reads.files[0].closed


def test_a_retried_service_reads_the_file_from_its_start(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Each run is given it from its start."""
    use_auth()
    reads = Reads()
    lost = ConcurrentUpdate("Lost a race.")
    runs = 0

    async def loses_once(
        command: AttachManual, uow: FakeGadgetsUnitOfWork, *, received: ReceivedUpload
    ) -> Gadget:
        nonlocal runs
        runs += 1
        reads.fail = lost if runs == 1 else None
        return await reads.attach(command, uow, received=received)

    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=loses_once,
        field="manual",
        path="/manual",
        types=[PDF],
        max_size=10_000,
        storage=MemoryStorage(),
        retry=Retry(attempts=2, backoff=0, max_backoff=0, retry_on=(ConcurrentUpdate,)),
    )
    answer = App(resource).client.post(
        f"{LOCATION}{ids(store)['a']}/manual", content=PDF_FILE, headers=as_("changer")
    )

    assert answer.status_code == 200
    assert reads.read == [PDF_FILE, PDF_FILE]


class Failing(MemoryStorage):
    """A storage that cannot store."""

    async def _write(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        """Fail, as a full disk does."""
        raise OSError("The disk is full.")


def test_a_file_that_cannot_be_stored_is_closed_before_the_service(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The service never runs; the temporary file is dropped."""
    use_auth()
    reads = Reads()
    opened: list[ReceivedUpload] = []
    real = uploads.receive_upload

    async def receive(*args: Any, **options: Any) -> ReceivedUpload:  # noqa: ANN401 - receive_upload's.
        received = await real(*args, **options)
        opened.append(received)
        return received

    app = reading(store, reads, storage=Failing())
    act = f"{LOCATION}{ids(store)['a']}/manual"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(uploading, "receive_upload", receive)
        with pytest.raises(OSError, match="full"):
            app.client.post(act, content=PDF_FILE, headers=as_("changer"))

    assert reads.read == []
    assert [each.file.closed for each in opened] == [True]


def test_the_twin_runs_no_service_and_hands_nothing(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """It stores nothing and runs no service: the file it received is closed."""
    use_auth()
    reads = Reads()
    resource = gadgets(store)
    resource.upload_route(
        AttachManual,
        service=reads.attach,
        field="manual",
        path="/manual",
        rule=Asks(),
        types=[PDF],
        max_size=10_000,
        storage=MemoryStorage(),
    )
    client = App(resource).client
    act = f"{LOCATION}{ids(store)['a']}/manual"

    asked = client.post(
        f"{act}/authorization", content=PDF_FILE, headers=as_("changer")
    )
    done = client.post(act, content=PDF_FILE, headers=as_("changer", **AUTHORIZED))

    assert (asked.status_code, done.status_code) == (201, 200)
    assert reads.read == [PDF_FILE]


async def test_a_file_kept_by_store_stays_open_at_its_start() -> None:
    """``keep=True``: open after storing, until its caller closes it."""
    received = await receive_upload(PNG_FILE, types=IMAGES, max_size=1000)
    storage = MemoryStorage()

    stored = await received.store(storage, keep=True)

    assert not received.file.closed
    assert received.file.read() == PNG_FILE == storage.files[stored.name]
    received.close()
    failing = await receive_upload(PNG_FILE, types=IMAGES, max_size=1000)
    with pytest.raises(ImproperlyConfigured, match="nowhere"):
        await failing.store("nowhere", keep=True)
    assert failing.file.closed


def test_a_service_that_asks_nothing_is_called_as_before() -> None:
    """Only a parameter named ``received``, given by name, asks."""

    async def plain(command: object, uow: object) -> None: ...

    async def positional(command: object, uow: object, received: object, /) -> None: ...

    async def keyword(command: object, uow: object, *, received: object) -> None: ...

    assert not uploading._asks_received(plain)
    assert not uploading._asks_received(positional)
    assert uploading._asks_received(keyword)
    assert not uploading._asks_received(type)  # No signature to read.

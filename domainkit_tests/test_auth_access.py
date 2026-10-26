"""Access rules: each rule, their combinations, and how refusals answer."""

import asyncio
from typing import Any, get_args

import pytest
from starlette.requests import HTTPConnection, Request

from domainkit.contrib import auth as auth_package
from domainkit.contrib.auth import (
    AbstractBaseUser,
    AnonymousUser,
    AnyUser,
    IsAdminUser,
    IsSuperuser,
    PasswordHash,
    User,
)
from domainkit.contrib.auth.models import Permission
from domainkit.contrib.contenttypes import ContentType
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import access
from domainkit.http.access import (
    AllOf,
    AllowAny,
    AnyOf,
    BasePermission,
    HasObjectPermissions,
    HasPermissions,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
    ModelPermissions,
    Not,
    NotAuthenticated,
    Require,
    check_object_permissions,
)
from domainkit_tests.conftest import UseAuth

HASH = PasswordHash("pbkdf2_sha256$1$salt$aGFzaA==")


def request(method: str = "GET", user: AnyUser | None = None) -> Request:
    """A request with ``method``, from ``user`` (None: no middleware ran)."""
    scope: dict[str, Any] = {"type": "http", "method": method, "headers": []}
    if user is not None:
        scope["user"] = user
    return Request(scope)


def user(*keys: str, **fields: Any) -> User:  # noqa: ANN401 - user fields.
    """A user granted ``keys`` (``<domain>.<codename>``)."""
    alice = User.create(password=HASH, **{"username": "alice", **fields})
    for number, key in enumerate(keys, 1):
        domain, codename = key.split(".")
        content_type = ContentType(id=number, domain=domain, model="product")
        alice.grant(
            Permission(
                id=number, codename=codename, name=key, content_type=content_type
            )
        )
    return alice


def allows(rule: BasePermission, who: AnyUser, method: str = "GET") -> bool:
    """Whether ``rule`` allows ``who`` to make a ``method`` request."""
    return asyncio.run(rule.has_permission(request(method, who), who))


def allows_object(rule: BasePermission, who: AnyUser, obj: object) -> bool:
    """Whether ``rule`` allows ``who`` on ``obj``."""
    return asyncio.run(rule.has_object_permission(request("GET", who), who, obj))


class Never(BasePermission):
    """Refuses everything, with its own message."""

    message = "Never."

    async def has_permission(self, request: Any, user: AnyUser) -> bool:  # noqa: ANN401
        """No."""
        return False

    async def has_object_permission(
        self,
        request: Any,  # noqa: ANN401 - any connection.
        user: AnyUser,
        obj: object,
    ) -> bool:
        """No."""
        return False


class RequestOnly(BasePermission):
    """Allows requests, never objects."""

    async def has_object_permission(
        self,
        request: Any,  # noqa: ANN401 - any connection.
        user: AnyUser,
        obj: object,
    ) -> bool:
        """No."""
        return False


class ObjectOnly(BasePermission):
    """Allows objects, never requests."""

    async def has_permission(self, request: Any, user: AnyUser) -> bool:  # noqa: ANN401
        """No."""
        return False


@pytest.fixture(autouse=True)
def auth(use_auth: UseAuth) -> None:
    """Install auth (backends answer ``has_perm``)."""
    use_auth()


def test_simple_rules() -> None:
    """Who each rule lets in."""
    anonymous, alice = AnonymousUser(), user()
    staff, root = user(is_staff=True), user(is_superuser=True)
    idle_root = user(is_superuser=True, is_active=False)

    assert allows(AllowAny(), anonymous)
    assert allows(BasePermission(), anonymous)
    assert allows_object(BasePermission(), anonymous, object())
    assert [allows(IsAuthenticated(), who) for who in (anonymous, alice)] == [
        False,
        True,
    ]
    assert [allows(IsAdminUser(), who) for who in (anonymous, alice, staff)] == [
        False,
        False,
        True,
    ]
    assert [allows(IsSuperuser(), who) for who in (alice, root, idle_root)] == [
        False,
        True,
        False,
    ]
    read_only = IsAuthenticatedOrReadOnly()
    assert [allows(read_only, anonymous, m) for m in ("GET", "HEAD", "OPTIONS")] == [
        True
    ] * 3
    assert not allows(read_only, anonymous, "POST")
    assert allows(read_only, alice, "DELETE")
    assert (
        BasePermission.message == "You do not have permission to perform this action."
    )


def test_permission_rules() -> None:
    """Global permissions, object permissions, and model permissions."""
    alice = user("shop.view_product", "shop.add_product")

    assert allows(HasPermissions("shop.view_product"), alice)
    assert not allows(HasPermissions("shop.view_product", "shop.delete_product"), alice)
    assert allows_object(HasPermissions("shop.view_product"), alice, object())
    with pytest.raises(ImproperlyConfigured) as empty:
        HasPermissions()
    assert str(empty.value) == "HasPermissions needs at least one permission."

    on_object = HasObjectPermissions("shop.view_product")
    assert allows(on_object, user())  # Any user; each object decides.
    assert not allows(on_object, AnonymousUser())
    assert not allows(on_object, user(is_active=False))
    assert not allows_object(on_object, alice, object())  # ModelBackend has none.

    model = ModelPermissions("shop.product")
    assert [allows(model, alice, m) for m in ("GET", "HEAD", "POST")] == [True] * 3
    assert [allows(model, alice, m) for m in ("PUT", "PATCH", "DELETE")] == [False] * 3
    assert allows(model, user(), "OPTIONS")
    assert not allows(model, user(), "TRACE")
    assert not allows(model, AnonymousUser(), "OPTIONS")
    assert allows(model, user(is_superuser=True), "DELETE")


def test_rules_combine() -> None:
    """``&``, ``|``, ``~`` on instances, and with a class on either side."""
    alice, anonymous = user("shop.view_product"), AnonymousUser()
    view = HasPermissions("shop.view_product")

    assert isinstance(IsAuthenticated() & view, AllOf)
    assert isinstance(IsAuthenticated & view, AllOf)
    assert isinstance(view | IsAdminUser, AnyOf)
    assert isinstance(IsAdminUser | view, AnyOf)
    assert isinstance(~view, Not)
    assert allows(IsAuthenticated() & view, alice)
    assert not allows(view & Never(), alice)
    assert allows(Never() | view, alice)
    assert not allows(Never() | Never(), alice)
    assert allows(~IsAuthenticated(), anonymous)
    assert not allows(~IsAuthenticated(), alice)
    assert not allows_object(~BasePermission(), alice, object())
    assert allows_object(~Never(), alice, object())
    # One rule must allow both the request and the object.
    assert not allows_object(RequestOnly() | ObjectOnly(), alice, object())
    assert allows_object(RequestOnly() | BasePermission(), alice, object())
    assert not allows_object(BasePermission() & RequestOnly(), alice, object())
    assert allows_object(AllOf(BasePermission, AllowAny()), alice, object())
    with pytest.raises(ImproperlyConfigured) as wrong:
        Require("shop.view_product")
    assert str(wrong.value) == (
        "'shop.view_product' is not a BasePermission; combine rule instances, as "
        "in IsAdminUser() | IsSuperuser()."
    )


def test_require_answers_401_or_403() -> None:
    """Anonymous callers are asked to log in; users are refused."""
    alice = user()
    guard = Require()
    assert [type(rule) for rule in guard.rules] == [IsAuthenticated]

    with pytest.raises(NotAuthenticated) as anonymous:
        asyncio.run(guard(request(user=AnonymousUser()), None))
    assert anonymous.value.status_code == 401
    assert anonymous.value.headers == {"WWW-Authenticate": 'Bearer realm="api"'}
    assert asyncio.run(guard(request(user=alice), None)) is alice
    with pytest.raises(PermissionDenied) as refused:
        asyncio.run(Require(IsAuthenticated, Never)(request(user=alice), None))
    assert refused.value.message == "Never."


def test_object_checks_answer_401_or_403() -> None:
    """``check_object_permissions`` refuses like ``Require``."""
    alice: AbstractBaseUser = user()
    obj = object()

    asyncio.run(check_object_permissions(request(user=alice), obj, BasePermission))
    with pytest.raises(PermissionDenied) as refused:
        asyncio.run(check_object_permissions(request(user=alice), obj, Never()))
    assert refused.value.message == "Never."
    with pytest.raises(NotAuthenticated):
        asyncio.run(
            check_object_permissions(request(user=AnonymousUser()), obj, Never())
        )
    with pytest.raises(ImproperlyConfigured) as missing:
        asyncio.run(check_object_permissions(request(), obj, AllowAny()))
    assert str(missing.value) == (
        "Add an authentication middleware to MIDDLEWARE to know who sends "
        "requests: domainkit.contrib.auth.middleware.AuthenticationMiddleware, "
        'or one of your own that sets scope["user"].'
    )


class Sees(BasePermission):
    """Allows only the exact request, user, and object it expects."""

    def __init__(self, request: Request, user: AnyUser, obj: object) -> None:
        """Expect these."""
        self.expected = (request, user, obj)

    async def has_permission(self, request: Any, user: AnyUser) -> bool:  # noqa: ANN401
        """The request and the user are the expected ones."""
        return (request, user) == self.expected[:2]

    async def has_object_permission(
        self,
        request: Any,  # noqa: ANN401 - any connection.
        user: AnyUser,
        obj: object,
    ) -> bool:
        """The request, the user, and the object are the expected ones."""
        return (request, user, obj) == self.expected


def test_combinations_pass_everything_on() -> None:
    """Each rule of a combination sees the request, the user, and the object."""
    alice, obj = user(), object()
    seen = request("GET", alice)
    sees = Sees(seen, alice, obj)

    for rule in (AllOf(sees), AnyOf(sees), ~~sees):
        assert asyncio.run(rule.has_permission(seen, alice))
        assert asyncio.run(rule.has_object_permission(seen, alice, obj))
    assert not asyncio.run((~sees).has_object_permission(seen, alice, obj))
    asyncio.run(check_object_permissions(seen, obj, sees))


def test_both_sides_of_an_operator_count() -> None:
    """Neither operand is dropped, whichever side the class is on."""
    alice = user()

    assert not allows(Never() & AllowAny(), alice)
    assert not allows(AllowAny() & Never(), alice)
    assert not allows(Never & AllowAny(), alice)
    assert not allows(AllowAny & Never(), alice)
    assert allows(Never() | AllowAny(), alice)
    assert allows(AllowAny() | Never(), alice)
    # Type checkers read ``Class | instance`` as a union type; at runtime the
    # instance's ``__ror__`` combines them.
    assert allows(Never | AllowAny(), alice)  # type: ignore[arg-type]
    assert allows(AllowAny | Never(), alice)  # type: ignore[arg-type]


def test_users_without_a_staff_flag_are_not_staff() -> None:
    """``AbstractBaseUser`` models have no ``is_staff``: never admins."""
    from domainkit_tests.sample_domains.members.models import Member

    member = Member.create(password=HASH, email="boss@example.org")

    assert not allows(IsAdminUser(), member)
    assert allows(IsAuthenticated(), member)


def test_websocket_handshakes_count_as_reads() -> None:
    """A WebSocket has no method; its handshake is a ``GET``."""
    alice = user("shop.view_product")
    socket = HTTPConnection({"type": "websocket", "headers": []})

    assert asyncio.run(
        IsAuthenticatedOrReadOnly().has_permission(socket, AnonymousUser())
    )
    assert asyncio.run(ModelPermissions("shop.product").has_permission(socket, alice))
    assert not asyncio.run(
        ModelPermissions("shop.product").has_permission(socket, user())
    )


# Auth's names for the core's ------------------------------------------------------


def test_auth_s_typed_views_are_the_core_s_objects() -> None:
    """``current_user`` and ``CurrentUser`` of auth are the core's, typed as users."""
    assert auth_package.current_user is access.current_user
    assert auth_package.acurrent_user is access.acurrent_user
    assert get_args(auth_package.CurrentUser)[1] == get_args(access.CurrentUser)[1]
    assert get_args(auth_package.CurrentUser)[0] == AnyUser

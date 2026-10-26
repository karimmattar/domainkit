"""The principal cache: what it keeps, what it checks, and what drops it."""

import io
import logging
import warnings
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import sqlalchemy
from fakeredis import FakeServer
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import delete, event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import class_mapper
from sqlalchemy.orm.attributes import instance_state
from starlette.requests import HTTPConnection

from domainkit import setup
from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.cache import redis as redis_cache
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth import (
    AbstractBaseUser,
    AuthenticationFailed,
    AuthUnitOfWork,
    Credentials,
    PasswordHash,
    User,
    UserId,
    principals,
)
from domainkit.contrib.auth.authentication import (
    JWTAuthentication,
    get_authenticators,
)
from domainkit.contrib.auth.cookies import (
    cookie_names,
    csrf_token,
)
from domainkit.contrib.auth.management.commands.changepassword import (
    Command as ChangePassword,
)
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.contrib.auth.principals import (
    ALL,
    Entry,
    PrincipalCache,
    principal_cache,
    token_target,
    user_target,
)
from domainkit.contrib.auth.tables import tokens
from domainkit.contrib.auth.tokens import (
    LAST_USED_EVERY,
    AuthToken,
    TokenKind,
    token_digest,
)
from domainkit.contrib.auth.users import get_user_table
from domainkit.db import connections
from domainkit.db.hooks import on_committed, on_stage
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    SHOP,
    Api,
    UseAuth,
)
from domainkit_tests.fake_smtp import certificate
from domainkit_tests.gadgets import FakeGadgetsUnitOfWork, InMemoryGadgets
from domainkit_tests.test_auth import accounts as accounts
from domainkit_tests.test_auth_http import (
    JWT,
    PASSWORD,
    bearer,
    create_user,
    grant,
    login,
)
from domainkit_tests.test_redis import FakeRedis

AUTHENTICATION = "domainkit.contrib.auth"
CERTIFICATES = [
    f"{AUTHENTICATION}.authentication.TokenAuthentication",
    f"{AUTHENTICATION}.cookies.CookieAuthentication",
    f"{AUTHENTICATION}.certificates.ClientCertificateAuthentication",
]
SITE = "https://testserver"
HIDDEN: Final = PasswordHash("pbkdf2_sha256$1$salt$a-hash-no-repr-shows")
NEW_PASSWORD = "Fresh-pa55-word"


@pytest.fixture
def statements() -> Iterator[list[str]]:
    """The SQL statements every engine runs during the test, by first word."""
    seen: list[str] = []

    def count(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy's arguments.
        seen.append(args[2].lstrip().split(None, 1)[0].upper())

    event.listen(Engine, "before_cursor_execute", count)
    yield seen
    event.remove(Engine, "before_cursor_execute", count)


def scope(
    headers: dict[str, str] | None = None,
    *,
    method: str = "GET",
    certificate_pem: str | None = None,
) -> dict[str, Any]:
    """An HTTP scope with ``headers``, and a client certificate if given."""
    built: dict[str, Any] = {
        "type": "http",
        "method": method,
        "scheme": "https",
        "path": "/whoami",
        "root_path": "",
        "query_string": b"",
        "server": ("testserver", 443),
        "client": ("203.0.113.7", 50000),
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
    }
    if certificate_pem is not None:
        tls = {"client_cert_chain": [certificate_pem], "client_cert_error": None}
        built["extensions"] = {"tls": tls}
    return built


async def credentials_of(**options: Any) -> Credentials | None:  # noqa: ANN401
    """What the configured classes make of a request (``scope``'s options)."""
    connection = HTTPConnection(scope(**options))
    for authenticator in get_authenticators():
        found = await authenticator.authenticate(connection)
        if found is not None:
            return found
    return None


async def user_of(**options: Any) -> AbstractBaseUser:  # noqa: ANN401
    """The user the configured classes accept for a request (``scope``'s options)."""
    found = await credentials_of(**options)
    assert found is not None
    return found.user


def columns(instance: Any, *leave: str) -> tuple[tuple[str, Any], ...]:  # noqa: ANN401 - any mapped instance.
    """Every column value of ``instance`` but ``leave``; unloaded ones raise."""
    return tuple(
        (prop.key, str(value) if isinstance(value, UUID) else value)
        for prop in class_mapper(type(instance)).column_attrs
        if prop.key not in leave
        for value in [getattr(instance, prop.key)]
    )


def granted(permission: Any) -> tuple[Any, ...]:  # noqa: ANN401 - a Permission.
    """A permission's columns, with its content type's."""
    return (*columns(permission), columns(permission.content_type))


def view(user: AbstractBaseUser) -> dict[str, Any]:
    """Everything a user built for a request holds but the password, comparable."""
    state = instance_state(user)
    return {
        "type": type(user),
        "columns": dict(columns(user, "password")),
        "groups": sorted(
            (
                columns(group),
                sorted(granted(each) for each in group.permissions),
            )
            for group in user.groups
        ),
        "permissions": sorted(granted(each) for each in user.user_permissions),
        "every": user.get_all_permissions(),
        "detached": state.detached,
        "identity": state.identity_key,
    }


def one_object_each(user: AbstractBaseUser) -> bool:
    """Whether each permission, and content type, is one object however reached.

    As the ORM loads them: a permission given directly and by a group is
    the same object.
    """
    permissions: dict[int, Any] = {}
    content_types: dict[int, Any] = {}
    reached = [
        *user.user_permissions,
        *(each for group in user.groups for each in group.permissions),
    ]
    return all(
        permissions.setdefault(each.id, each) is each
        and content_types.setdefault(each.content_type.id, each.content_type)
        is each.content_type
        for each in reached
    )


def lent_password(user: AbstractBaseUser) -> PasswordHash:
    """The password hash of a request's ``user``, read with the warning it gives."""
    with pytest.warns(DomainkitDeprecationWarning, match="password_holds") as warned:
        hashed = user.password
    assert len(warned) == 1
    # Whoever read it, not the code that lends it, nor SQLAlchemy's.
    assert warned[0].filename != principals.__file__
    assert not warned[0].filename.startswith(str(Path(sqlalchemy.__file__).parent))
    return hashed


async def stored_hash(user_id: UUID) -> PasswordHash | None:
    """The password hash stored for ``user_id``."""
    async with AuthUnitOfWork() as uow:
        return await uow.users.password_hash(user_id)


async def orm_user(user_id: UUID) -> AbstractBaseUser:
    """The user as the ORM loads it, password and all."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
    assert user is not None
    return user


async def editors_with(username: str, *keys: str) -> None:
    """Put ``username`` in a new group ``editors`` that gives ``keys``.

    And in a group that gives nothing; not in ``owners``, which gives
    ``shop.delete_product``.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        editors = await uow.groups.create("editors")
        editors.grant(*await uow.permissions.get_many(keys))
        empty = await uow.groups.create("empty")
        owners = await uow.groups.create("owners")
        owners.grant(await uow.permissions.get("shop.delete_product"))
        user.join(editors)
        user.join(empty)
        await uow.commit()


async def issue(
    username: str,
    *,
    kind: TokenKind = TokenKind.ACCESS,
    ttl: timedelta | None = None,
    scopes: frozenset[str] | None = None,
) -> str:
    """A new stored token of ``username``."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        raw = await uow.tokens.issue(user.id, kind=kind, ttl=ttl, scopes=scopes)
        await uow.commit()
    return raw


# The cache itself.


def entry(key: str, *targets: str, started: int = 0) -> Entry:
    """An entry of ``key`` that rests on ``targets``."""
    return Entry(
        ("token", key), None, targets=targets, started=started, keep=True, deadline=10
    )


def test_entries_are_kept_until_their_deadline_and_the_oldest_go_first() -> None:
    """Bounded in time by each deadline, and in number, least recently used out."""
    cache = PrincipalCache()
    first, second, third = entry("a", "user:1"), entry("b", "user:1"), entry("c")

    assert cache.put(first, limit=2) == []
    assert cache.put(second, limit=2) == []
    assert cache.get(first.key, now=5) is first  # Now the most recently used.
    assert cache.put(third, limit=2) == [second]
    assert (cache.get(second.key, 5), len(cache)) == (None, 2)
    assert cache.get(first.key, now=10) is None  # Past its deadline.
    assert len(cache) == 1
    assert cache.get(("token", "missing"), now=0) is None
    assert not first.fresh  # Kept entries are no longer fresh.
    assert cache.put(third, limit=1) == []  # Again: it replaces itself.
    assert cache._by_target == {}


def test_invalidation_drops_what_rests_on_a_target() -> None:
    """A user's entries go together; ``ALL`` drops everything."""
    cache = PrincipalCache()
    alice, also_alice, bob = (
        entry("a", "user:alice", "token:a"),
        entry("b", "user:alice", "token:b"),
        entry("c", "user:bob", "token:c"),
    )
    for each in (alice, also_alice, bob):
        cache.put(each, limit=10)

    assert cache.invalidate(["token:b", "token:nothing"]) == [also_alice]
    assert set(cache._by_target) == {"user:alice", "token:a", "user:bob", "token:c"}
    assert cache.invalidate(["user:alice"]) == [alice]
    assert cache.invalidate([ALL]) == [bob]
    assert (len(cache), cache._by_target) == (0, {})


def test_what_was_read_while_a_change_committed_is_not_kept() -> None:
    """``begin`` before reading; an invalidation after it refuses the entry."""
    cache = PrincipalCache(recent=2)
    started = cache.begin()
    cache.invalidate(["user:alice"])
    late = cache.begin()

    assert cache.put(entry("a", "user:alice", started=started), limit=9) is None
    assert cache.put(entry("b", "user:bob", started=started), limit=9) == []
    assert cache.put(entry("c", "user:alice", started=late), limit=9) == []
    cache.invalidate([ALL])
    assert cache.put(entry("d", "user:carol", started=late), limit=9) is None
    # Targets forgotten (past ``recent``) still refuse what was read before.
    cache.invalidate(["user:x"])
    cache.invalidate(["user:y"])
    cache.invalidate(["user:z"])
    assert list(cache._invalidated) == ["user:y", "user:z"]
    assert cache.put(entry("e", "user:x", started=late), limit=9) is None
    assert cache.put(entry("f", "user:x", started=cache.begin()), limit=9) == []


def test_only_the_kept_entry_is_discarded() -> None:
    """An entry replaced under its key stays when the old one is discarded."""
    cache = PrincipalCache()
    old, new = entry("a", "user:1"), entry("a", "user:1")
    cache.put(old, limit=9)
    cache.put(new, limit=9)

    assert not cache.discard(old)
    assert cache.discard(new)
    assert not cache.discard(new)
    assert len(cache) == 0


def test_last_use_is_claimed_once_per_period() -> None:
    """The first request that finds it due writes it; the others do not."""
    cache = PrincipalCache()
    with freeze_time("2026-05-01T10:00:00Z") as clock:
        now = clock.now()
        kept = entry("a")
        assert cache.claim_touch(kept, now)
        assert not cache.claim_touch(kept, now + LAST_USED_EVERY / 2)
        assert cache.claim_touch(kept, now + LAST_USED_EVERY)
        assert kept.last_used == now + LAST_USED_EVERY


def test_principals_hide_their_fields(api: Api) -> None:
    """A principal's ``repr`` names its model only: the rest is personal data.

    Nor does a user built from it show the password hash it lends.
    """
    alice = create_user("alice")
    api()
    raw = run_async(issue("alice"))
    credentials = run_async(credentials_of(headers=bearer(raw)))
    kept = principal_cache.get(("token", token_digest(raw)), 0)
    hashed = run_async(stored_hash(alice))
    assert credentials is not None
    assert kept is not None
    assert hashed is not None

    assert repr(kept.principal) == "<Principal of a User>"
    assert "alice" not in repr(kept)
    shown = [
        repr(kept),
        repr(credentials),
        repr(credentials.user),
        repr(instance_state(credentials.user).callables),
    ]
    assert not [each for each in shown if hashed.value in each]
    assert not [each for each in shown if "alice" in each]  # Personal data.
    assert repr(credentials) == (
        f"Credentials(user_id={alice!r}, scheme='token', scopes=None)"
    )


# A kept principal is what a query builds.


def test_tokens_kept_and_read_build_the_same_user(
    api: Api, statements: list[str]
) -> None:
    """A hit builds what a miss builds, which is what the ORM loads, unhashed."""
    alice = create_user("alice", is_staff=True)
    grant("alice", "shop.view_product", "shop.add_product")
    client = api()
    run_async(editors_with("alice", "shop.change_product", "shop.view_product"))
    token = login(client, "alice")["access_token"]
    headers = bearer(token)

    async def scenario() -> tuple[Any, ...]:
        principal_cache.invalidate([ALL])
        statements.clear()
        missed = await credentials_of(headers=headers)
        reads = list(statements)
        statements.clear()
        hit = await credentials_of(headers=headers)
        again = await credentials_of(headers=headers)
        return missed, hit, again, reads, list(statements)

    missed, hit, again, reads, hits = run_async(scenario())
    stored = run_async(orm_user(alice))
    assert reads.count("SELECT") == 2  # The token, then the user with grants.
    assert hits == []  # Hits ask the database nothing.
    assert view(missed.user) == view(hit.user) == view(stored)
    assert one_object_each(stored)
    assert one_object_each(missed.user)
    assert one_object_each(hit.user)
    assert len(hit.user.groups) == 2
    assert hit.user is not again.user  # Each request gets a user of its own.
    assert all(
        mine is not theirs for mine in hit.user.groups for theirs in again.user.groups
    )
    assert (missed.scheme, missed.token_id) == (hit.scheme, hit.token_id)
    assert missed.token_id == token_digest(token)
    assert missed.scopes is hit.scopes is None
    assert lent_password(missed.user) == lent_password(hit.user) == stored.password
    assert missed.user.password is missed.user.password  # Warned once, then kept.
    assert hit.user.has_perm("shop.change_product")
    assert not hit.user.has_perm("shop.delete_product")


def test_request_users_lend_their_password_hash_deprecated(api: Api) -> None:
    """``request.user.password`` works for one more release, and warns.

    Kept or read, it is the stored hash; projects that make the warning an
    error (``filterwarnings``) find each use.
    """
    alice = create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))
    stored = run_async(stored_hash(alice))

    with freeze_time("2026-05-01T10:00:00Z"):
        missed = run_async(user_of(headers=bearer(raw)))
        hit = run_async(user_of(headers=bearer(raw)))
        strict = run_async(user_of(headers=bearer(raw)))
    with pytest.warns(DomainkitDeprecationWarning) as warned:
        assert missed.password == stored
    with warnings.catch_warnings():
        warnings.simplefilter("error", DomainkitDeprecationWarning)
        with pytest.raises(DomainkitDeprecationWarning):
            _ = strict.password

    (message,) = [str(each.message) for each in warned]
    assert "password_holds(user, password)" in message
    assert "uow.users.password_hash(user.id)" in message
    assert "removed in domainkit 0.10" in message
    assert lent_password(hit) == stored


def test_a_user_lent_a_hash_again_reads_the_last_and_keeps_its_loaders(
    use_auth: UseAuth,
) -> None:
    """``lend_password`` on a user lent one already (an ORM-read user read twice)."""
    use_auth()
    first, last = PasswordHash.unusable(), PasswordHash.unusable()
    user = User.create(password=first, username="alice")
    state = instance_state(user)
    principals.lend_password(user, first, usable=False)

    def own_loader(state: object, passive: object) -> str:
        return "its own"

    state.callables["nickname"] = own_loader
    principals.lend_password(user, last, usable=False)

    assert state.callables["nickname"] is own_loader
    assert lent_password(user) == last


@pytest.mark.parametrize("password", [PASSWORD, None])
def test_request_users_know_whether_their_password_is_usable(
    api: Api, password: str | None
) -> None:
    """Kept or read, with no warning and no hash: Django's ``has_usable_password``."""
    create_user("alice", password)
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))

    with freeze_time("2026-05-01T10:00:00Z"):
        missed = run_async(user_of(headers=bearer(raw)))
        hit = run_async(user_of(headers=bearer(raw)))

    usable = password is not None
    assert missed.has_usable_password() is usable  # Warnings are errors here.
    assert hit.has_usable_password() is usable
    assert lent_password(hit).usable is usable
    assert hit.has_usable_password() is usable  # The hash read, now.
    hit.set_unusable_password()
    assert not hit.has_usable_password()


def test_password_hashes_stay_out_of_logs_metrics_and_stamps(
    api: Api, shared: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    """Kept, read, refused, dropped, and stamped: the hash shows nowhere."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    hashed = run_async(stored_hash(alice))
    assert hashed is not None

    async def scenario() -> list[Any]:
        await credentials_of(headers=bearer(raw))
        await credentials_of(headers=bearer(raw))
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke_user(alice)
            await uow.commit()
        with pytest.raises(AuthenticationFailed):
            await credentials_of(headers=bearer(raw))
        client = redis_cache.create_client(None)  # type: ignore[arg-type]
        return [(key, await client.get(key)) for key in await client.keys("*")]

    with caplog.at_level(logging.DEBUG):
        stamps = run_async(scenario())

    assert stamps
    assert hashed.value not in repr(stamps)
    assert hashed.value not in caplog.text
    assert not [
        sample
        for metric in REGISTRY.collect()
        for sample in metric.samples
        if hashed.value in repr(sample)
    ]


def test_personal_tokens_keep_their_scopes(api: Api) -> None:
    """A scoped token's scopes come back the same, from memory."""
    create_user("alice")
    grant("alice", "shop.view_product", "shop.add_product")
    api()
    raw = run_async(issue("alice", scopes=frozenset({"shop.view_product"})))

    async def twice() -> tuple[Credentials | None, Credentials | None]:
        return await credentials_of(headers=bearer(raw)), await credentials_of(
            headers=bearer(raw)
        )

    first, second = run_async(twice())
    assert first is not None
    assert second is not None
    assert first.scopes == second.scopes == frozenset({"shop.view_product"})
    assert view(first.user) == view(second.user)
    assert (principal_cache.get(("token", token_digest(raw)), 0)) is not None


def test_browser_sessions_kept_and_read_build_the_same_user(
    identity: Api, statements: list[str]
) -> None:
    """Session cookies: the same user, and the CSRF checks on every request."""
    create_user("alice", is_superuser=True)
    identity()
    raw = run_async(issue("alice", kind=TokenKind.SESSION, ttl=timedelta(hours=1)))
    access = run_async(issue("alice"))
    session, csrf = cookie_names()
    digest = token_digest(raw)
    token = csrf_token(digest)
    site = {"Host": "testserver", "Origin": SITE}
    cookie = {**site, "Cookie": f"{session}={raw}"}
    unsafe = {**site, "Cookie": f"{session}={raw}; {csrf}={token}"}

    async def scenario() -> tuple[Any, ...]:
        missed = await credentials_of(headers=cookie)
        statements.clear()
        hit = await credentials_of(headers=cookie)
        reads = list(statements)
        with pytest.raises(AuthenticationFailed) as refused:
            await credentials_of(headers=cookie, method="POST")
        posted = await credentials_of(
            headers={**unsafe, "X-CSRF-Token": token}, method="POST"
        )
        with_header = await credentials_of(
            headers={**cookie, "Authorization": "Basic eDp5"}
        )
        without_cookie = await credentials_of(headers=site)
        return missed, hit, reads, refused.value, posted, with_header, without_cookie

    before = {name: sample(name, "cookie") for name in ("hit", "miss")}
    missed, hit, reads, refused, posted, with_header, without_cookie = run_async(
        scenario()
    )
    not_a_session = {**site, "Cookie": f"{session}={access}"}
    assert reads == []
    assert view(missed.user) == view(hit.user)
    assert (hit.scheme, hit.token_id) == ("cookie", digest)
    assert refused.status_code == 403  # From memory, still checked for CSRF.
    assert posted is not None
    assert posted.user.is_superuser
    assert with_header is None
    assert without_cookie is None
    assert sample("miss", "cookie") == before["miss"] + 1
    assert sample("hit", "cookie") == before["hit"] + 3
    for _ in range(2):  # Read, then as the token it is: never a session.
        assert run_async(credentials_of(headers=not_a_session)) is None
    run_async(credentials_of(headers=bearer(access)))
    assert run_async(credentials_of(headers=not_a_session)) is None


def test_json_web_tokens_kept_and_read_build_the_same_user(
    api: Api, statements: list[str]
) -> None:
    """A login and its user are kept by both; the signature is checked each time."""
    create_user("alice")
    grant("alice", "shop.view_product")
    client = api(**JWT)
    access = bearer(login(client, "alice")["access_token"])

    async def scenario() -> tuple[Any, ...]:
        missed = await credentials_of(headers=access)
        statements.clear()
        hit = await credentials_of(headers=access)
        reads = list(statements)
        with pytest.raises(AuthenticationFailed):
            await credentials_of(
                headers={"Authorization": access["Authorization"] + "x"}
            )
        return missed, hit, reads

    missed, hit, reads = run_async(scenario())
    assert reads == []
    assert view(missed.user) == view(hit.user)
    assert missed.session == hit.session is not None
    assert hit.scheme == "jwt"


def test_certificates_kept_and_read_build_the_same_user(
    api: Api, tmp_path: Path, statements: list[str]
) -> None:
    """By fingerprint and by subject: the same user, from memory the second time."""
    alice = create_user("alice")
    path, _ = certificate(tmp_path, "alice")
    pem = path.read_text(encoding="ascii")
    call_command("addcertificate", "alice", str(path), stdout=io.StringIO())
    api(AUTHENTICATION_CLASSES=CERTIFICATES)

    async def twice() -> tuple[Any, ...]:
        missed = await credentials_of(certificate_pem=pem)
        statements.clear()
        hit = await credentials_of(certificate_pem=pem)
        return missed, hit, list(statements)

    by_fingerprint = run_async(twice())
    api(AUTHENTICATION_CLASSES=CERTIFICATES, CLIENT_CERT={"MATCH": "subject"})
    by_subject = run_async(twice())

    stored = run_async(orm_user(alice))
    for missed, hit, reads in (by_fingerprint, by_subject):
        assert reads == []
        assert view(missed.user) == view(hit.user) == view(stored)
        assert hit.scheme == "certificate"
    assert by_fingerprint[1].token_id is not None
    assert by_subject[1].token_id is None


def test_certificates_record_no_use_and_are_counted_as_theirs(
    api: Api, tmp_path: Path, statements: list[str]
) -> None:
    """As before, a certificate's token stores no last use; counters name its class."""
    create_user("alice")
    path, _ = certificate(tmp_path, "alice")
    pem = path.read_text(encoding="ascii")
    call_command("addcertificate", "alice", str(path), stdout=io.StringIO())
    api(AUTHENTICATION_CLASSES=CERTIFICATES)
    before = {name: sample(name, "certificate") for name in ("hit", "miss")}

    with freeze_time("2026-05-01T10:00:00Z"):
        statements.clear()
        read = run_async(credentials_of(certificate_pem=pem))
        kept = run_async(credentials_of(certificate_pem=pem))

    assert read is not None
    assert kept is not None
    assert "UPDATE" not in statements
    assert sample("miss", "certificate") == before["miss"] + 1
    assert sample("hit", "certificate") == before["hit"] + 1

    async def last_used() -> Any:  # noqa: ANN401 - a datetime, or None.
        async with connections.engine().connect() as connection:
            return await connection.scalar(
                select(tokens.c.last_used_at).where(tokens.c.id == read.token_id)
            )

    assert run_async(last_used()) is None


def test_a_user_change_drops_the_principal_of_a_subject(
    api: Api, tmp_path: Path
) -> None:
    """Matched by subject, a principal rests on its user, as tokens' do."""
    create_user("alice")
    path, _ = certificate(tmp_path, "alice")
    pem = path.read_text(encoding="ascii")
    api(AUTHENTICATION_CLASSES=CERTIFICATES, CLIENT_CERT={"MATCH": "subject"})

    async def deactivate() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            user.is_active = False
            await uow.commit()

    with freeze_time("2026-05-01T10:00:00Z"):
        assert run_async(credentials_of(certificate_pem=pem)) is not None
        assert len(principal_cache) == 1
        run_async(deactivate())
        assert len(principal_cache) == 0
        with pytest.raises(AuthenticationFailed) as refused:
            run_async(credentials_of(certificate_pem=pem))
    assert refused.value.detail == "Unknown client certificate."


def test_a_subject_renamed_while_it_is_read_names_no_one(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read checks the name is still the user's, whoever renamed them."""
    create_user("alice")
    path, _ = certificate(tmp_path, "alice")
    pem = path.read_text(encoding="ascii")
    api(AUTHENTICATION_CLASSES=CERTIFICATES, CLIENT_CERT={"MATCH": "subject"})
    read = principals_store_principal()

    async def renamed_meanwhile(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        table = get_user_table()
        async with connections.engine().begin() as connection:  # Another process.
            await connection.execute(
                update(table).where(table.c.username == "alice").values(username="bob")
            )
        return await read(self, *args, **kwargs)

    monkeypatch.setattr(
        "domainkit.contrib.auth.repositories.PrincipalStore.principal",
        renamed_meanwhile,
    )
    with pytest.raises(AuthenticationFailed) as refused:
        run_async(credentials_of(certificate_pem=pem))
    assert refused.value.detail == "Unknown client certificate."
    assert len(principal_cache) == 0


# What every request checks, and what bounds the cache.


def test_an_expired_token_is_refused_from_memory(api: Api) -> None:
    """Expiry is checked on every request, on the clock: the entry goes with it."""
    create_user("alice")
    api()
    with freeze_time("2026-05-01T10:00:00Z") as clock:
        raw = run_async(issue("alice", ttl=timedelta(seconds=3)))
        assert run_async(credentials_of(headers=bearer(raw))) is not None
        clock.tick(3)
        before = sample("invalidation")
        with pytest.raises(AuthenticationFailed) as expired:
            run_async(credentials_of(headers=bearer(raw)))
    assert expired.value.detail == "Token expired."
    assert len(principal_cache) == 0
    assert sample("invalidation") == before + 1


def test_principals_are_read_again_after_the_ttl(
    api: Api, statements: list[str]
) -> None:
    """``TTL`` bounds how long one is used, whatever happens meanwhile."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(seconds=30)})
    raw = run_async(issue("alice"))

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        run_async(credentials_of(headers=bearer(raw)))
        statements.clear()
        clock.tick(29)
        run_async(credentials_of(headers=bearer(raw)))
        assert "SELECT" not in statements
        clock.tick(1)
        run_async(credentials_of(headers=bearer(raw)))
        assert statements.count("SELECT") == 2


def test_the_cache_keeps_at_most_max_entries(api: Api) -> None:
    """The least recently used principal makes room; evictions are counted."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"MAX_ENTRIES": 2})
    raws = [run_async(issue("alice")) for _ in range(3)]
    before = sample("eviction")

    for raw in raws:
        run_async(credentials_of(headers=bearer(raw)))

    assert len(principal_cache) == 2
    assert principal_cache.get(("token", token_digest(raws[0])), 0) is None
    assert sample("eviction") == before + 1


def test_the_cache_can_be_turned_off(api: Api, statements: list[str]) -> None:
    """``ENABLED = False``: every request reads its principal, the same way."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"ENABLED": False})
    raw = run_async(issue("alice"))

    first = run_async(credentials_of(headers=bearer(raw)))
    statements.clear()
    second = run_async(credentials_of(headers=bearer(raw)))

    assert statements.count("SELECT") == 2
    assert first is not None
    assert second is not None
    assert view(first.user) == view(second.user)
    assert len(principal_cache) == 0


def test_last_use_is_written_only_when_due(api: Api, statements: list[str]) -> None:
    """A hit opens no transaction; one request per period writes the time."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        raw = run_async(issue("alice"))
        run_async(set_last_used(raw, clock.now() - timedelta(minutes=4)))
        statements.clear()
        run_async(credentials_of(headers=bearer(raw)))  # Read: not due yet.
        assert "UPDATE" not in statements
        clock.tick(59)
        statements.clear()
        run_async(credentials_of(headers=bearer(raw)))
        assert statements == []  # Kept, and not due: nothing at all.
        clock.tick(1)
        run_async(credentials_of(headers=bearer(raw)))  # Kept, and due.
        run_async(credentials_of(headers=bearer(raw)))  # Written already.
        assert statements.count("UPDATE") == 1
        assert "SELECT" not in statements

    assert stored_last_used(raw).isoformat().startswith("2026-05-01T10:01:00")


def test_a_last_use_written_elsewhere_is_left(api: Api, statements: list[str]) -> None:
    """Due here, but written meanwhile by another process: no commit, no harm."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        raw = run_async(issue("alice"))
        run_async(set_last_used(raw, clock.now() - timedelta(minutes=4)))
        run_async(credentials_of(headers=bearer(raw)))
        run_async(set_last_used(raw, clock.now()))  # Another process wrote it.
        clock.tick(60)
        statements.clear()
        run_async(credentials_of(headers=bearer(raw)))

    assert statements.count("UPDATE") == 1
    assert "COMMIT" not in statements
    assert stored_last_used(raw).isoformat().startswith("2026-05-01T10:00:00")


def test_touching_a_loaded_token_writes_once_per_period(
    api: Api, statements: list[str]
) -> None:
    """``TokenStore.touch`` trusts the time it loaded, and writes at its end."""
    create_user("alice")
    api()

    async def touch(raw: str) -> tuple[bool, Any]:
        async with AuthUnitOfWork() as uow:
            token = await uow.tokens.find(raw)
            assert token is not None
            written = await uow.tokens.touch(token)
            await uow.commit()
        return written, token.last_used_at

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        raw = run_async(issue("alice"))
        start = clock.now()
        assert run_async(touch(raw)) == (True, start)
        clock.tick(LAST_USED_EVERY - timedelta(seconds=1))
        statements.clear()
        assert run_async(touch(raw)) == (False, start)
        assert "UPDATE" not in statements  # Not due: no statement at all.
        clock.tick(1)
        assert run_async(touch(raw)) == (True, start + LAST_USED_EVERY)

    assert stored_last_used(raw) == start + LAST_USED_EVERY


def stored_last_used(raw: str) -> Any:  # noqa: ANN401 - a datetime.
    """The ``last_used_at`` stored for token ``raw``."""

    async def read() -> Any:  # noqa: ANN401
        async with connections.engine().connect() as connection:
            return await connection.scalar(
                tokens.select()
                .with_only_columns(tokens.c.last_used_at)
                .where(tokens.c.id == token_digest(raw))
            )

    return run_async(read())


async def set_last_used(raw: str, when: Any) -> None:  # noqa: ANN401 - a datetime.
    """Write ``last_used_at`` of ``raw`` as another process would."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(tokens)
            .where(tokens.c.id == token_digest(raw))
            .values(last_used_at=when)
        )


def sample(event_name: str, authentication: str = "token") -> float:
    """The principal cache counter of ``event_name`` for one class."""
    value = REGISTRY.get_sample_value(
        "auth_principal_cache_total",
        {"authentication": authentication, "event": event_name},
    )
    return value or 0.0


# What drops principals, in this process at once.


@pytest.fixture
def kept(api: Api) -> Callable[..., TestClient]:
    """``api``, whose clients' principals are kept for an hour (a long TTL)."""

    def make(**options: Any) -> TestClient:  # noqa: ANN401 - AUTH keys.
        return api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)}, **options)

    return make


def test_logging_out_ends_a_kept_token_at_once(
    kept: Callable[..., TestClient],
) -> None:
    """This session, or every one: their principals go with the commit."""
    create_user("alice")
    client = kept()
    first, second, third = (
        bearer(login(client, "alice")["access_token"]) for _ in "123"
    )
    for headers in (first, second, third):
        assert client.get("/private", headers=headers).status_code == 200

    assert client.post("/auth/logout", headers=first).status_code == 204
    assert client.get("/private", headers=first).status_code == 401
    assert client.get("/private", headers=second).status_code == 200
    everywhere = client.post("/auth/logout", headers=second, json={"everywhere": True})
    assert everywhere.status_code == 204
    assert client.get("/private", headers=third).status_code == 401


@pytest.mark.parametrize(
    ("change", "path", "status"),
    [
        ("deactivate", "/private", 401),
        ("delete", "/private", 401),
        ("leave_group", "/products", 403),
        ("revoke_permission", "/products", 403),
        ("revoke_from_group", "/products", 403),
        ("delete_group", "/products", 403),
        ("set_password", "/private", 401),
        ("log_out_user", "/private", 401),
    ],
)
def test_admin_changes_reach_kept_principals_at_once(
    kept: Callable[..., TestClient], change: str, path: str, status: int
) -> None:
    """Every change staff make drops what it affects, before the next request."""
    alice = create_user("alice")
    create_user("root", is_superuser=True)
    client = kept()
    if change != "revoke_permission":  # The group gives it, and only the group.
        run_async(editors_with("alice", "shop.view_product"))
    else:  # Given directly, and only directly.
        grant("alice", "shop.view_product")
    headers = bearer(login(client, "alice")["access_token"])
    root = bearer(login(client, "root")["access_token"])
    groups = client.get("/admin/auth/groups", headers=root).json()["items"]
    editors = next((group["id"] for group in groups if group["name"] == "editors"), "")
    users = f"/admin/auth/users/{alice}"
    requests: dict[str, tuple[str, str, Any]] = {
        "deactivate": ("PATCH", users, {"is_active": False}),
        "delete": ("DELETE", users, None),
        "leave_group": ("PUT", f"{users}/groups", {"groups": []}),
        "revoke_permission": ("PUT", f"{users}/permissions", {"permissions": []}),
        "revoke_from_group": (
            "PUT",
            f"/admin/auth/groups/{editors}/permissions",
            {"permissions": []},
        ),
        "delete_group": ("DELETE", f"/admin/auth/groups/{editors}", None),
        "set_password": ("POST", f"{users}/password", {"password": NEW_PASSWORD}),
        "log_out_user": ("POST", f"{users}/logout", None),
    }
    method, url, body = requests[change]
    assert client.get(path, headers=headers).status_code == 200  # Now kept.

    response = client.request(method, url, headers=root, json=body)

    assert response.status_code < 300, response.json()
    assert client.get(path, headers=headers).status_code == status


def test_a_password_change_keeps_this_session_and_ends_the_others(
    kept: Callable[..., TestClient],
) -> None:
    """The kept session is read again; the others are refused at once."""
    create_user("alice")
    client = kept()
    mine, other = (bearer(login(client, "alice")["access_token"]) for _ in "12")
    for headers in (mine, other):
        assert client.get("/private", headers=headers).status_code == 200

    changed = client.post(
        "/auth/password",
        headers=mine,
        json={"old_password": "vy3!Tq-pX9#mR2", "new_password": "Brand-new-pa55"},
    )

    assert changed.status_code == 204
    assert client.get("/private", headers=mine).status_code == 200
    assert client.get("/private", headers=other).status_code == 401
    wrong = client.post(
        "/auth/password",
        headers=mine,
        json={"old_password": PASSWORD, "new_password": "Another-pa55"},
    )
    assert wrong.json()["code"] == "wrong_password"


def test_a_password_holds_only_for_a_stored_user(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user deleted meanwhile has no password to check."""
    from domainkit.contrib.auth.api import password_holds
    from domainkit.contrib.auth.hashers import acheck_password

    alice = create_user("alice")
    api()
    user = run_async(orm_user(alice))
    priorities: list[bool] = []

    async def check(password: str, encoded: str | None, *, priority: bool) -> bool:
        priorities.append(priority)
        return await acheck_password(password, encoded, priority=priority)

    monkeypatch.setattr("domainkit.contrib.auth.api.acheck_password", check)

    async def delete() -> None:
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(alice))
            await uow.users.remove(stored)
            await uow.commit()

    assert run_async(password_holds(user, PASSWORD))
    assert run_async(password_holds(user, PASSWORD, priority=True))
    assert priorities == [False, True]  # Reserved places only when asked.
    run_async(delete())
    assert not run_async(password_holds(user, PASSWORD))


@pytest.mark.parametrize(
    "command",
    [
        ["changepassword", "alice"],
        ["cleartokens", "--user", "alice"],
    ],
)
def test_commands_end_kept_tokens_at_once(
    kept: Callable[..., TestClient],
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators' commands drop what they change, as the API does."""
    create_user("alice")
    client = kept()
    headers = bearer(login(client, "alice")["access_token"])
    assert client.get("/private", headers=headers).status_code == 200
    monkeypatch.setattr(ChangePassword, "ask_secret", lambda *_: NEW_PASSWORD)

    call_command(*command, stdout=io.StringIO())

    assert client.get("/private", headers=headers).status_code == 401


def test_a_login_shows_on_kept_principals(kept: Callable[..., TestClient]) -> None:
    """``last_login`` changes with a new login: other sessions read it again."""
    create_user("alice")
    client = kept()
    headers = bearer(login(client, "alice")["access_token"])
    before = client.get("/auth/me", headers=headers).json()["last_login"]

    with freeze_time("2030-01-01T00:00:00Z"):
        login(client, "alice")

    after = client.get("/auth/me", headers=headers).json()["last_login"]
    assert before != after
    assert after.startswith("2030-01-01T00:00:00")


def test_orm_changes_outside_a_unit_of_work_drop_principals(api: Api) -> None:
    """A bare session's commit drops what it changed, too."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))
    assert run_async(credentials_of(headers=bearer(raw))) is not None

    async def deactivate() -> None:
        async with connections.session() as session:
            user = await session.get(User, alice)
            assert user is not None
            user.set_active(False)
            await session.commit()

    run_async(deactivate())
    with pytest.raises(AuthenticationFailed) as refused:
        run_async(credentials_of(headers=bearer(raw)))
    assert refused.value.detail == "User inactive or deleted."


def test_sql_outside_the_orm_waits_for_the_ttl_or_an_invalidation(
    api: Api,
) -> None:
    """A raw DELETE is not seen until ``invalidate`` (or the TTL) says so."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))
    assert run_async(credentials_of(headers=bearer(raw))) is not None

    async def delete() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                tokens.delete().where(tokens.c.id == token_digest(raw))
            )

    run_async(delete())
    assert run_async(credentials_of(headers=bearer(raw))) is not None
    assert principals.invalidate([token_target(token_digest(raw))]) == 1
    with pytest.raises(AuthenticationFailed):
        run_async(credentials_of(headers=bearer(raw)))
    assert principals.invalidate_all() == 0


def test_a_principal_read_while_its_token_is_revoked_is_not_kept(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revoked between the token and the user: served, as before, never kept."""
    create_user("alice")
    api()
    raw = run_async(issue("alice"))
    read = principals_store_principal()

    async def revoked_meanwhile(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()
        return await read(self, *args, **kwargs)

    monkeypatch.setattr(
        "domainkit.contrib.auth.repositories.PrincipalStore.principal",
        revoked_meanwhile,
    )
    assert run_async(credentials_of(headers=bearer(raw))) is not None
    monkeypatch.undo()

    assert len(principal_cache) == 0
    with pytest.raises(AuthenticationFailed) as refused:
        run_async(credentials_of(headers=bearer(raw)))
    assert refused.value.detail == "Invalid token."


def test_a_principal_whose_token_another_process_deletes_is_not_kept(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No invalidation reaches this process: the read itself checks the token."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))
    read = principals_store_principal()

    async def deleted_meanwhile(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        async with connections.engine().begin() as connection:  # Another process.
            await connection.execute(
                delete(tokens).where(tokens.c.id == token_digest(raw))
            )
        return await read(self, *args, **kwargs)

    monkeypatch.setattr(
        "domainkit.contrib.auth.repositories.PrincipalStore.principal",
        deleted_meanwhile,
    )
    with freeze_time("2026-05-01T10:00:00Z"):
        assert run_async(credentials_of(headers=bearer(raw))) is not None
        monkeypatch.undo()
        assert len(principal_cache) == 0
        with pytest.raises(AuthenticationFailed) as refused:
            run_async(credentials_of(headers=bearer(raw)))
    assert refused.value.detail == "Invalid token."


def principals_store_principal() -> Any:  # noqa: ANN401 - the unbound method.
    """``PrincipalStore.principal`` as it is."""
    from domainkit.contrib.auth.repositories import PrincipalStore

    return PrincipalStore.principal


def test_json_web_token_logins_end_and_renew(api: Api) -> None:
    """A refresh keeps the login kept; logging out ends it, for every access token."""
    create_user("alice")
    client = api(**JWT, PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    with freeze_time("2026-05-01T10:00:00Z") as clock:
        tokens_out = login(client, "alice")
        access = bearer(tokens_out["access_token"])
        assert client.get("/private", headers=access).status_code == 200
        clock.tick(60)
        renewed = client.post(
            "/auth/refresh", json={"refresh_token": tokens_out["refresh_token"]}
        ).json()
        fresh = bearer(renewed["access_token"])
        assert client.get("/private", headers=fresh).status_code == 200
        assert client.post("/auth/logout", headers=fresh).status_code == 204
        assert client.get("/private", headers=fresh).status_code == 401
        assert client.get("/private", headers=access).status_code == 401


def test_ending_a_login_drops_it_here_though_its_tokens_are_gone(api: Api) -> None:
    """``revoke_family`` drops the kept login even when another process deleted it."""
    create_user("alice")
    api(**JWT)
    authenticator = next(
        each for each in get_authenticators() if isinstance(each, JWTAuthentication)
    )

    async def log_in() -> tuple[str, UUID]:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            family = uuid4()
            await uow.tokens.issue(
                user.id, kind=TokenKind.REFRESH, ttl=timedelta(hours=1), family=family
            )
            await uow.commit()
        return authenticator.encode(user, family), family

    async def deleted_elsewhere_then_ended(family: UUID) -> int:
        async with connections.engine().begin() as connection:  # Another process.
            await connection.execute(delete(tokens).where(tokens.c.family == family))
        async with AuthUnitOfWork() as uow:
            ended = await uow.tokens.revoke_family(family)
            await uow.commit()
        return ended

    with freeze_time("2026-05-01T10:00:00Z"):
        access, family = run_async(log_in())
        assert run_async(credentials_of(headers=bearer(access))) is not None
        assert run_async(deleted_elsewhere_then_ended(family)) == 0
        with pytest.raises(AuthenticationFailed) as ended:
            run_async(credentials_of(headers=bearer(access)))
    assert ended.value.detail == "Session ended; log in again."


def test_a_kept_login_past_its_tokens_is_read_again(api: Api) -> None:
    """Its last token expired: read again, it may have a newer one, or none."""
    create_user("alice")
    api(**JWT)
    authenticator = next(
        each for each in get_authenticators() if isinstance(each, JWTAuthentication)
    )

    async def login_until(minutes: int) -> str:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            family = uuid4()
            await uow.tokens.issue(
                user.id,
                kind=TokenKind.REFRESH,
                ttl=timedelta(minutes=minutes),
                family=family,
            )
            await uow.commit()
        return authenticator.encode(user, family)

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        access = run_async(login_until(2))
        assert run_async(credentials_of(headers=bearer(access))) is not None
        clock.tick(119)
        assert run_async(credentials_of(headers=bearer(access))) is not None
        clock.tick(1)  # Past the login's last token, inside the cache's TTL.
        with pytest.raises(AuthenticationFailed) as ended:
            run_async(credentials_of(headers=bearer(access)))
    assert ended.value.detail == "Session ended; log in again."


def test_a_login_with_a_token_that_never_expires_lasts() -> None:
    """``until`` is None: the login is active whatever the time."""
    login_state = principals.Login(active=True, until=None)
    gone = principals.Login(active=False, until=None)

    with freeze_time("2099-01-01T00:00:00Z") as clock:
        assert login_state.active_at(clock.now())
        assert not gone.active_at(clock.now())


async def jwt_login(
    ttl: timedelta | None, family: UUID | None = None
) -> tuple[str, UUID, str]:
    """Alice's access token, login ``family``, and refresh token for ``ttl``.

    A new login when ``family`` is None; another token of it otherwise.
    """
    authenticator = next(
        each for each in get_authenticators() if isinstance(each, JWTAuthentication)
    )
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key("alice")
        assert user is not None
        family = family or uuid4()
        refresh = await uow.tokens.issue(
            user.id, kind=TokenKind.REFRESH, ttl=ttl, family=family
        )
        await uow.commit()
    return authenticator.encode(user, family), family, refresh


def test_logins_read_whether_any_token_never_expires(api: Api) -> None:
    """A login ends with its last expiring token, unless one never expires."""
    create_user("alice")
    api(**JWT)

    async def login_of(family: UUID) -> principals.Login:
        async with AuthUnitOfWork() as uow:
            return await uow.principals.login(family)

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        _, family, _ = run_async(jwt_login(timedelta(minutes=2)))
        ends = clock.now() + timedelta(minutes=2)
        assert run_async(login_of(family)) == principals.Login(active=True, until=ends)
        run_async(jwt_login(None, family))
        assert run_async(login_of(family)) == principals.Login(active=True, until=None)
        gone = principals.Login(active=False, until=None)
        assert run_async(login_of(uuid4())) == gone


def test_a_kept_login_renewed_meanwhile_is_read_again(api: Api) -> None:
    """Past the end it was kept with, a login is read again, and lasts if renewed."""
    create_user("alice")
    api(**JWT, PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        access, family, _ = run_async(jwt_login(timedelta(minutes=2)))
        assert run_async(credentials_of(headers=bearer(access))) is not None
        run_async(jwt_login(timedelta(minutes=10), family))  # A refresh elsewhere.
        clock.tick(120)  # Past the end kept, inside the cache's TTL.
        assert run_async(credentials_of(headers=bearer(access))) is not None


def test_ending_a_logins_only_token_drops_the_kept_login(api: Api) -> None:
    """``revoke`` of a login's token drops the login, which rests on its family."""
    create_user("alice")
    api(**JWT, PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})

    async def end(refresh: str) -> int:
        async with AuthUnitOfWork() as uow:
            ended = await uow.tokens.revoke(token_digest(refresh))
            await uow.commit()
        return ended

    with freeze_time("2026-05-01T10:00:00Z"):
        access, _, refresh = run_async(jwt_login(timedelta(hours=1)))
        assert run_async(credentials_of(headers=bearer(access))) is not None
        assert run_async(end(refresh)) == 1
        with pytest.raises(AuthenticationFailed) as ended:
            run_async(credentials_of(headers=bearer(access)))
    assert ended.value.detail == "Session ended; log in again."


def test_revoking_a_users_tokens_reads_each_of_their_principals_again(
    api: Api,
) -> None:
    """The token kept too: ``revoke_user`` drops every principal of the user."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})

    async def revoke_others(kept: str) -> int:
        async with AuthUnitOfWork() as uow:
            ended = await uow.tokens.revoke_user(alice, keep=kept)
            await uow.commit()
        return ended

    with freeze_time("2026-05-01T10:00:00Z"):
        raw = run_async(issue("alice"))
        assert run_async(credentials_of(headers=bearer(raw))) is not None
        assert len(principal_cache) == 1
        assert run_async(revoke_others(token_digest(raw))) == 0
        assert len(principal_cache) == 0
        assert run_async(credentials_of(headers=bearer(raw))) is not None


def test_live_connections_see_changes_as_requests_do(
    api: Api, statements: list[str]
) -> None:
    """Re-authorization reads through the cache: no query, and revocations at once."""
    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))
    middleware = AuthenticationMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
    socket = {**scope(bearer(raw)), "type": "websocket"}

    async def check() -> bool:
        socket["auth"] = await credentials_of(headers=bearer(raw))
        statements.clear()
        held = await middleware.reauthenticate(socket)
        assert statements == []
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()
        return held and not await middleware.reauthenticate(socket)

    assert run_async(check())


# Settings and checks.


def test_the_cache_is_bounded() -> None:
    """At most five minutes, at most a million entries; five seconds by default."""
    from pydantic import ValidationError

    from domainkit.contrib.auth.settings import PrincipalCacheSettings

    for bad in (
        {"TTL": timedelta(minutes=6)},
        {"TTL": timedelta(0)},
        {"MAX_ENTRIES": 0},
        {"MAX_ENTRIES": 1_000_001},
        {"STAMPS": ""},
    ):
        with pytest.raises(ValidationError):
            PrincipalCacheSettings(**bad)
    assert timedelta(seconds=5) == PrincipalCacheSettings().TTL


# Other processes: stamps in a shared cache.


@pytest.fixture
def shared(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeServer]:
    """A Redis (fakeredis) cache ``shared`` for stamps; ``connected`` toggles it."""
    server = FakeServer()
    monkeypatch.setattr(
        redis_cache, "create_client", lambda config: FakeRedis(server=server)
    )
    caches_setting = {
        "default": {"URL": "memory://"},
        "shared": {"URL": "redis://fakeredis:6379/0", "KEY_PREFIX": "stamps"},
    }
    with override_settings(CACHES=caches_setting):
        yield server


STAMPED: Final = {"TTL": timedelta(minutes=5), "STAMPS": "shared"}


def test_stamps_carry_changes_made_in_other_processes(
    api: Api, shared: FakeServer, statements: list[str]
) -> None:
    """A hit costs one round trip to the cache and none to the database."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw, other = run_async(issue("alice")), run_async(issue("alice"))
    digest = token_digest(raw)

    async def scenario() -> tuple[Any, ...]:
        await credentials_of(headers=bearer(raw))
        await credentials_of(headers=bearer(other))
        statements.clear()
        hit = await credentials_of(headers=bearer(raw))
        reads = list(statements)
        # Another process deletes the token, and stamps it.
        async with connections.engine().begin() as connection:
            await connection.execute(tokens.delete().where(tokens.c.id == digest))
        await principals.write_stamps("shared", [token_target(digest)])
        with pytest.raises(AuthenticationFailed) as revoked:
            await credentials_of(headers=bearer(raw))
        kept = await credentials_of(headers=bearer(other))
        # Another process deactivates the user: every token of theirs.
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(User.__table__)  # type: ignore[attr-defined]
                .where(User.__table__.c.id == alice)  # type: ignore[attr-defined]
                .values(is_active=False)
            )
        await principals.write_stamps("shared", [user_target(alice)])
        with pytest.raises(AuthenticationFailed) as deactivated:
            await credentials_of(headers=bearer(other))
        return hit, reads, revoked.value, kept, deactivated.value

    before = sample("invalidation")
    hit, reads, revoked, kept, deactivated = run_async(scenario())
    assert hit is not None
    assert reads == []
    assert revoked.detail == "Invalid token."
    assert kept is not None
    assert deactivated.detail == "User inactive or deleted."
    assert sample("invalidation") == before + 2  # One for each stamp seen changed.


def test_commits_stamp_what_they_change(api: Api, shared: FakeServer) -> None:
    """Revoking here stamps the token, so other processes drop it too."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    key = principals.stamp_key(token_target(token_digest(raw)))

    async def scenario() -> tuple[object, object]:
        before = await caches["shared"].get(key)
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()
        return before, await caches["shared"].get(key)

    before, after = run_async(scenario())
    assert before is None
    assert isinstance(after, str)
    assert len(after) == 16


def test_commits_stamp_nothing_without_stamps_or_changes(
    api: Api, shared: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``STAMPS``, or a commit that changed no principal: no stamp, no error."""
    create_user("alice")
    api()
    raw = run_async(issue("alice"))

    async def revoke() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()

    async def change_nothing() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.commit()

    run_async(revoke())
    api(PRINCIPAL_CACHE=STAMPED)
    run_async(change_nothing())

    failed = ("After-commit work", "Could not stamp")
    assert not [each for each in caplog.messages if each.startswith(failed)]


def test_a_failing_shared_cache_is_never_trusted(
    api: Api, shared: FakeServer, statements: list[str]
) -> None:
    """Down, it confirms nothing: each request reads the database, and keeps nothing."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    digest = token_digest(raw)

    async def scenario() -> tuple[Any, ...]:
        await credentials_of(headers=bearer(raw))
        shared.connected = False
        statements.clear()
        served = await credentials_of(headers=bearer(raw))
        reads = list(statements)
        kept = principal_cache.get(("token", digest), 0) is not None
        principal_cache.invalidate([ALL])
        await credentials_of(headers=bearer(raw))  # Read, but not kept.
        async with connections.engine().begin() as connection:
            await connection.execute(tokens.delete().where(tokens.c.id == digest))
        with pytest.raises(AuthenticationFailed) as refused:
            await credentials_of(headers=bearer(raw))
        return served, reads, kept, refused.value

    served, reads, kept, refused = run_async(scenario())
    assert served is not None
    assert reads.count("SELECT") == 2
    assert kept  # Left for when the cache answers again.
    assert refused.detail == "Invalid token."
    assert len(principal_cache) == 0


def test_stamps_that_cannot_be_written_are_logged(
    api: Api, shared: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    """This process drops the principal anyway; the others wait for the TTL."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    run_async(credentials_of(headers=bearer(raw)))
    shared.connected = False

    async def revoke() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()

    run_async(revoke())

    assert len(principal_cache) == 0
    before, after = [m for m in caplog.messages if m.startswith("Could not stamp")]
    expected = (
        "Could not stamp 1 changed principals in cache 'shared' (CacheUnavailable); "
        "other processes keep theirs for up to 0:05:00"
    )
    assert before == after == expected


def test_stamps_need_a_shared_redis_cache(use_auth: UseAuth) -> None:
    """``STAMPS`` must name a Redis cache: a memory one is this process's own."""
    redis = {"URL": "redis://cache.internal:6379/0", "KEY_PREFIX": "svc"}

    for stamps, problem in (("default", "'default'"), ("missing", "'missing'")):
        with pytest.raises(ImproperlyConfigured) as refused:
            use_auth(PRINCIPAL_CACHE={"STAMPS": stamps})
        assert str(refused.value) == (
            "AUTH settings cannot be used:\n"
            "  auth.E003: AUTH.PRINCIPAL_CACHE.STAMPS must name a Redis cache of "
            f"CACHES that every process shares, not {problem}."
        )
    use_auth(
        settings={"CACHES": {"default": {"URL": "memory://"}, "shared": redis}},
        PRINCIPAL_CACHE={"STAMPS": "shared"},
    )


def test_deployments_warn_of_long_ttls_without_stamps(use_auth: UseAuth) -> None:
    """Above five seconds, a change takes that long to reach other processes."""
    deployed = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
        "RATE_LIMIT_BACKEND": "domainkit.http.ratelimit.RedisBackend",
    }
    hashers = [f"{AUTHENTICATION}.hashers.PBKDF2PasswordHasher"]
    redis = {"URL": "rediss://:pw@cache.internal:6380/0", "KEY_PREFIX": "svc"}

    def warnings(settings: dict[str, Any] | None = None, **cache: Any) -> list[str]:  # noqa: ANN401
        use_auth(
            settings={**deployed, **(settings or {})},
            PASSWORD_HASHERS=hashers,
            PRINCIPAL_CACHE=cache,
        )
        return registry.get_domain_config("auth").deployment_warnings()

    assert warnings(TTL=timedelta(seconds=30)) == [
        (
            "AUTH.PRINCIPAL_CACHE.TTL is 30 seconds without STAMPS: with several "
            "processes, a revoked token, a deactivated user, or a removed "
            "permission keeps working in the others for up to 30 seconds. Set "
            "STAMPS to a Redis cache, or the TTL to 5 seconds or less."
        )
    ]
    assert warnings() == []
    assert warnings(TTL=timedelta(seconds=30), ENABLED=False) == []
    shared_cache = {"CACHES": {"default": {"URL": "memory://"}, "shared": redis}}
    assert warnings(shared_cache, TTL=timedelta(seconds=30), STAMPS="shared") == []


# A user model a snapshot cannot hold.


@pytest.fixture
def profiles(database_url: str, tmp_path: Path) -> Iterator[None]:
    """A project whose user loads addresses of its own with it."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit_tests.sample_domains.profiles",
            SHOP,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        MIDDLEWARE=MIDDLEWARE,
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "USER_MODEL": "domainkit_tests.sample_domains.profiles.models.Profile",
            "USER_TABLE": "domainkit_tests.sample_domains.profiles.tables.profiles",
        },
    ):
        setup()
        call_command("makemigrations", "-m", "profiles", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield
    registry.clear()


def test_users_with_relationships_of_their_own_are_read_as_before(
    profiles: None, statements: list[str]
) -> None:
    """The ORM loads them, addresses and all, every time; none is kept."""
    from domainkit_tests.sample_domains.profiles.models import Address, Profile

    alice = create_user("alice")

    async def scenario() -> tuple[Any, ...]:
        async with AuthUnitOfWork() as uow:
            uow.session.add(Address(id=uuid4(), profile_id=alice, city="Cairo"))
            await uow.commit()
        raw = await issue("alice")
        first = await credentials_of(headers=bearer(raw))
        statements.clear()
        second = await credentials_of(headers=bearer(raw))
        return first, second, list(statements)

    first, second, reads = run_async(scenario())
    assert principals.user_shape(Profile) is None
    assert first is not None
    assert second is not None
    assert isinstance(second.user, Profile)
    assert [address.city for address in second.user.addresses] == ["Cairo"]  # type: ignore[attr-defined]
    assert second.user.has_usable_password()
    assert lent_password(second.user) == run_async(stored_hash(alice))
    assert "SELECT" in reads
    assert len(principal_cache) == 0
    gone = Entry(("token", "x"), None, loaded=second.user)
    second.user.set_active(False)
    assert gone.user() is None


def test_tokens_of_deleted_users_the_orm_reads_name_no_one(profiles: None) -> None:
    """Read by the ORM too, a token whose user is gone is refused."""
    from domainkit_tests.sample_domains.profiles.models import Profile

    alice = create_user("alice")
    raw = run_async(issue("alice"))

    async def delete() -> None:
        table = Profile.__table__  # type: ignore[attr-defined]
        async with connections.engine().begin() as connection:
            await connection.execute(table.delete().where(table.c.id == alice))

    run_async(delete())
    with pytest.raises(AuthenticationFailed) as refused:
        run_async(credentials_of(headers=bearer(raw)))
    assert refused.value.detail == "User inactive or deleted."


# The counters.


def test_counters_name_the_class_never_the_user(api: Api) -> None:
    """Hits, misses, invalidations, and evictions, by authentication class."""
    create_user("alice")
    api()
    raw, other = run_async(issue("alice")), run_async(issue("alice"))
    before = {name: sample(name) for name in ("hit", "miss", "invalidation")}

    with freeze_time("2026-05-01T10:00:00Z"):
        run_async(credentials_of(headers=bearer(raw)))
        run_async(credentials_of(headers=bearer(raw)))
        run_async(credentials_of(headers=bearer(other)))
        dropped = principals.invalidate([ALL])

    assert dropped == 2
    assert sample("miss") == before["miss"] + 2
    assert sample("hit") == before["hit"] + 1
    assert sample("invalidation") == before["invalidation"] + 2  # One each.
    labels = {
        tuple(sorted(sample_.labels))
        for metric in REGISTRY.collect()
        if metric.name == "auth_principal_cache"
        for sample_ in metric.samples
    }
    assert labels == {("authentication", "event")}


# Edges: what can change while a principal is read, or around it.


def test_a_principal_read_while_its_user_changes_is_not_kept(
    api: Api, monkeypatch: pytest.MonkeyPatch, statements: list[str]
) -> None:
    """A grant committed during the read: served as read, read again next time."""
    create_user("alice")
    api()
    raw = run_async(issue("alice"))
    read = principals_store_principal()

    async def granted_meanwhile(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            user.grant(await uow.permissions.get("shop.view_product"))
            await uow.commit()
        return await read(self, *args, **kwargs)

    monkeypatch.setattr(
        "domainkit.contrib.auth.repositories.PrincipalStore.principal",
        granted_meanwhile,
    )
    served = run_async(credentials_of(headers=bearer(raw)))
    monkeypatch.undo()

    assert served is not None
    assert served.user.has_perm("shop.view_product")
    assert len(principal_cache) == 0
    statements.clear()
    run_async(credentials_of(headers=bearer(raw)))
    assert statements.count("SELECT") == 2


def test_tokens_of_users_deleted_in_sql_name_no_one(api: Api) -> None:
    """Tokens have no foreign key: a token whose user is gone is refused."""
    alice = create_user("alice")
    api()
    raw = run_async(issue("alice"))

    async def delete() -> None:
        table = User.__table__  # type: ignore[attr-defined]
        async with connections.engine().begin() as connection:
            await connection.execute(table.delete().where(table.c.id == alice))

    run_async(delete())
    with pytest.raises(AuthenticationFailed) as refused:
        run_async(credentials_of(headers=bearer(raw)))
    assert refused.value.detail == "User inactive or deleted."


def test_refresh_tokens_are_not_access_tokens(api: Api) -> None:
    """A login's refresh token, sent as a bearer token, is invalid, never kept."""
    create_user("alice")
    api()

    async def refresh_as_access() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            raw = await uow.tokens.issue(
                user.id, kind=TokenKind.REFRESH, ttl=None, family=uuid4()
            )
            await uow.commit()
        with pytest.raises(AuthenticationFailed) as refused:
            await credentials_of(headers=bearer(raw))
        assert refused.value.detail == "Invalid token."

    run_async(refresh_as_access())
    assert len(principal_cache) == 0


def test_inactive_users_are_refused_with_json_web_tokens(api: Api) -> None:
    """At the request, kept or not, and at the refresh."""
    create_user("alice")
    client = api(**JWT, PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    issued = login(client, "alice")
    assert client.get("/private", headers=bearer(issued["access_token"])).is_success

    async def deactivate() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            user.set_active(False)
            await uow.commit()

    run_async(deactivate())
    response = client.get("/private", headers=bearer(issued["access_token"]))
    refreshed = client.post(
        "/auth/refresh", json={"refresh_token": issued["refresh_token"]}
    )

    assert response.json()["detail"] == "User inactive or deleted."
    assert refreshed.json()["detail"] == "User inactive or deleted."


def test_tokens_the_orm_deletes_drop_their_principals(api: Api) -> None:
    """A token, or a JSON Web Token's login, removed through the ORM."""
    create_user("alice")
    client = api(**JWT, PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)})
    raw = run_async(issue("alice"))
    issued = login(client, "alice")
    access = bearer(issued["access_token"])
    assert run_async(credentials_of(headers=bearer(raw))) is not None
    assert client.get("/private", headers=access).status_code == 200

    async def delete_all() -> None:
        async with connections.session() as session:
            for digest in (token_digest(raw), token_digest(issued["refresh_token"])):
                await session.delete(await session.get(AuthToken, digest))
            await session.commit()

    run_async(delete_all())
    assert len(principal_cache) == 0
    assert client.get("/private", headers=access).status_code == 401


def test_only_instances_in_a_session_wait_for_its_commit(use_auth: UseAuth) -> None:
    """A user no session holds has no commit to wait for."""
    use_auth()
    user = User.create(password=PasswordHash.unusable(), username="alice")

    principals._after_commit_of(user, [user_target(user.id)])  # Nothing happens.


def test_units_of_work_without_a_session_stamp_nothing(
    use_auth: UseAuth, shared: FakeServer
) -> None:
    """Only SQL units of work commit what principals rest on.

    Auth's stamps are ``on_stage`` and ``on_committed`` hooks, which only a
    ``SqlAlchemyUnitOfWork`` runs: a unit of work kept in memory commits
    without them, and nothing reaches the shared cache.
    """
    use_auth(PRINCIPAL_CACHE=STAMPED)
    assert "auth" in {hook.domain for hook in on_stage}
    assert "auth" in {hook.domain for hook in on_committed}

    async def commit() -> tuple[int, list[bytes | str]]:
        async with FakeGadgetsUnitOfWork(InMemoryGadgets()) as uow:
            await uow.commit()
        client = FakeRedis(server=shared)
        stamped = await client.keys("*")
        await client.aclose()
        return uow.commits, stamped

    assert run_async(commit()) == (1, [])


def test_stamps_seen_changed_twice_drop_the_entry_once(
    api: Api, shared: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests find a stamp changed: one drops the entry, both read again."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    key = ("token", token_digest(raw))
    run_async(credentials_of(headers=bearer(raw)))
    read = principals.read_stamps

    async def raced(alias: str, targets: Any) -> Any:  # noqa: ANN401
        kept = principal_cache.get(key, 0)
        assert kept is not None
        assert principal_cache.discard(kept)  # Another request got there first.
        return ("changed", *await read(alias, targets))[: len(targets) + 1]

    monkeypatch.setattr(principals, "read_stamps", raced)
    before = sample("invalidation")
    entry_ = run_async(principals.recall(key, principals_settings(STAMPED)))

    assert entry_ is None
    assert sample("invalidation") == before


def principals_settings(options: dict[str, Any]) -> Any:  # noqa: ANN401
    """``PrincipalCacheSettings`` of ``options``."""
    from domainkit.contrib.auth.settings import PrincipalCacheSettings

    return PrincipalCacheSettings(**options)


def test_mutable_column_values_are_each_requests_own(use_auth: UseAuth) -> None:
    """A value a handler could change in place is copied for every user built."""
    use_auth()
    principal = principals.Principal(
        model=User,
        is_active=True,
        columns=(("username", "alice"), ("email", ["mutable", {"deep": []}])),
        groups=(),
        permissions=(),
        usable_password=True,
        password=HIDDEN,
    )

    first, second = principal.user(), principal.user()

    mine: Any = first.__dict__["email"]
    theirs: Any = second.__dict__["email"]
    assert mine == theirs == ["mutable", {"deep": []}]
    assert mine is not theirs
    assert mine[1]["deep"] is not theirs[1]["deep"]
    assert first.get_username() is second.get_username()  # Immutable: shared.


def test_custom_users_kept_and_read_build_the_same_user(
    accounts: Path, statements: list[str]
) -> None:
    """A swapped user model, with columns of its own, is kept the same way."""
    from domainkit_tests.sample_domains.accounts.models import Account

    async def scenario() -> tuple[Any, ...]:
        async with AuthUnitOfWork() as uow:
            account = await uow.users.create_user("carol", PASSWORD, phone="+20 1")
            account.grant(await uow.permissions.get("accounts.view_account"))
            await uow.commit()
        raw = await issue("carol")
        missed = await credentials_of(headers=bearer(raw))
        statements.clear()
        hit = await credentials_of(headers=bearer(raw))
        return account.id, missed, hit, list(statements)

    account_id, missed, hit, reads = run_async(scenario())
    stored = run_async(orm_user(account_id))
    assert reads == []
    assert isinstance(hit.user, Account)
    assert view(missed.user) == view(hit.user) == view(stored)
    assert hit.user.phone == "+20 1"
    assert hit.user.has_perm("accounts.view_account")


def test_changes_made_in_sql_can_be_dropped_everywhere(
    api: Api, shared: FakeServer
) -> None:
    """Here at once, and in the other processes through their stamps."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    target = token_target(token_digest(raw))
    run_async(credentials_of(headers=bearer(raw)))

    async def drop(options: dict[str, Any]) -> tuple[int, object]:
        dropped = await principals.invalidate_everywhere(
            [target], principals_settings(options)
        )
        return dropped, await caches["shared"].get(principals.stamp_key(target))

    async def given() -> object:  # The stamp the token's principal was kept with.
        return await caches["shared"].get(principals.stamp_key(target))

    kept_with = run_async(given())
    assert isinstance(kept_with, str)
    assert run_async(drop({"TTL": timedelta(minutes=5)})) == (1, kept_with)
    run_async(credentials_of(headers=bearer(raw)))
    dropped, stamp = run_async(drop(STAMPED))
    assert dropped == 1
    assert isinstance(stamp, str)
    assert stamp != kept_with


def test_refused_credentials_are_never_kept(api: Api) -> None:
    """Guesses, of any number, take no room: only accepted credentials do."""
    create_user("alice", is_active=False)
    api()
    inactive = run_async(issue("alice"))
    guesses = [f"dkt_{uuid4().hex}{uuid4().hex[:11]}" for _ in range(20)]

    for raw in [*guesses, inactive]:
        with pytest.raises(AuthenticationFailed):
            run_async(credentials_of(headers=bearer(raw)))

    assert len(principal_cache) == 0

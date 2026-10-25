"""Feature flags and dynamic settings (``domainkit.contrib.flags``)."""

import asyncio
import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import pytest
from fastapi.testclient import TestClient
from pydantic import Field
from sqlalchemy import insert, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.auth.models import AnonymousUser
from domainkit.contrib.flags import (
    Flag,
    FlagsUnitOfWork,
    InvalidFlag,
    InvalidSettingValue,
    dynamic_setting,
    flag_enabled,
)
from domainkit.contrib.flags import store as store_module
from domainkit.contrib.flags.events import FlagChanged
from domainkit.contrib.flags.models import DynamicSetting, bucket
from domainkit.contrib.flags.tables import dynamic_settings, flags
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.management import CommandError, call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    SHOP,
    MakeDatabase,
)
from domainkit_tests.test_auth_admin import staff
from domainkit_tests.test_auth_http import audited

FLAGS = "domainkit.contrib.flags"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
]
AUDIT = "domainkit.contrib.audit"
ADMIN = "/admin/flags"
FLAG_PERMISSIONS = (
    "flags.view_flag",
    "flags.add_flag",
    "flags.change_flag",
    "flags.delete_flag",
    "flags.view_dynamicsetting",
    "flags.change_dynamicsetting",
)


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


@pytest.fixture(autouse=True)
def declared() -> Iterator[None]:
    """Forget the dynamic settings a test declares."""
    before = dict(store_module._declared)
    yield
    store_module._declared.clear()
    store_module._declared.update(before)


@pytest.fixture
def project(database_url: str, tmp_path: Path) -> Iterator[None]:
    """Auth, audit, and flags, migrated, behind the staff routes."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, AUDIT, FLAGS, SHOP, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.staff_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        run("makemigrations", "-m", "init")
        run("migrate")
        yield
    registry.clear()


@pytest.fixture
def client(project: None) -> Iterator[TestClient]:
    """A client of the staff routes."""
    with TestClient(get_asgi_application()) as test_client:
        yield test_client


def clear_caches() -> None:
    """Empty the default cache, as its entries expiring would."""

    async def clear() -> None:
        await caches["default"].clear()

    run_async(clear())


def enabled(name: str, user: object = None) -> bool:
    """``flag_enabled`` from sync code."""
    return run_async(flag_enabled(name, user))


def max_items() -> Any:  # noqa: ANN401 - a declared setting.
    """Declare the test's dynamic setting."""
    return dynamic_setting(
        "checkout.max_items",
        Annotated[int, Field(ge=1, le=100)],
        default=10,
        description="Items per order.",
    )


# The rules ------------------------------------------------------------------------


def test_flags_are_on_for_everyone_no_one_or_some() -> None:
    """``everyone`` decides alone; then users, groups, and the rollout."""
    flag = Flag(id="beta")
    flag.configure(
        everyone=None, percentage=0, users=["u1", "u1"], groups=["staff"], note=""
    )

    assert flag.users == ("u1",)
    assert flag.enabled_for("u1", [])
    assert flag.enabled_for("u2", ["staff"])
    assert not flag.enabled_for("u2", ["other"])
    assert not flag.enabled_for(None, ["staff"])
    flag.configure(everyone=True, percentage=0, users=[], groups=[], note="")
    assert flag.enabled_for(None, [])
    flag.configure(everyone=False, percentage=100, users=["u1"], groups=[], note="")
    assert not flag.enabled_for("u1", [])


def test_rollouts_are_stable_and_grow_monotonically() -> None:
    """A user's bucket depends on the flag and the user only."""
    users = [str(number) for number in range(2000)]
    flag = Flag(id="rollout")
    on = {}
    for percentage in (0, 10, 50, 100):
        flag.configure(
            everyone=None, percentage=percentage, users=[], groups=[], note=""
        )
        on[percentage] = {user for user in users if flag.enabled_for(user, [])}

    assert on[0] == set()
    assert on[0] <= on[10] <= on[50] <= on[100] == set(users)
    assert 120 < len(on[10]) < 280
    assert bucket("rollout", "7") == bucket("rollout", "7")


@pytest.mark.parametrize("percentage", [-1, 101])
def test_percentages_stay_in_range(percentage: int) -> None:
    """A rollout is 0 to 100 percent."""
    with pytest.raises(InvalidFlag):
        Flag(id="x").configure(
            everyone=None, percentage=percentage, users=[], groups=[], note=""
        )


# Reading through the cache --------------------------------------------------------


def test_flags_are_read_for_users_of_any_shape(project: None) -> None:
    """Auth users, anonymous ones, and objects with an id and groups."""
    run("flags", "set", "beta", "--users", "u1", "--groups", "staff")

    assert enabled("beta", SimpleNamespace(id="u1"))
    assert enabled(
        "beta", SimpleNamespace(id="u2", groups=[SimpleNamespace(name="staff")])
    )
    assert enabled("beta", SimpleNamespace(id="u3", groups=["staff"]))
    assert not enabled("beta", SimpleNamespace(id="u4", groups=None))
    assert not enabled("beta", SimpleNamespace(groups=["staff"]))  # No id.
    assert not enabled("beta", AnonymousUser())
    assert not enabled("beta")
    assert not enabled("missing", SimpleNamespace(id="u1"))


def test_changes_drop_the_cached_copy(project: None) -> None:
    """Changes through the unit of work are seen at once; others after the TTL."""
    run("flags", "set", "beta", "--everyone", "off")
    assert not enabled("beta")

    run("flags", "set", "beta", "--everyone", "on")
    assert enabled("beta")

    async def behind_its_back() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(flags).values(everyone=False))
        await connections.close_all()

    asyncio.run(behind_its_back())
    assert enabled("beta")  # Still cached.
    clear_caches()
    assert not enabled("beta")


def test_the_cache_must_exist(project: None) -> None:
    """``FLAGS.CACHE`` names a configured cache."""
    registry.clear()
    with (
        override_settings(FLAGS={"CACHE": "nope"}),
        pytest.raises(ImproperlyConfigured, match=r"FLAGS.CACHE = 'nope'"),
    ):
        setup()


def test_flags_live_in_one_database(project: None, tmp_path: Path) -> None:
    """A shard group cannot hold them."""
    shards = {
        f"shard_{n}": {"URL": f"sqlite+aiosqlite:///{tmp_path / f'{n}.db'}"}
        for n in range(2)
    }
    registry.clear()
    with (
        override_settings(
            DATABASES={"default": {"URL": "sqlite+aiosqlite:///:memory:"}, **shards},
            SHARDS={"group": {"DATABASES": list(shards)}},
            DATABASE_ROUTING={"flags": "group"},
        ),
        pytest.raises(ImproperlyConfigured, match=r"DATABASE_ROUTING\['flags'\]"),
    ):
        setup()


# Dynamic settings -----------------------------------------------------------------


def test_dynamic_settings_are_typed_and_default(
    project: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The default until set; a stored value the type refuses gives the default."""
    setting = max_items()
    assert run_async(setting.get()) == 10

    assert (
        run("flags", "setting", "checkout.max_items", "20")
        == "checkout.max_items = 20\n"
    )
    assert run_async(setting.get()) == 20

    async def corrupt() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(dynamic_settings).values(value="lots"))
        await connections.close_all()

    asyncio.run(corrupt())
    clear_caches()
    with caplog.at_level(logging.WARNING, logger="domainkit.contrib.flags.store"):
        assert run_async(setting.get()) == 10
    assert "holds a value its type refuses" in caplog.text
    assert "lots" not in caplog.text


def test_dynamic_settings_check_their_declaration() -> None:
    """Keys are plain names, and the default fits the type."""
    with pytest.raises(ImproperlyConfigured, match="must be lower-case"):
        dynamic_setting("Bad Key", int, default=1)
    with pytest.raises(ImproperlyConfigured, match="does not fit its type"):
        dynamic_setting("ok.key", int, default="many")
    setting = max_items()
    with pytest.raises(
        InvalidSettingValue, match="less than or equal to 100"
    ) as refused:
        setting.checked(4242)
    assert "4242" not in str(refused.value)
    assert setting.json_schema() == {"maximum": 100, "minimum": 1, "type": "integer"}
    assert dynamic_setting("checkout.max_items", int, default=3).default == 3


# The flags command ----------------------------------------------------------------


def test_the_command_lists_and_changes_flags(project: None) -> None:
    """``set`` creates or changes only what it is given; ``delete`` removes."""
    assert run("flags") == "No flags.\n"
    assert run("flags", "set", "beta", "--percentage", "25", "--note", "new UI") == (
        "beta: 25% of users, users [], groups []  (new UI)\n"
    )
    assert run("flags", "set", "beta", "--users", "u2", "u1") == (
        "beta: 25% of users, users ['u1', 'u2'], groups []  (new UI)\n"
    )
    run("flags", "set", "alpha", "--everyone", "on", "--groups", "staff")
    run("flags", "set", "beta", "--everyone", "off")
    assert run("flags", "list") == (
        "alpha: on for everyone\nbeta: off for everyone  (new UI)\n"
    )
    assert "beta: 25% of users" in run("flags", "set", "beta", "--everyone", "rules")
    assert run("flags", "delete", "beta") == "Deleted beta.\n"
    with pytest.raises(CommandError, match="No flag is called 'beta'"):
        run("flags", "delete", "beta")
    with pytest.raises(CommandError, match="is not a flag name"):
        run("flags", "set", "Not Valid")
    with pytest.raises(CommandError, match="A rollout percentage is 0 to 100"):
        run("flags", "set", "beta", "--percentage", "101")
    assert [row["details"]["name"] for row in audited("flags.flag_changed")] == [
        "beta",
        "beta",
        "alpha",
        "beta",
        "beta",
    ]
    assert audited("flags.flag_deleted")[0]["details"] == {"name": "beta"}


def test_the_command_sets_dynamic_settings(project: None) -> None:
    """Values as JSON, validated; ``--reset`` goes back to the default."""
    assert run("flags", "settings") == "No dynamic settings are declared.\n"
    max_items()
    assert run("flags", "settings") == "checkout.max_items = 10 (default 10)\n"
    run("flags", "setting", "checkout.max_items", "30")
    run("flags", "setting", "checkout.max_items", "40")
    assert run("flags", "settings") == "checkout.max_items = 40 (default 10)\n"
    assert run("flags", "setting", "checkout.max_items", "--reset") == (
        "checkout.max_items is back to 10.\n"
    )
    assert run("flags", "setting", "checkout.max_items", "--reset") == (
        "checkout.max_items is back to 10.\n"
    )
    for arguments, message in [
        (("checkout.max_items",), r"^Give a value as JSON, or --reset\.$"),
        (
            ("checkout.max_items", "1", "--reset"),
            r"^Give a value as JSON, or --reset\.$",
        ),
        (("checkout.max_items", "{"), "is not JSON"),
        (("checkout.max_items", "0"), "greater than or equal to 1"),
        (("nope", "1"), "No dynamic setting is called 'nope'"),
    ]:
        with pytest.raises(CommandError, match=message):
            run("flags", "setting", *arguments)
    assert [row["details"] for row in audited("flags.setting_changed")] == [
        {"key": "checkout.max_items", "value": 30, "secret": False},
        {"key": "checkout.max_items", "value": 40, "secret": False},
    ]
    assert audited("flags.setting_reset")[0]["details"] == {"key": "checkout.max_items"}


def test_secret_settings_record_that_they_changed_not_their_value(
    project: None,
) -> None:
    """``secret=True`` keeps the value out of the trail and the listings (DATA-10).

    At 0.9.0 the value went into the append-only trail, for good.
    """
    api_key = dynamic_setting("payments.api_key", str, default="", secret=True)

    assert run("flags", "setting", "payments.api_key", '"sk_live_S3CR3T"') == (
        "payments.api_key = ********\n"
    )
    assert run("flags", "settings") == "payments.api_key = ******** (secret)\n"
    assert run_async(api_key.get()) == "sk_live_S3CR3T"  # Still stored and read.
    assert run("flags", "setting", "payments.api_key", "--reset") == (
        "payments.api_key is back to ********.\n"
    )
    (changed,) = audited("flags.setting_changed")
    assert changed["details"] == {
        "key": "payments.api_key",
        "value": None,
        "secret": True,
    }
    assert audited("flags.setting_reset")[0]["details"] == {"key": "payments.api_key"}
    assert "S3CR3T" not in repr(audited("flags.setting_changed"))


def test_a_stored_settings_repr_shows_no_value() -> None:
    """A secret value is not in the aggregate's ``repr`` (tracebacks, ``%r``)."""
    stored = DynamicSetting(id="payments.api_key")
    stored.change("sk_live_S3CR3T", secret=True)

    assert "S3CR3T" not in repr(stored)
    assert "payments.api_key" in repr(stored)
    assert stored.value == "sk_live_S3CR3T"


def test_the_trail_counts_a_flags_users(project: None) -> None:
    """Users of a flag are audited as a count; groups and rules as they are."""
    run("flags", "set", "beta", "--users", "u2", "u1", "--groups", "staff")
    run("flags", "set", "beta", "--users")

    assert [row["details"] for row in audited("flags.flag_changed")] == [
        {
            "name": "beta",
            "everyone": None,
            "percentage": 0,
            "users": 2,
            "groups": ["staff"],
        },
        {
            "name": "beta",
            "everyone": None,
            "percentage": 0,
            "users": 0,
            "groups": ["staff"],
        },
    ]


# Routes and the staff API ---------------------------------------------------------


def test_routes_behind_a_flag_look_absent_while_it_is_off(client: TestClient) -> None:
    """404 while off; served once on for the caller."""
    assert client.get("/beta").status_code == 404
    run("flags", "set", "beta", "--everyone", "on")
    assert client.get("/beta").json() == {"feature": "beta"}


def test_staff_need_the_flag_permissions(client: TestClient) -> None:
    """Anonymous callers get 401, staff without the permission 403."""
    assert client.get(f"{ADMIN}/flags").status_code == 401
    headers = staff("viewer", "flags.view_flag")
    assert client.get(f"{ADMIN}/flags", headers=headers).json() == []
    assert (
        client.post(f"{ADMIN}/flags", json={"name": "x"}, headers=headers).status_code
        == 403
    )
    assert client.get(f"{ADMIN}/settings", headers=headers).status_code == 403


def test_staff_manage_flags(client: TestClient) -> None:
    """Create, read, change, and delete; a taken name is 409 on ``name``."""
    headers = staff("ops", *FLAG_PERMISSIONS)

    created = client.post(
        f"{ADMIN}/flags",
        json={"name": "beta", "percentage": 10, "groups": ["staff"]},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json() == {
        "name": "beta",
        "everyone": None,
        "percentage": 10,
        "users": [],
        "groups": ["staff"],
        "note": "",
    }
    taken = client.post(f"{ADMIN}/flags", json={"name": "beta"}, headers=headers)
    assert taken.status_code == 409
    assert taken.json()["errors"][0]["loc"] == ["body", "name"]
    changed = client.put(
        f"{ADMIN}/flags/beta", json={"everyone": True}, headers=headers
    )
    assert changed.json()["everyone"] is True
    assert [
        f["name"] for f in client.get(f"{ADMIN}/flags", headers=headers).json()
    ] == ["beta"]
    assert client.delete(f"{ADMIN}/flags/beta", headers=headers).status_code == 204
    for method in ("PUT", "DELETE"):
        response = client.request(
            method, f"{ADMIN}/flags/beta", json={}, headers=headers
        )
        assert (response.status_code, response.json()["code"]) == (
            404,
            "flag_not_found",
        )
    bad = client.post(
        f"{ADMIN}/flags", json={"name": "Bad Name", "percentage": 200}, headers=headers
    )
    assert bad.status_code == 422
    assert (
        client.put(f"{ADMIN}/flags/Bad%20Name", json={}, headers=headers).status_code
        == 422
    )
    assert audited("flags.flag_changed")[0]["actor"] != "system"


def test_staff_manage_dynamic_settings(client: TestClient) -> None:
    """Values validated by the type; unknown keys are 404; reset to defaults."""
    max_items()
    headers = staff("ops", *FLAG_PERMISSIONS)

    listed = client.get(f"{ADMIN}/settings", headers=headers).json()
    assert listed == [
        {
            "key": "checkout.max_items",
            "value": 10,
            "default": 10,
            "description": "Items per order.",
            "json_schema": {"maximum": 100, "minimum": 1, "type": "integer"},
            "secret": False,
        }
    ]
    changed = client.put(
        f"{ADMIN}/settings/checkout.max_items", json={"value": 25}, headers=headers
    )
    assert changed.json()["value"] == 25
    assert client.get(f"{ADMIN}/settings", headers=headers).json()[0]["value"] == 25
    refused = client.put(
        f"{ADMIN}/settings/checkout.max_items", json={"value": 500}, headers=headers
    )
    assert (refused.status_code, refused.json()["code"]) == (
        422,
        "invalid_setting_value",
    )
    unknown = client.put(f"{ADMIN}/settings/nope", json={"value": 1}, headers=headers)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "unknown_setting")
    assert (
        client.put(
            f"{ADMIN}/settings/checkout.max_items", json={"value": 30}, headers=headers
        ).json()["value"]
        == 30
    )
    reset = client.delete(f"{ADMIN}/settings/checkout.max_items", headers=headers)
    assert reset.status_code == 204
    assert (
        client.delete(
            f"{ADMIN}/settings/checkout.max_items", headers=headers
        ).status_code
        == 204
    )
    assert client.get(f"{ADMIN}/settings", headers=headers).json()[0]["value"] == 10


def test_staff_see_no_value_of_a_secret_setting(client: TestClient) -> None:
    """The staff API shows that a secret setting is set, not its value."""
    dynamic_setting("payments.api_key", str, default="none", secret=True)
    headers = staff("ops", *FLAG_PERMISSIONS)

    changed = client.put(
        f"{ADMIN}/settings/payments.api_key",
        json={"value": "sk_live_S3CR3T"},
        headers=headers,
    )
    listed = client.get(f"{ADMIN}/settings", headers=headers).json()

    assert changed.status_code == 200
    assert (changed.json()["value"], changed.json()["secret"]) == (None, True)
    assert (listed[0]["value"], listed[0]["default"]) == (None, None)
    assert "S3CR3T" not in changed.text
    (row,) = audited("flags.setting_changed")
    assert row["details"]["value"] is None


def test_migrate_creates_the_flag_permissions(project: None) -> None:
    """Content types give flags and settings their permissions, audit its one."""

    async def keys() -> set[str]:
        from domainkit.contrib.auth import AuthUnitOfWork

        async with AuthUnitOfWork() as uow:
            return {permission.key for permission in await uow.permissions.all()}

    found = run_async(keys())
    assert set(FLAG_PERMISSIONS) <= found
    assert "flags.add_dynamicsetting" not in found
    assert "audit.view_history" in found
    assert not [key for key in found if key.endswith("_auditentry")]


def test_stored_settings_can_be_read_without_a_declaration(project: None) -> None:
    """Loading tolerates stored keys nobody declares any more."""

    async def store() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                insert(dynamic_settings).values(id="old.key", value=1, version=1)
            )
        await connections.close_all()

    asyncio.run(store())
    assert run_async(store_module.current()).settings == {"old.key": 1}
    assert not enabled("beta")


def test_rollout_buckets_never_change() -> None:
    """A user's bucket is part of the contract: releases must not move it."""
    flag = Flag(id="rollout")
    assert bucket("rollout", "7") == 43

    flag.configure(everyone=None, percentage=43, users=[], groups=[], note="")
    assert not flag.enabled_for("7", [])
    flag.configure(everyone=None, percentage=44, users=[], groups=[], note="")
    assert flag.enabled_for("7", [])


def test_changes_are_recorded_with_their_rules() -> None:
    """``FlagChanged`` carries every rule but the note."""
    flag = Flag(id="beta")
    flag.configure(
        everyone=False, percentage=5, users=["u2", "u1"], groups=["g"], note="why"
    )

    (event,) = flag.pull_events()
    assert isinstance(event, FlagChanged)
    assert (event.name, event.everyone, event.percentage) == ("beta", False, 5)
    assert (event.users, event.groups) == (("u1", "u2"), ("g",))
    setting = DynamicSetting(id="checkout.max_items")
    setting.change(25)  # Not secret unless said.
    (changed,) = setting.pull_events()
    assert (changed.value, changed.secret) == (25, False)  # type: ignore[attr-defined]


def test_flags_are_read_with_the_stored_rules(project: None) -> None:
    """The rollout uses the flag's name; anonymous users are nobody."""
    user = SimpleNamespace(id="7")
    run("flags", "set", "rollout", "--percentage", "43")
    assert not enabled("rollout", user)
    run("flags", "set", "rollout", "--percentage", "44")
    assert enabled("rollout", user)

    run("flags", "set", "beta", "--users", "u1", "--groups", "staff")
    assert not enabled("beta", SimpleNamespace(id="u1", is_authenticated=False))
    assert not enabled("beta", SimpleNamespace(id="u9"))  # No groups at all.


def test_cached_flags_expire_after_the_ttl(project: None) -> None:
    """Changes made behind the store's back show once ``CACHE_TTL`` passes."""
    with freeze_time() as clock:
        run("flags", "set", "beta", "--everyone", "on")
        assert enabled("beta")

        async def behind_its_back() -> None:
            async with connections.engine().begin() as connection:
                await connection.execute(update(flags).values(everyone=False))
            await connections.close_all()

        asyncio.run(behind_its_back())
        clock.tick(timedelta(seconds=29))
        assert enabled("beta")
        clock.tick(timedelta(seconds=2))
        assert not enabled("beta")


def test_setting_values_are_stored_as_json(
    project: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Dates and the like are stored as JSON text; unset settings log nothing."""
    when = dynamic_setting(
        "launch.at", datetime, default=datetime(2026, 1, 1, tzinfo=UTC)
    )
    pair = dynamic_setting("grid.size", tuple[int, int], default=(1, 2))

    assert when.default_json() == "2026-01-01T00:00:00Z"
    assert when.checked("2026-02-01T00:00:00+00:00") == "2026-02-01T00:00:00Z"
    assert pair.default_json() == [1, 2]
    assert dynamic_setting("plain.key", int, default=1).description == ""
    with pytest.raises(InvalidSettingValue) as refused:
        pair.checked(["a", "b"])
    assert str(refused.value) == (
        "grid.size: Input should be a valid integer, unable to parse string as an "
        "integer; Input should be a valid integer, unable to parse string as an "
        "integer."
    )
    with caplog.at_level(logging.WARNING, logger="domainkit.contrib.flags.store"):
        assert run_async(when.get()) == datetime(2026, 1, 1, tzinfo=UTC)
    assert caplog.text == ""


def test_refused_stored_values_are_logged_by_key(
    project: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning names the key, never the value."""
    setting = max_items()
    run("flags", "setting", "checkout.max_items", "20")

    async def corrupt() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(dynamic_settings).values(value="lots"))
        await connections.close_all()

    asyncio.run(corrupt())
    clear_caches()
    with caplog.at_level(logging.WARNING, logger="domainkit.contrib.flags.store"):
        run_async(setting.get())

    assert [record.getMessage() for record in caplog.records] == [
        (
            "Dynamic setting 'checkout.max_items' holds a value its type refuses; "
            "using the default"
        )
    ]


def test_the_command_refuses_unknown_everyone_values(
    project: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--everyone`` is on, off, or rules."""
    with pytest.raises(SystemExit):
        run("flags", "set", "beta", "--everyone", "maybe")

    assert "invalid choice: 'maybe'" in capsys.readouterr().err


def test_flagged_routes_ask_about_the_caller(client: TestClient) -> None:
    """The request's user decides; while off, the route is a plain 404."""
    headers = staff("tester")

    async def user_id() -> str:
        from domainkit.contrib.auth import AuthUnitOfWork

        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("tester")
            assert user is not None
            return str(user.id)

    run("flags", "set", "beta", "--users", run_async(user_id()))

    assert client.get("/beta", headers=headers).json() == {"feature": "beta"}
    absent = client.get("/beta")
    assert (absent.status_code, absent.json()["code"], absent.json()["detail"]) == (
        404,
        "not_found",
        "Not found.",
    )


def test_out_of_range_percentages_say_the_range() -> None:
    """The refusal says what a rollout may be."""
    with pytest.raises(InvalidFlag) as refused:
        Flag(id="x").configure(
            everyone=None, percentage=101, users=[], groups=[], note=""
        )

    assert refused.value.message == "A rollout percentage is 0 to 100."


def test_flags_are_read_where_they_are_routed(
    project: None, make_database: MakeDatabase
) -> None:
    """With ``DATABASE_ROUTING['flags']``, flags are written and read there."""
    with override_settings(
        DATABASES={
            "default": {"URL": connections.config().URL.get_secret_value()},
            "flags_db": {"URL": make_database("flags")},
        },
        DATABASE_ROUTING={"flags": "flags_db"},
    ):

        async def create() -> None:
            async with connections.engine("flags_db").begin() as connection:
                await connection.run_sync(flags.create)
                await connection.run_sync(dynamic_settings.create)
                await connection.run_sync(audit_log.create)  # Changes are audited.
            await connections.close_all()

        asyncio.run(create())
        run("flags", "set", "beta", "--everyone", "on")

        assert enabled("beta")


def test_a_shard_group_is_refused_by_the_setting_that_names_it(
    project: None, tmp_path: Path
) -> None:
    """The refusal names ``DATABASE_ROUTING['flags']`` and its value."""
    shards = {
        f"shard_{n}": {"URL": f"sqlite+aiosqlite:///{tmp_path / f'{n}.db'}"}
        for n in range(2)
    }
    registry.clear()
    with (
        override_settings(
            DATABASES={"default": {"URL": "sqlite+aiosqlite:///:memory:"}, **shards},
            SHARDS={"group": {"DATABASES": list(shards)}},
            DATABASE_ROUTING={"flags": "group"},
        ),
        pytest.raises(
            ImproperlyConfigured,
            match=r"^DATABASE_ROUTING\['flags'\] = 'group' must name a SQL alias",
        ),
    ):
        setup()


def test_staff_see_every_rule_of_a_flag(client: TestClient) -> None:
    """The users and the note are part of what is created and listed."""
    headers = staff("ops", *FLAG_PERMISSIONS)
    rules = {
        "name": "beta",
        "everyone": None,
        "percentage": 0,
        "users": ["u2", "u1"],
        "groups": [],
        "note": "for the beta testers",
    }

    created = client.post(f"{ADMIN}/flags", json=rules, headers=headers)

    expected = {**rules, "users": ["u1", "u2"]}
    assert created.json() == expected
    assert client.get(f"{ADMIN}/flags", headers=headers).json() == [expected]


def test_the_staff_api_documents_its_errors(client: TestClient) -> None:
    """Each endpoint documents its error statuses with the error body."""
    schema = client.get("/openapi.json").json()

    responses = schema["paths"][f"{ADMIN}/flags/{{name}}"]["put"]["responses"]
    for code in ("401", "403", "404", "409", "422"):
        assert responses[code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_flags_changed_after_listing_are_recorded(project: None) -> None:
    """Flags read with ``all()`` are tracked: their changes are events."""
    run("flags", "set", "beta", "--everyone", "off")
    assert not enabled("beta")

    async def everyone_on() -> None:
        async with FlagsUnitOfWork() as uow:
            for flag in await uow.flags.all():
                flag.configure(
                    everyone=True, percentage=0, users=[], groups=[], note=""
                )
            await uow.commit()

    run_async(everyone_on())

    assert enabled("beta")  # The cached copy was dropped at once.
    assert [row["details"]["everyone"] for row in audited("flags.flag_changed")] == [
        False,
        True,
    ]

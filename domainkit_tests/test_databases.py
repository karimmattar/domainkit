"""Several databases: kinds, shard groups, routing, and units of work."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from domainkit import setup
from domainkit.conf import (
    UNSET,
    DatabaseConfig,
    Settings,
    ShardConfig,
    override_settings,
    settings,
)
from domainkit.db import connections
from domainkit.db.routing import (
    ShardMap,
    bucket_for,
    for_each_shard,
    router,
    shard_text,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakePackage
from domainkit_tests.settings import SECRET_KEY
from domainkit_tests.test_db import NotesUnitOfWork

SQLITE = "sqlite+aiosqlite:///:memory:"
LEDGER = "immudb://immudb:secret@ledger.internal:3322/evidence"
ALPHA = "domainkit_tests.sample_domains.alpha"


def sql(url: str = SQLITE, **options: Any) -> dict[str, Any]:  # noqa: ANN401 - options.
    """A SQL ``DATABASES`` entry."""
    return {"URL": url, **options}


def validate(**values: Any) -> Settings:  # noqa: ANN401 - settings values.
    """Validate settings built from ``values``."""
    return Settings.model_validate(values)


@pytest.fixture
def two_shards() -> Iterator[None]:
    """A shard group of two databases, routing the alpha domain to it."""
    with override_settings(
        DATABASES={"default": sql(), "shard_a": sql(), "shard_b": sql()},
        SHARDS={"tenants": {"DATABASES": ["shard_a", "shard_b"], "BUCKETS": 16}},
        DATABASE_ROUTING={"alpha": "tenants"},
    ):
        yield


# SQL databases and ledgers ---------------------------------------------------


def test_ledgers_have_their_own_setting() -> None:
    """DATABASES holds SQL databases; LEDGERS holds ledgers, with their options."""
    current = validate(
        DATABASES={"default": sql()},
        LEDGERS={
            "evidence": {"URL": LEDGER, "TIMEOUT": 3},
            "scratch": {"URL": "memory://"},
        },
    )

    assert isinstance(current.DATABASES["default"], DatabaseConfig)
    assert current.LEDGERS["evidence"].scheme == "immudb"
    assert current.LEDGERS["evidence"].TIMEOUT == 3
    assert current.LEDGERS["scratch"].scheme == "memory"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"DATABASES": {"default": {"URL": "memory://"}}}, "configure it in LEDGERS"),
        ({"DATABASES": {"default": {"URL": LEDGER}}}, "configure it in LEDGERS"),
        ({"LEDGERS": {"l": {"URL": "immudb://"}}}, "needs a host"),
        ({"LEDGERS": {"l": {"URL": "memory://", "POOL_SIZE": 1}}}, "POOL_SIZE"),
        ({"LEDGERS": {"l": {"URL": "sqlite+aiosqlite://"}}}, "immudb:// or memory://"),
    ],
)
def test_each_setting_takes_its_kind(values: dict[str, Any], message: str) -> None:
    """A ledger URL in DATABASES, or a SQL URL in LEDGERS, says where it goes."""
    with pytest.raises(ValidationError, match=message):
        validate(**values)


# Shard groups ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "message"),
    [
        ({"DATABASES": ["a", "a"]}, "lists an alias twice"),
        ({"DATABASES": ["a", "b"], "BUCKETS": 1}, "BUCKETS must be at least"),
        ({"DATABASES": ["a"], "PINNED": {"k": "z"}}, "outside the group"),
        ({"DATABASES": ["a", "b"], "RANGES": {"a": [[0, 15]]}}, "exactly the group"),
        (
            {
                "DATABASES": ["a", "b"],
                "BUCKETS": 8,
                "RANGES": {"a": [[0, 3]], "b": [[3, 7]]},
            },
            "bucket 4 is missing or overlapped",
        ),
        (
            {
                "DATABASES": ["a", "b"],
                "BUCKETS": 8,
                "RANGES": {"a": [[0, 3]], "b": [[5, 7]]},
            },
            "bucket 4 is missing",
        ),
        (
            {
                "DATABASES": ["a", "b"],
                "BUCKETS": 8,
                "RANGES": {"a": [[0, 3]], "b": [[4, 6]]},
            },
            "they end at 6",
        ),
        (
            {"DATABASES": ["a"], "BUCKETS": 8, "RANGES": {"a": [[0, 3], [5, 4]]}},
            "missing or overlapped",
        ),
    ],
)
def test_shard_groups_are_validated(group: dict[str, Any], message: str) -> None:
    """Every bucket has exactly one owner, and pins stay inside the group."""
    with pytest.raises(ValidationError, match=message):
        ShardConfig.model_validate(group)


def test_buckets_default_to_equal_ranges() -> None:
    """Without RANGES, buckets split evenly in list order."""
    group = ShardConfig.model_validate({"DATABASES": ["a", "b", "c"], "BUCKETS": 10})
    explicit = ShardConfig.model_validate(
        {
            "DATABASES": ["a", "b"],
            "BUCKETS": 4,
            "RANGES": {"a": [[0, 0], [3, 3]], "b": [[1, 2]]},
        }
    )

    assert group.ranges() == {"a": ((0, 2),), "b": ((3, 5),), "c": ((6, 9),)}
    assert explicit.ranges() == {"a": ((0, 0), (3, 3)), "b": ((1, 2),)}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {
                "DATABASES": {"default": sql(), "t": sql()},
                "SHARDS": {"t": {"DATABASES": ["t"]}},
            },
            "has the name of an alias",
        ),
        (
            {
                "DATABASES": {"default": sql()},
                "SHARDS": {"g": {"DATABASES": ["default"]}},
            },
            "other than 'default'",
        ),
        (
            {
                "DATABASES": {"default": sql()},
                "SHARDS": {"g": {"DATABASES": ["missing"]}},
            },
            "must be a SQL alias",
        ),
        (
            {
                "DATABASES": {"default": sql()},
                "LEDGERS": {"l": {"URL": "memory://"}},
                "SHARDS": {"g": {"DATABASES": ["l"]}},
            },
            "must be a SQL alias",
        ),
        (
            {
                "DATABASES": {"default": sql(), "a": sql()},
                "SHARDS": {"g": {"DATABASES": ["a"]}, "h": {"DATABASES": ["a"]}},
            },
            "in shard groups 'g' and 'h'",
        ),
        (
            {"DATABASES": {"default": sql()}, "DATABASE_ROUTING": {"x": "nowhere"}},
            "nowhere",
        ),
        (
            {
                "DATABASES": {"default": sql()},
                "LEDGERS": {"l": {"URL": "memory://"}},
                "DATABASE_ROUTING": {"x": "l"},
            },
            "outside the shard groups",
        ),
        (
            {
                "DATABASES": {"default": sql(), "a": sql()},
                "SHARDS": {"g": {"DATABASES": ["a"]}},
                "DATABASE_ROUTING": {"x": "a"},
            },
            "outside the shard groups",
        ),
    ],
)
def test_groups_and_routing_are_validated(values: dict[str, Any], message: str) -> None:
    """Groups use SQL aliases once each; routing targets a group or a free alias."""
    with pytest.raises(ValidationError, match=message):
        validate(**values)


def test_every_routing_entry_is_checked() -> None:
    """A valid entry does not stop the check of the ones after it."""
    with pytest.raises(ValidationError, match="'nowhere'"):
        validate(
            DATABASES={"default": sql()},
            DATABASE_ROUTING={"first": "default", "second": "nowhere"},
        )


def test_routing_names_installed_domains() -> None:
    """A misspelled domain label fails at setup, with a suggestion."""
    with (
        override_settings(DATABASE_ROUTING={"alpah": "default"}),
        pytest.raises(ImproperlyConfigured, match="alpah \\(did you mean alpha\\?\\)"),
    ):
        setup()


# Ledger deployment checks ------------------------------------------------------


@pytest.mark.parametrize(
    ("ledger", "message"),
    [
        ({"URL": "memory://"}, "memory:// ledgers are for tests"),
        ({"URL": LEDGER, "SERVER_SIGNING_KEY": "/k.pub"}, "need TLS_CA"),
        ({"URL": LEDGER, "TLS_CA": "/ca.pem"}, "need SERVER_SIGNING_KEY"),
    ],
)
def test_deployments_need_verified_ledgers(
    ledger: dict[str, Any], message: str
) -> None:
    """Production ledgers use immudb with TLS and signed states."""
    database = {"URL": "postgresql+asyncpg://db/app", "SSLMODE": "verify-full"}
    with pytest.raises(ValidationError, match=message):
        validate(
            ENVIRONMENT="production",
            ALLOWED_HOSTS=["api.example.com"],
            SECRET_KEY=SECRET_KEY,
            DATABASES={"default": database},
            LEDGERS={"evidence": ledger},
        )


def test_verified_ledgers_pass_the_deployment_checks() -> None:
    """TLS and a signing key are enough."""
    database = {"URL": "postgresql+asyncpg://db/app", "SSLMODE": "verify-full"}
    ledger = {"URL": LEDGER, "TLS_CA": "/ca.pem", "SERVER_SIGNING_KEY": "/k.pub"}

    current = validate(
        ENVIRONMENT="production",
        ALLOWED_HOSTS=["api.example.com"],
        SECRET_KEY=SECRET_KEY,
        DATABASES={"default": database},
        LEDGERS={"evidence": ledger},
    )

    assert current.LEDGERS["evidence"].TLS_CA is not None


# Shard keys and maps -----------------------------------------------------------


def test_shard_keys_hash_to_fixed_buckets() -> None:
    """Frozen values: changing the hash would strand every row in its shard."""
    assert bucket_for("tenant-1", 4096) == 3827
    assert bucket_for(42, 4096) == 2170
    assert bucket_for(uuid.UUID(int=1), 4096) == 1896
    assert bucket_for("", 16) == 4


@pytest.mark.parametrize("key", [True, 1.5, None, b"bytes"])
def test_shard_keys_are_text_ints_or_uuids(key: object) -> None:
    """Booleans and floats are refused: their text is ambiguous."""
    with pytest.raises(TypeError, match="str, int, or UUID"):
        shard_text(key)  # type: ignore[arg-type]


def test_shard_maps_route_keys_and_buckets() -> None:
    """Pins win; otherwise the bucket's owner holds the key."""
    config = ShardConfig.model_validate(
        {
            "DATABASES": ["a", "b"],
            "BUCKETS": 8,
            "RANGES": {"a": [[0, 1], [6, 7]], "b": [[2, 5]]},
            "PINNED": {"tenant-1": "a"},
        }
    )
    shards = ShardMap("tenants", config)

    assert [shards.owner_of(bucket) for bucket in range(8)] == list("aabbbbaa")
    assert shards.alias_for("tenant-1") == "a"
    assert shards.alias_for(42) == shards.owner_of(bucket_for(42, 8))
    assert shards.aliases == ("a", "b")
    with pytest.raises(ValueError, match="outside 0-7"):
        shards.owner_of(8)


# The router --------------------------------------------------------------------


def test_routing_targets_and_aliases(two_shards: None) -> None:
    """Domains map to targets, targets to aliases, keys to one alias."""
    setup()

    assert router.target_for("alpha") == "tenants"
    assert router.target_for("other") == "default"
    assert router.is_shard_group("tenants")
    assert router.aliases_of("tenants") == ("shard_a", "shard_b")
    assert router.aliases_of("default") == ("default",)
    assert router.target_of_alias("shard_b") == "tenants"
    assert router.target_of_alias("default") == "default"
    assert router.alias_for("default") == "default"
    assert router.alias_for("tenants", shard_key="tenant-1") in {"shard_a", "shard_b"}
    assert router.hosting_targets() == ("default", "tenants")


def test_routing_refuses_missing_and_misplaced_keys(two_shards: None) -> None:
    """A shard group needs a key; a plain alias refuses one."""
    with pytest.raises(ImproperlyConfigured, match="pass shard_key"):
        router.alias_for("tenants")
    with pytest.raises(ImproperlyConfigured, match="takes no shard_key"):
        router.alias_for("default", shard_key="tenant-1")
    with pytest.raises(ImproperlyConfigured, match="neither a SQL alias"):
        router.aliases_of("nowhere")
    with pytest.raises(ImproperlyConfigured, match="no 'nowhere' group"):
        router.shards("nowhere")


def test_modules_route_by_their_domain(two_shards: None) -> None:
    """The innermost installed domain containing a module decides its target."""
    setup()

    assert router.target_for_module(ALPHA) == "tenants"
    assert router.target_for_module(f"{ALPHA}.infrastructure.orm") == "tenants"
    assert router.target_for_module("domainkit_tests.sample_domains") == "default"
    assert router.target_for_module(f"{ALPHA}x.orm") == "default"
    assert router.shards("tenants").name == "tenants"


def test_the_innermost_domain_decides(
    two_shards: None, make_package: MakePackage
) -> None:
    """A domain nested in another domain's package routes its own modules."""
    make_package("outer", {})
    make_package("outer.inner", {})
    with override_settings(
        INSTALLED_DOMAINS=["outer.inner", "outer", ALPHA],
        DATABASE_ROUTING={"inner": "tenants", "alpha": "tenants"},
    ):
        setup()

        assert router.target_for_module("outer.inner.domain.model") == "tenants"
        assert router.target_for_module("outer.domain.model") == "default"


def test_apps_choose_where_their_tables_live(two_shards: None) -> None:
    """Tables of domains go to their target; untagged ones to default."""
    with override_settings(
        INSTALLED_DOMAINS=[
            ALPHA,
            "domainkit.contrib.messaging",
            "domainkit.contrib.audit",
            "domainkit.contrib.idempotency",
        ],
    ):
        setup()

        assert router.holds_tables("idempotency", "default")
        assert not router.holds_tables("idempotency", "tenants")
        assert router.holds_tables("alpha", "tenants")
        assert not router.holds_tables("alpha", "default")
        assert router.holds_tables("messaging", "tenants")
        assert router.holds_tables("audit", "default")
        assert router.holds_tables(None, "default")
        assert not router.holds_tables(None, "tenants")
        assert not router.holds_tables("not_installed", "default")
        assert router.migration_targets() == ("default", "tenants")


def test_without_databases_nothing_is_migrated() -> None:
    """A project without DATABASES has no migration targets."""
    with override_settings(DATABASES={}, ALPHA=UNSET):
        assert router.migration_targets() == ()


@pytest.mark.anyio
async def test_for_each_shard_runs_everywhere(two_shards: None) -> None:
    """The function runs once per database of the group."""

    async def describe(alias: str) -> str:
        return connections.config(alias).URL.get_secret_value()

    assert await for_each_shard("tenants", describe) == {
        "shard_a": SQLITE,
        "shard_b": SQLITE,
    }


# Units of work -----------------------------------------------------------------


def test_units_of_work_follow_the_routing(two_shards: None) -> None:
    """A unit of work defined in a domain opens on that domain's target."""
    setup()
    alpha_uow = type(
        "AlphaUnitOfWork", (NotesUnitOfWork,), {"__module__": f"{ALPHA}.infrastructure"}
    )

    keyed = alpha_uow(shard_key="tenant-1")
    pinned = NotesUnitOfWork(database="shard_b")

    assert (keyed.database, keyed.shard_key) == ("tenants", "tenant-1")
    assert keyed.alias == router.alias_for("tenants", "tenant-1")
    assert (pinned.database, pinned.alias) == ("shard_b", "shard_b")
    assert NotesUnitOfWork().alias == "default"
    with pytest.raises(ImproperlyConfigured, match="pass shard_key"):
        alpha_uow()


def test_class_level_databases_still_work(two_shards: None) -> None:
    """Subclasses that set ``database`` keep using it."""

    class ShardUnitOfWork(NotesUnitOfWork):
        database = "tenants"

    uow = ShardUnitOfWork(shard_key=42)

    assert uow.alias == router.shards("tenants").alias_for(42)
    assert settings.DATABASE_ROUTING == {"alpha": "tenants"}

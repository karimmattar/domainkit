"""Installed domains: configuration classes, the registry, and domain settings."""

import asyncio

import pytest

from domainkit import setup
from domainkit.apps import DomainConfig, DomainRegistry, registry
from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakePackage
from domainkit_tests.sample_domains.alpha.apps import AlphaConfig, AlphaSettings

ALPHA = "domainkit_tests.sample_domains.alpha"
BETA = "domainkit_tests.sample_domains.beta"


def test_package_without_apps_module_gets_the_base_config() -> None:
    """A plain package is a domain with default hooks."""
    config = DomainConfig.create(BETA)

    assert type(config) is DomainConfig
    assert (config.label, config.verbose_name) == ("beta", "Beta")
    assert config.settings_name == "BETA"
    assert config.path.name == "beta"
    assert repr(config) == "<DomainConfig: beta>"


def test_apps_module_provides_the_config() -> None:
    """The single ``DomainConfig`` subclass in ``apps`` is used."""
    config = DomainConfig.create(ALPHA)

    assert isinstance(config, AlphaConfig)
    assert config.verbose_name == "Alpha sample"


@pytest.mark.parametrize(
    ("apps", "error", "message"),
    [
        (
            """
            from domainkit.apps import DomainConfig
            class One(DomainConfig): pass
            class Two(DomainConfig): pass
            """,
            ImproperlyConfigured,
            "exactly one DomainConfig subclass, found 2",
        ),
        (
            "from domainkit.apps import DomainConfig\n",
            ImproperlyConfigured,
            "found 0",
        ),
        (
            """
            from domainkit.apps import DomainConfig
            class Bad(DomainConfig):
                label = "not-valid"
            """,
            ImproperlyConfigured,
            "not a valid Python identifier",
        ),
        ("import apps_dependency_that_is_missing\n", ModuleNotFoundError, "missing"),
    ],
)
def test_invalid_apps_modules(
    make_package: MakePackage, apps: str, error: type[Exception], message: str
) -> None:
    """Ambiguous or broken ``apps`` modules are reported, not guessed."""
    make_package("apps_case", {"apps.py": apps})

    with pytest.raises(error, match=message):
        DomainConfig.create("apps_case")


def test_uninstallable_domains(make_package: MakePackage) -> None:
    """Missing packages, broken packages, and plain modules are rejected."""
    root = make_package("apps_errors", {"broken/__init__.py": "import gone_module\n"})
    (root / "single.py").write_text("", encoding="utf-8")

    with pytest.raises(ImproperlyConfigured, match="cannot be imported"):
        DomainConfig.create("apps_errors.absent")
    with pytest.raises(ModuleNotFoundError):
        DomainConfig.create("apps_errors.broken")
    with pytest.raises(ImproperlyConfigured, match="must be a package"):
        DomainConfig.create("apps_errors.single")


def test_registry_populates_in_order() -> None:
    """Domains load in settings order, get ready once, and can be looked up."""
    domains = DomainRegistry()
    with override_settings(INSTALLED_DOMAINS=[ALPHA, BETA]):
        domains.populate([ALPHA, BETA])
        domains.populate([ALPHA, BETA])

    assert [config.label for config in domains] == ["alpha", "beta"]
    assert len(domains) == 2
    assert AlphaConfig.calls == ["ready"]
    assert domains.get_domain_config("beta").name == BETA
    assert domains.is_installed(ALPHA)
    assert not domains.is_installed("elsewhere")
    with pytest.raises(LookupError, match="gamma"):
        domains.get_domain_config("gamma")

    domains.clear()
    assert (len(domains), domains.ready) == (0, False)


def test_duplicate_labels_are_rejected(make_package: MakePackage) -> None:
    """Two packages ending in the same name need distinct labels."""
    make_package("other_place.beta", {})

    with pytest.raises(ImproperlyConfigured, match="'beta' is not unique"):
        DomainRegistry().populate([BETA, "other_place.beta"])


def test_domain_settings_are_validated_and_cached() -> None:
    """``registry.settings`` validates once per settings object."""
    setup()

    first = registry.settings(AlphaSettings)
    assert first.GREETING == "hi"
    assert registry.settings(AlphaSettings) is first
    with override_settings(ALPHA={"GREETING": "hey"}):
        assert registry.settings(AlphaSettings).GREETING == "hey"
    with override_settings(ALPHA=None):
        assert registry.settings(AlphaSettings).GREETING == "hello"


def test_domain_settings_errors() -> None:
    """Invalid or undeclared domain settings fail with the setting's name."""
    with (
        override_settings(ALPHA={"GREETING": 1}),
        pytest.raises(
            ImproperlyConfigured, match=r"Invalid ALPHA settings:\n  GREETING"
        ),
    ):
        DomainRegistry().populate([ALPHA])

    domains = DomainRegistry()
    with override_settings(ALPHA=None):
        domains.populate([BETA, ALPHA])
    domains.clear()
    with pytest.raises(ImproperlyConfigured, match="No installed domain declares"):
        domains.settings(AlphaSettings)


def test_unknown_settings_are_rejected() -> None:
    """Names nothing declares fail at startup, with a suggestion when close."""
    with (
        override_settings(DEBUGG=True, ZZZ=1),
        pytest.raises(ImproperlyConfigured) as caught,
    ):
        DomainRegistry().populate([ALPHA])
    assert "DEBUGG (did you mean DEBUG?)" in str(caught.value)
    assert ", ZZZ." in str(caught.value)

    with pytest.raises(ImproperlyConfigured, match="ALPHA"):
        DomainRegistry().populate([BETA])


def test_lifespan_starts_in_order_and_stops_in_reverse(
    make_package: MakePackage,
) -> None:
    """A failing startup still stops the domains that already started."""
    make_package(
        "late",
        {
            "apps.py": """
                from domainkit.apps import DomainConfig
                from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

                class LateConfig(DomainConfig):
                    async def startup(self) -> None:
                        AlphaConfig.calls.append("late startup")
                        raise RuntimeError("late failed")
            """
        },
    )
    domains = DomainRegistry()
    domains.populate([ALPHA, "late"])

    async def run() -> None:
        async with domains.lifespan():
            pass

    with pytest.raises(RuntimeError, match="late failed"):
        asyncio.run(run())
    assert AlphaConfig.calls == ["ready", "startup", "late startup", "shutdown"]


def test_readiness_reports_failing_domains(caplog: pytest.LogCaptureFixture) -> None:
    """Each failing or slow check is reported by label and logged."""
    domains = DomainRegistry()
    domains.populate([ALPHA, BETA])

    assert asyncio.run(domains.check(1.0)) == []
    AlphaConfig.healthy = False
    assert asyncio.run(domains.check(1.0)) == ["alpha"]
    assert "Readiness check 'alpha' failed" in caplog.text


def test_slow_checks_time_out(make_package: MakePackage) -> None:
    """A check that exceeds its time limit counts as failed."""
    make_package(
        "slow",
        {
            "apps.py": """
                import asyncio
                from domainkit.apps import DomainConfig

                class SlowConfig(DomainConfig):
                    async def check(self) -> None:
                        await asyncio.sleep(1)
            """
        },
    )
    domains = DomainRegistry()
    domains.populate([ALPHA, "slow"])

    assert asyncio.run(domains.check(0.01)) == ["slow"]


def test_setup_is_idempotent() -> None:
    """``domainkit.setup()`` loads the configured domains once."""
    setup()
    setup()

    assert [config.label for config in registry] == ["alpha"]
    assert AlphaConfig.calls == ["ready"]

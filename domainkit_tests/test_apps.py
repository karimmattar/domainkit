"""Installed domains: configuration classes, the registry, and domain settings."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import BaseModel

from domainkit import setup
from domainkit.apps import DomainConfig, DomainRegistry, Requirement, registry
from domainkit.conf import configure, override_settings, use_settings
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


class Validations:
    """Count ``AlphaSettings`` validations, and run ``during`` inside the next one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap ``AlphaSettings.model_validate``."""
        self.count = 0
        self.during: Callable[[], None] | None = None
        validate = AlphaSettings.model_validate

        def counted(value: object) -> AlphaSettings:
            self.count += 1
            if self.during is not None:
                during, self.during = self.during, None
                during()
            return validate(value)

        monkeypatch.setattr(AlphaSettings, "model_validate", counted)


def test_domain_settings_are_found_without_validating_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each settings object is validated from once; replacing it is noticed."""
    setup()
    validations = Validations(monkeypatch)

    slow: list[type] = []
    validate = registry._validate_settings

    def looked_up(settings_class: type[BaseModel]) -> BaseModel:
        slow.append(settings_class)
        return validate(settings_class)

    monkeypatch.setattr(registry, "_validate_settings", looked_up)

    first = registry.settings(AlphaSettings)  # Validated as ``setup`` began.
    assert [registry.settings(AlphaSettings) for _ in range(3)] == [first] * 3
    assert (validations.count, slow) == (0, [])
    with override_settings(ALPHA={"GREETING": "hey"}):
        assert registry.settings(AlphaSettings).GREETING == "hey"
        assert registry.settings(AlphaSettings).GREETING == "hey"
    assert validations.count == 1
    back = registry.settings(AlphaSettings)
    assert (back, validations.count) == (first, 2)
    with override_settings(DEBUG=True):  # ALPHA's mapping is the same object.
        assert registry.settings(AlphaSettings) is back
    assert validations.count == 2
    with use_settings("domainkit_tests.other_settings"):
        assert registry.settings(AlphaSettings).GREETING == "hello"
    configure("domainkit_tests.settings")
    assert registry.settings(AlphaSettings).GREETING == "hi"
    assert validations.count == 4


def test_domain_settings_are_validated_again_after_the_domains_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``clear`` and ``populate`` forget what was validated, and count a change."""
    setup()
    validations = Validations(monkeypatch)
    before = registry.generation

    first = registry.settings(AlphaSettings)
    registry.clear()
    assert registry.generation > before
    with pytest.raises(ImproperlyConfigured, match="No installed domain declares"):
        registry.settings(AlphaSettings)
    setup()  # Validates the settings again.
    again = registry.settings(AlphaSettings)

    assert (again, validations.count) == (first, 1)
    assert registry.settings(AlphaSettings) is again
    assert validations.count == 1


def test_settings_validated_while_the_domains_change_are_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value worked out across a ``clear`` is returned, not remembered."""
    setup()
    validations = Validations(monkeypatch)
    registry.settings(AlphaSettings)
    with override_settings(ALPHA={"GREETING": "hey"}):
        validations.during = lambda: registry._changed(dict(registry._configs))

        assert registry.settings(AlphaSettings).GREETING == "hey"
        assert registry.settings(AlphaSettings).GREETING == "hey"
        assert registry.settings(AlphaSettings).GREETING == "hey"
    assert validations.count == 2


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


CONTRIB_APPS = {
    "domainkit.contrib.audit": (),
    "domainkit.contrib.auth": ("domainkit.contrib.contenttypes",),
    "domainkit.contrib.contenttypes": (),
    "domainkit.contrib.evidence": (),
    "domainkit.contrib.flags": (),
    "domainkit.contrib.idempotency": (),
    "domainkit.contrib.mail": ("domainkit.contrib.messaging",),
    "domainkit.contrib.messaging": (),
    "domainkit.contrib.notifications": ("domainkit.contrib.messaging",),
    "domainkit.contrib.objectpermissions": (),
    "domainkit.contrib.privacy": ("domainkit.contrib.audit",),
    "domainkit.contrib.webhooks": ("domainkit.contrib.messaging",),
}
"""Every installable contrib app, and the apps it cannot work without."""


def requiring(make_package: MakePackage, name: str, requires: str) -> None:
    """Write domain ``name`` whose config declares ``requires`` (Python source)."""
    make_package(
        name,
        {
            "apps.py": f"""
                from domainkit.apps import DomainConfig, Requirement
                from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

                class NeedyConfig(DomainConfig):
                    requires = {requires}

                    def ready(self) -> None:
                        AlphaConfig.calls.append("needy ready")
            """
        },
    )


def test_a_missing_requirement_is_refused_before_any_domain_is_ready(
    make_package: MakePackage,
) -> None:
    """``setup`` names the package and the setting; no ``ready()`` has run."""
    requiring(make_package, "needy", '("domainkit_tests.sample_domains.beta",)')
    domains = DomainRegistry()
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "needy"]),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        domains.populate([ALPHA, "needy"])

    assert str(raised.value) == (
        "needy needs 'domainkit_tests.sample_domains.beta' in INSTALLED_DOMAINS."
    )
    assert AlphaConfig.calls == []
    assert not domains.ready


def test_every_missing_requirement_is_named_with_its_reason(
    make_package: MakePackage,
) -> None:
    """One error lists each missing package, in ``INSTALLED_DOMAINS`` order."""
    requiring(
        make_package,
        "needy",
        f'(Requirement("gone.first", "it keeps the rows"), "gone.second", "{ALPHA}")',
    )
    with (
        override_settings(INSTALLED_DOMAINS=["needy", ALPHA]),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        DomainRegistry().populate(["needy", ALPHA])

    assert str(raised.value) == (
        "needy needs 'gone.first' in INSTALLED_DOMAINS: it keeps the rows. "
        "needy needs 'gone.second' in INSTALLED_DOMAINS."
    )


def test_a_requirement_installed_in_any_order_is_met(
    make_package: MakePackage,
) -> None:
    """Order does not matter: a requirement after its dependant is enough."""
    requiring(make_package, "needy", f'("{ALPHA}",)')
    domains = DomainRegistry()
    with override_settings(INSTALLED_DOMAINS=["needy", ALPHA]):
        domains.populate(["needy", ALPHA])

    assert domains.ready
    assert AlphaConfig.calls == ["needy ready", "ready"]


def test_requires_may_list_up_to_the_bound(make_package: MakePackage) -> None:
    """Thirty-two requirements are checked as such, not refused as too many."""
    requiring(make_package, "needy", "tuple(f'pkg{n}' for n in range(32))")
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "needy"]),
        pytest.raises(ImproperlyConfigured, match="needy needs 'pkg0' in"),
    ):
        DomainRegistry().populate([ALPHA, "needy"])


@pytest.mark.parametrize(
    ("requires", "message"),
    [
        ("(3,)", "NeedyConfig.requires takes package names or Requirement, not 3"),
        ('("not a package",)', "names 'not a package', which is not a package name"),
        ('(Requirement(""),)', "names '', which is not a package name"),
        ('("needy",)', "NeedyConfig.requires names the domain itself"),
        (
            "tuple(f'pkg{n}' for n in range(33))",
            "NeedyConfig.requires lists 33 domains; at most 32",
        ),
    ],
)
def test_requires_is_validated(
    make_package: MakePackage, requires: str, message: str
) -> None:
    """Entries are package names, other than the domain's, and few."""
    requiring(make_package, "needy", requires)
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "needy"]),
        pytest.raises(ImproperlyConfigured, match=message),
    ):
        DomainRegistry().populate([ALPHA, "needy"])


def test_every_contrib_app_declares_what_it_requires() -> None:
    """Each app states its requirements itself, and they are installable apps."""
    for name, required in CONTRIB_APPS.items():
        config = DomainConfig.create(name)
        assert "requires" in vars(type(config)), name
        assert [r.name for r in config.requirements()] == list(required), name
        assert set(required) <= set(CONTRIB_APPS)


def test_requirements_read_as_one_sentence_each() -> None:
    """A bare name and a reasoned requirement, as errors show them."""
    assert Requirement.of("pkg") == Requirement("pkg")
    assert Requirement.of(Requirement("pkg", "why")).reason == "why"
    assert Requirement("pkg").problem("app") == "app needs 'pkg' in INSTALLED_DOMAINS."
    assert Requirement("pkg", "it needs it").problem("app") == (
        "app needs 'pkg' in INSTALLED_DOMAINS: it needs it."
    )

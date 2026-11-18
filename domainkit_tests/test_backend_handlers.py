"""Settings that name backend classes are loaded one way: ``BackendHandler``.

A28-ARCH-09. Six loaders read such settings by hand, each with its own
checks and lifetime. ``RATE_LIMIT_BACKEND`` was imported by five, with no
check of what it named: a class without ``hit`` passed settings and
``check``, and failed on the first request. Names that installed packages
offer worked for some settings, not for ``AUTH.BACKENDS`` or
``RATE_LIMIT_BACKEND``. ``BackendHandler`` imports by path or by name,
checks the kind, and makes a setting's instances once per settings
object (ADR 0196).
"""

from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from typing import Any, ClassVar, Final

import pytest
from fastapi.testclient import TestClient

from domainkit.checks import CheckMessage
from domainkit.conf import Settings, override_settings
from domainkit.conf.backends import BackendHandler
from domainkit.contrib.auth.apps import _counted_per_process
from domainkit.contrib.auth.backends import ModelBackend, get_backends
from domainkit.contrib.auth.settings import auth_settings
from domainkit.contrib.auth.validators import get_password_validators
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.application import application_problems, backend_problems
from domainkit.http.ratelimit import (
    Backend,
    Decision,
    MemoryBackend,
    RedisBackend,
    rate_limit_backend,
)
from domainkit.utils.module_loading import backend_class
from domainkit_tests.conftest import UseAuth
from domainkit_tests.test_custom_backends import offer

HERE: Final = __name__
LIMITED: Final = {
    "RATE_LIMIT": "100/minute",
    "MIDDLEWARE": [
        "domainkit.http.middleware.RequestContextMiddleware",
        "domainkit.http.ratelimit.RateLimitMiddleware",
    ],
}
"""Settings that count every request with ``RATE_LIMIT_BACKEND``."""


class Counted:
    """A rate-limit backend of the project's own, counting how many were made."""

    made: ClassVar[int] = 0

    def __init__(self, *, step: int = 1) -> None:
        """Count this one, by ``step``."""
        Counted.made += step
        self.step = step

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Let everyone through."""
        return Decision(allowed=True, remaining=limit, reset_after=0.0, retry_after=0.0)


class NoHelp:
    """A password rule without ``help_text``: what a person is told is missing."""

    def validate(self, password: str, user: object | None = None) -> None:
        """Take every password."""


def handler(
    read: Any = None,  # noqa: ANN401 - what a setting holds.
) -> BackendHandler[Backend]:
    """A handler of rate-limit backends, reading ``read()`` or the setting."""
    return BackendHandler("RATE_LIMIT_BACKEND", Backend, read)


def test_a_class_is_loaded_by_dotted_path_or_by_the_name_a_package_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``module.Class``, ``module:Class``, and a name of the entry points."""
    offer(tmp_path, "counting", {"counted": f"{HERE}:Counted"})
    monkeypatch.syspath_prepend(str(tmp_path))

    for named in (f"{HERE}.Counted", f"{HERE}:Counted", "counted"):
        with override_settings(RATE_LIMIT_BACKEND=named):
            assert rate_limit_backend.backend_class() is Counted
            assert isinstance(rate_limit_backend.create(), Counted)
    assert backend_class("X", f"{HERE}.Counted", Backend) is Counted


def test_what_is_not_of_its_kind_is_refused_by_setting() -> None:
    """A path that imports nothing, or a class without the kind's methods."""
    with pytest.raises(
        ImproperlyConfigured, match=r"^RATE_LIMIT_BACKEND: No module named 'nowhere'$"
    ):
        handler(lambda: "nowhere.Backend").create()
    with pytest.raises(
        ImproperlyConfigured,
        match=r"^RATE_LIMIT_BACKEND: 'collections.OrderedDict' is not a Backend\.$",
    ):
        handler(lambda: "collections.OrderedDict").backend_class()
    with pytest.raises(ImproperlyConfigured, match="is not a Backend"):
        handler(lambda: f"{HERE}.NOT_A_CLASS").create()
    with pytest.raises(ImproperlyConfigured, match=r"^RATE_LIMIT_BACKEND: .*stride"):
        handler(lambda: [(f"{HERE}.Counted", {"stride": 2})]).instances()


def test_instances_are_made_once_per_settings_object() -> None:
    """The same until the settings change; made again for the new ones."""
    Counted.made = 0
    several = handler(lambda: [f"{HERE}.Counted", (f"{HERE}.Counted", {"step": 2})])

    first = several.instances()
    assert several.instances() is first
    assert [each.step for each in first] == [1, 2]  # type: ignore[attr-defined]
    assert Counted.made == 3
    with override_settings(DEBUG=False):
        overridden = several.instances()
    assert overridden is not first
    assert several.instances() not in {first, overridden}  # One kept at a time.
    assert Counted.made == 9


def test_instances_made_while_the_settings_change_are_not_kept() -> None:
    """Made for settings no longer in place, they would outlive them."""
    with ExitStack() as stack:

        def read() -> str:
            stack.enter_context(override_settings(DEBUG=False))
            return f"{HERE}.Counted"

        changing = handler(read)
        made = changing.instances()

        assert changing.instances() is not made


def test_each_call_of_create_makes_a_new_backend() -> None:
    """What counts keeps its own counts: nothing is kept."""
    with override_settings(RATE_LIMIT_BACKEND=f"{HERE}.Counted"):
        first, second = rate_limit_backend.create(), rate_limit_backend.create()

    assert first is not second
    assert isinstance(rate_limit_backend.create(), MemoryBackend)


def test_rate_limit_backend_is_a_path_or_a_name_when_the_settings_load() -> None:
    """A value of no form that could load is refused with the settings."""
    for named in (
        "domainkit.http.ratelimit.RedisBackend",
        "domainkit.http.ratelimit:RedisBackend",
        "redis-limits",
    ):
        assert Settings.model_validate({"RATE_LIMIT_BACKEND": named})
    for wrong in ("", "Redis Backend", ".ratelimit.Backend", "ratelimit.", "a..b"):
        with pytest.raises(ValueError, match="RATE_LIMIT_BACKEND"):
            Settings.model_validate({"RATE_LIMIT_BACKEND": wrong})


def test_a_rate_limit_backend_without_hit_is_a_problem_of_the_application() -> None:
    """``check`` and the application's start report it (``backends.E001``)."""
    assert backend_problems() == []
    with override_settings(RATE_LIMIT_BACKEND="collections.OrderedDict"):
        problems = application_problems(get_asgi_application())

    (problem,) = [
        each for each in problems if getattr(each, "id", None) == "backends.E001"
    ]
    assert isinstance(problem, CheckMessage)
    assert str(problem) == (
        "RATE_LIMIT_BACKEND: 'collections.OrderedDict' is not a Backend."
    )
    assert "RedisBackend" in problem.hint


def test_the_limit_refuses_to_count_with_what_is_not_a_backend() -> None:
    """The first request is refused with what is wrong, not an error of ``hit``."""
    with override_settings(**LIMITED, RATE_LIMIT_BACKEND="collections.OrderedDict"):
        client = TestClient(get_asgi_application())
        with pytest.raises(ImproperlyConfigured, match="is not a Backend"):
            client.get("/ok")
    assert issubclass(MemoryBackend, Backend)
    assert issubclass(RedisBackend, Backend)
    assert not issubclass(OrderedDict, Backend)


def test_a_rate_limit_backend_a_package_offers_counts_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RATE_LIMIT_BACKEND = "counted"``: the class the package names."""
    offer(tmp_path, "counting", {"counted": f"{HERE}:Counted"})
    monkeypatch.syspath_prepend(str(tmp_path))
    Counted.made = 0

    with override_settings(**LIMITED, RATE_LIMIT_BACKEND="counted"):
        response = TestClient(get_asgi_application()).get("/ok")

    assert response.headers["RateLimit-Limit"] == "100"
    assert Counted.made == 1


def test_auth_loads_its_classes_by_the_names_packages_offer(
    use_auth: UseAuth, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AUTH.BACKENDS = ["models"]``, made once for the settings."""
    offer(
        tmp_path,
        "authing",
        {"models": "domainkit.contrib.auth.backends:ModelBackend"},
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    use_auth(BACKENDS=["models"])
    (backend,) = get_backends()

    assert isinstance(backend, ModelBackend)
    assert get_backends()[0] is backend
    assert auth_settings().BACKENDS == ("models",)


def test_a_password_rule_without_help_text_is_refused_at_startup(
    use_auth: UseAuth,
) -> None:
    """Its help is what ``/password/rules`` tells a person: it must have one."""
    with pytest.raises(
        ImproperlyConfigured,
        match=rf"^AUTH\.PASSWORD_VALIDATORS: '{HERE}\.NoHelp' is not a "
        r"PasswordValidator\.$",
    ):
        use_auth(PASSWORD_VALIDATORS=[{"NAME": f"{HERE}.NoHelp"}])
    use_auth(PASSWORD_VALIDATORS=[])
    assert get_password_validators() == ()


def test_attempts_counted_per_process_follow_the_backend_named(
    use_auth: UseAuth,
) -> None:
    """A memory backend counts in each process; one that does not load is ours."""
    use_auth(LOGIN_RATE="5/minute")
    config = auth_settings()

    assert _counted_per_process(config)
    with override_settings(RATE_LIMIT_BACKEND="domainkit.http.ratelimit.RedisBackend"):
        assert not _counted_per_process(config)
    with override_settings(RATE_LIMIT_BACKEND="nowhere.Backend"):
        assert not _counted_per_process(config)  # backends.E001 says so.


NOT_A_CLASS: Final = "not a class"
"""Something a path can name that is no class at all."""

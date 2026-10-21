"""Event loops: uvloop for commands and runserver when it is installed."""

import asyncio
import inspect
import sys
import types
from typing import Any, cast

import pytest

from domainkit.conf import override_settings, settings
from domainkit.conf.lazy import LazySettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit.utils.loops import loop_factory, run


class MarkedLoop(asyncio.SelectorEventLoop):
    """The loop the stand-in uvloop makes, so tests can tell it apart."""


@pytest.fixture
def uvloop_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in uvloop whose loops are ``MarkedLoop``."""
    module = types.ModuleType("uvloop")
    module.new_event_loop = MarkedLoop  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvloop", module)


@pytest.fixture
def uvloop_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The uvloop module cannot be imported."""
    monkeypatch.setitem(sys.modules, "uvloop", None)


async def loop_type() -> type[asyncio.AbstractEventLoop]:
    """The type of the loop running this coroutine."""
    return type(asyncio.get_running_loop())


@pytest.mark.usefixtures("uvloop_installed")
def test_auto_and_uvloop_take_uvloop_when_installed() -> None:
    """``auto`` and ``uvloop`` use uvloop's factory; ``asyncio`` never does."""
    assert loop_factory("auto") is MarkedLoop
    assert loop_factory("uvloop") is MarkedLoop
    assert loop_factory("asyncio") is None
    assert loop_factory() is MarkedLoop
    assert run(loop_type()) is MarkedLoop
    assert run(loop_type(), kind="asyncio") is not MarkedLoop


@pytest.mark.usefixtures("uvloop_missing")
def test_without_uvloop_auto_falls_back_and_uvloop_is_refused() -> None:
    """Without uvloop, ``auto`` is asyncio's loop and ``uvloop`` a config error."""
    assert loop_factory("auto") is None
    assert run(loop_type()) is not MarkedLoop
    with pytest.raises(ImproperlyConfigured) as refused:
        loop_factory("uvloop")
    assert str(refused.value) == (
        "EVENT_LOOP 'uvloop' needs the uvloop extra: pip install 'domainkit[uvloop]'."
    )

    main = loop_type()
    with pytest.raises(ImproperlyConfigured, match="uvloop extra"):
        run(main, kind="uvloop")
    # Closed unstarted, so it never warns that it was not awaited.
    assert inspect.getcoroutinestate(main) == inspect.CORO_CLOSED


@pytest.mark.usefixtures("uvloop_installed")
def test_commands_run_on_the_loop_event_loop_names() -> None:
    """``run_async`` follows ``EVENT_LOOP``; uvloop when ``auto``."""
    assert run_async(loop_type()) is MarkedLoop
    with override_settings(EVENT_LOOP="asyncio"):
        assert run_async(loop_type()) is not MarkedLoop


@pytest.mark.usefixtures("uvloop_installed")
def test_commands_before_settings_load_take_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without loaded settings, ``run_async`` loads none to pick a loop."""
    with override_settings(EVENT_LOOP="asyncio"):
        monkeypatch.setattr(settings, "_wrapped", None)
        assert run_async(loop_type()) is MarkedLoop
        assert not cast(LazySettings, settings).configured


@pytest.mark.usefixtures("uvloop_missing")
def test_runserver_refuses_a_loop_that_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EVENT_LOOP = "uvloop"`` without uvloop fails before uvicorn starts."""
    started: list[Any] = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: started.append(args))

    with (
        override_settings(ASGI_APPLICATION="config.asgi:application"),
        pytest.raises(ImproperlyConfigured, match="uvloop extra"),
        override_settings(EVENT_LOOP="uvloop"),
    ):
        call_command("runserver", "8001")

    assert started == []


@pytest.mark.parametrize("kind", ["auto", "asyncio"])
def test_runserver_hands_uvicorn_the_loop(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Uvicorn picks the same loop as the commands: ``loop`` is ``EVENT_LOOP``."""
    options: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: options.append(kwargs))

    with override_settings(ASGI_APPLICATION="config.asgi:application", EVENT_LOOP=kind):
        call_command("runserver", "8001", reload=False)

    assert options[0]["loop"] == kind

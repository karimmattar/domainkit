"""``phone_identity``: the settings of the phone as identity, for a settings file."""

import subprocess
import sys

from domainkit.conf import override_settings
from domainkit.contrib.auth.settings import AuthSettings
from domainkit.contrib.ciba.settings import CibaSettings
from domainkit.contrib.devices.settings import DevicesSettings
from domainkit.contrib.presets import phone_identity
from domainkit_tests.test_ciba import App, app

__all__ = ["app"]  # The fixture the tests take.


def test_it_sets_what_goes_together_and_the_projects_keys_win() -> None:
    """Each app's settings validate, and a project's own choice stays."""
    auth, devices, ciba = phone_identity()
    strict = phone_identity(
        strict=True,
        auth={"IDENTIFIERS": ("email",), "CREDENTIAL_CHANGE_DEVICE_LOGINS": "ask"},
        devices={"MAX_DEVICES": 2},
        ciba={"NUMBER_MATCHING": False},
    )

    assert AuthSettings(**auth).CREDENTIAL_CHANGE_DEVICE_LOGINS == "keep"
    assert DevicesSettings(**devices).ENROLLMENT is True
    assert DevicesSettings(**devices).DEVICE_LOGIN is True
    assert CibaSettings(**ciba).APPROVE_LOGINS is True
    assert strict.AUTH == {
        "CREDENTIAL_CHANGE_DEVICE_LOGINS": "ask",
        "IDENTIFIERS": ("email",),
    }
    assert strict.DEVICES == {
        "ENROLLMENT": True,
        "DEVICE_LOGIN": True,
        "MAX_DEVICES": 2,
    }
    assert strict.CIBA == {"APPROVE_LOGINS": "always", "NUMBER_MATCHING": False}


def test_with_it_the_phone_modes_checks_are_quiet(app: App) -> None:
    """No ``ciba.E005`` (enrollment) and no ``ciba.W001`` (a change signs out)."""
    from fastapi import FastAPI

    from domainkit.apps import registry
    from domainkit.contrib.ciba.api import router as sign_in_router
    from domainkit.contrib.devices.api import router as devices_router

    app()
    config = registry.get_domain_config("ciba")
    served = FastAPI()
    served.include_router(sign_in_router)
    served.include_router(devices_router)
    auth, devices, ciba = phone_identity(strict=True)

    def found() -> set[str]:
        problems = config.application_problems(served)  # type: ignore[attr-defined]
        warnings = config.weak_settings()  # type: ignore[attr-defined]
        return {each.id for each in (*problems, *warnings)}

    with override_settings(AUTH=auth, DEVICES=devices, CIBA=ciba):
        assert not found() & {"ciba.E005", "ciba.W001"}
    with override_settings(CIBA=ciba):  # The same mode, set by hand alone.
        assert found() >= {"ciba.E005", "ciba.W001"}


def test_a_settings_file_imports_it_without_loading_an_app() -> None:
    """Settings are read before the apps: no SQLAlchemy, no FastAPI, no app."""
    code = (
        "import sys; import domainkit.contrib.presets; "
        "print([name for name in ('sqlalchemy', 'fastapi', 'domainkit.contrib.auth', "
        "'domainkit.contrib.ciba') if name in sys.modules])"
    )

    loaded = subprocess.run(  # noqa: S603 - this interpreter, a fixed script.
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert loaded.stdout.strip() == "[]"

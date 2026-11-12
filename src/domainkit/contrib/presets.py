"""Settings of several apps that go together, for a project's settings file.

A settings file is read before any app is loaded: this module imports
nothing of them. ``phone_identity`` gives the settings of a project whose
users' phone is their identity (``docs/auth/ciba.md``)::

    from domainkit.contrib.presets import phone_identity

    AUTH, DEVICES, CIBA = phone_identity(auth={"IDENTIFIERS": ("email",)})
"""

from collections.abc import Mapping
from typing import NamedTuple

__all__ = ["PhoneIdentity", "phone_identity"]


class PhoneIdentity(NamedTuple):
    """``AUTH``, ``DEVICES`` and ``CIBA`` for the phone as identity."""

    AUTH: dict[str, object]
    DEVICES: dict[str, object]
    CIBA: dict[str, object]


def phone_identity(
    *,
    strict: bool = False,
    auth: Mapping[str, object] | None = None,
    devices: Mapping[str, object] | None = None,
    ciba: Mapping[str, object] | None = None,
) -> PhoneIdentity:
    """The settings that make a user's bound phone their identity.

    What it sets, each unless the project's own mapping does:

    - ``CIBA.APPROVE_LOGINS``: True, or ``"always"`` with ``strict``. A
      user who bound a phone logs in from it, or once it approves; with
      ``strict``, nobody logs in without one.
    - ``DEVICES.ENROLLMENT`` and ``DEVICES.DEVICE_LOGIN``: a first phone
      enrolls with no login first, and a bound phone logs in by its key.
    - ``AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS``: ``"keep"``. A password
      change, a reset, or turning the second factor on does not sign the
      phone out.

    ``auth``, ``devices`` and ``ciba`` are the project's own settings of
    each app: their keys win. Install ``domainkit.contrib.messaging``,
    ``mail``, ``notifications``, ``devices`` and ``ciba`` too.
    """
    return PhoneIdentity(
        AUTH={"CREDENTIAL_CHANGE_DEVICE_LOGINS": "keep", **(auth or {})},
        DEVICES={"ENROLLMENT": True, "DEVICE_LOGIN": True, **(devices or {})},
        CIBA={"APPROVE_LOGINS": "always" if strict else True, **(ciba or {})},
    )

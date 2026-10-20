"""Feature flags and dynamic settings, stored in the database and cached.

A flag is on for everyone, for no one, or for some users: those listed, the
members of listed groups, and a percentage of the rest, picked by a stable
hash of the flag and the user. Check one anywhere, or guard a route::

    if await flag_enabled("new_checkout", user=user): ...

    @router.get("/beta", dependencies=[Depends(RequireFlag("beta")), Public])

A dynamic setting is a typed value staff change at runtime, validated by
its type::

    MAX_ITEMS = dynamic_setting(
        "checkout.max_items", Annotated[int, Field(ge=1, le=100)], default=10
    )
    limit = await MAX_ITEMS.get()

Change them with ``manage.py flags`` or the staff API (``admin``); each
change is a domain event, which the audit trail records, and drops the
cached copy (``FLAGS.CACHE``).
"""

from .dependencies import RequireFlag
from .events import FlagChanged, FlagDeleted, SettingChanged, SettingReset
from .exceptions import FlagNotFound, InvalidSettingValue, UnknownSetting
from .models import DynamicSetting, Flag, InvalidFlag
from .settings import FlagsSettings
from .store import (
    DynamicSettingDefinition,
    declared_settings,
    dynamic_setting,
    flag_enabled,
)
from .uow import FlagsUnitOfWork

__all__ = [
    "DynamicSetting",
    "DynamicSettingDefinition",
    "Flag",
    "FlagChanged",
    "FlagDeleted",
    "FlagNotFound",
    "FlagsSettings",
    "FlagsUnitOfWork",
    "InvalidFlag",
    "InvalidSettingValue",
    "RequireFlag",
    "SettingChanged",
    "SettingReset",
    "UnknownSetting",
    "declared_settings",
    "dynamic_setting",
    "flag_enabled",
]

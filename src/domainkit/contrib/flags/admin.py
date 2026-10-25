"""Flags and dynamic settings over REST, for staff.

Mount it next to the auth admin API::

    from domainkit.contrib.flags.admin import router as flags_router

    router.include_router(flags_router, prefix="/admin/flags")

Every endpoint needs a staff user (or a superuser) with the model
permission of its action: ``flags.view_flag``, ``add_flag``,
``change_flag``, ``delete_flag``, and ``flags.view_dynamicsetting`` and
``change_dynamicsetting``. Changes are events, so the audit trail records
who changed what.
"""

from typing import Annotated, Any, Final, Self, cast

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from domainkit.contrib.auth.admin import staff
from domainkit.contrib.auth.models import AnyUser
from domainkit.http import error_responses

from .exceptions import FlagNotFound
from .models import DynamicSetting, Flag
from .store import NAME, DynamicSettingDefinition, declared, declared_settings
from .uow import FlagsUnitOfWork

router = APIRouter(tags=["flags admin"])

Name = Annotated[str, Path(pattern=f"^{NAME.pattern}$")]
UserId = Annotated[str, Field(min_length=1, max_length=255)]
GroupName = Annotated[str, Field(min_length=1, max_length=150)]
MAX_USERS: Final = 1000
MAX_GROUPS: Final = 100


class FlagRules(BaseModel):
    """Who a flag is on for: everyone, no one (``everyone`` set), or some users."""

    model_config = ConfigDict(extra="forbid")

    everyone: bool | None = None
    percentage: int = Field(default=0, ge=0, le=100)
    users: list[UserId] = Field(default_factory=list, max_length=MAX_USERS)
    """User ids the flag is on for."""
    groups: list[GroupName] = Field(default_factory=list, max_length=MAX_GROUPS)
    """Group names the flag is on for."""
    note: str = Field(default="", max_length=255)


class FlagIn(FlagRules):
    """A new flag."""

    name: str = Field(pattern=f"^{NAME.pattern}$")


class FlagOut(FlagRules):
    """A flag and its rules."""

    name: str

    @classmethod
    def of(cls, flag: Flag) -> Self:
        """Describe ``flag``."""
        return cls(
            name=flag.id,
            everyone=flag.everyone,
            percentage=flag.percentage,
            users=list(flag.users),
            groups=list(flag.groups),
            note=flag.note,
        )


class SettingIn(BaseModel):
    """A dynamic setting's new value."""

    model_config = ConfigDict(extra="forbid")

    value: JsonValue


class SettingOut(BaseModel):
    """A dynamic setting: its value, default, and the schema values follow.

    A ``secret`` setting shows None for its value and default.
    """

    key: str
    value: JsonValue
    default: JsonValue
    description: str
    json_schema: dict[str, Any]
    secret: bool = False

    @classmethod
    def of(
        cls, definition: DynamicSettingDefinition[Any], stored: DynamicSetting | None
    ) -> Self:
        """Describe ``definition`` with its ``stored`` value, if any."""
        default = cast(JsonValue, definition.default_json())
        value = default if stored is None else cast(JsonValue, stored.value)
        if definition.secret:
            default = value = None
        return cls(
            key=definition.key,
            value=value,
            default=default,
            description=definition.description,
            json_schema=definition.json_schema(),
            secret=definition.secret,
        )


_errors = error_responses
"""OpenAPI entries for the error statuses an endpoint returns."""


COMMON: Final = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
ONE: Final = (*COMMON, status.HTTP_404_NOT_FOUND)
CHANGE: Final = (*ONE, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT)


@router.get("/flags", responses=_errors(*COMMON))
async def list_flags(
    actor: Annotated[AnyUser, staff("view", lambda: Flag)],
) -> list[FlagOut]:
    """Every flag, by name."""
    async with FlagsUnitOfWork() as uow:
        return [FlagOut.of(flag) for flag in await uow.flags.all()]


@router.post("/flags", status_code=status.HTTP_201_CREATED, responses=_errors(*CHANGE))
async def create_flag(
    body: FlagIn, actor: Annotated[AnyUser, staff("add", lambda: Flag)]
) -> FlagOut:
    """Create a flag; a taken name is 409."""
    flag = Flag(id=body.name)
    flag.configure(**body.model_dump(exclude={"name"}))
    async with FlagsUnitOfWork() as uow:
        await uow.flags.add(flag)
        await uow.commit()
    return FlagOut.of(flag)


@router.put("/flags/{name}", responses=_errors(*CHANGE))
async def change_flag(
    name: Name,
    body: FlagRules,
    actor: Annotated[AnyUser, staff("change", lambda: Flag)],
) -> FlagOut:
    """Replace a flag's rules."""
    async with FlagsUnitOfWork() as uow:
        flag = await _flag(uow, name)
        flag.configure(**body.model_dump())
        await uow.commit()
    return FlagOut.of(flag)


@router.delete(
    "/flags/{name}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors(*ONE)
)
async def delete_flag(
    name: Name, actor: Annotated[AnyUser, staff("delete", lambda: Flag)]
) -> None:
    """Delete a flag: it is off for everyone."""
    async with FlagsUnitOfWork() as uow:
        flag = await _flag(uow, name)
        flag.delete()
        await uow.flags.remove(flag)
        await uow.commit()


@router.get("/settings", responses=_errors(*COMMON))
async def list_settings(
    actor: Annotated[AnyUser, staff("view", lambda: DynamicSetting)],
) -> list[SettingOut]:
    """Every declared dynamic setting, with its stored value or default."""
    async with FlagsUnitOfWork() as uow:
        found = []
        for definition in declared_settings():
            stored = await uow.settings.find(definition.key)
            found.append(SettingOut.of(definition, stored))
    return found


@router.put("/settings/{key}", responses=_errors(*CHANGE))
async def change_setting(
    key: Name,
    body: SettingIn,
    actor: Annotated[AnyUser, staff("change", lambda: DynamicSetting)],
) -> SettingOut:
    """Store a new value; one its type refuses is 422."""
    definition = declared(key)
    value = definition.checked(body.value)
    async with FlagsUnitOfWork() as uow:
        stored = await uow.settings.find(key)
        if stored is None:
            stored = DynamicSetting(id=key)
            await uow.settings.add(stored)
        stored.change(value, secret=definition.secret)
        await uow.commit()
    return SettingOut.of(definition, stored)


@router.delete(
    "/settings/{key}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors(*ONE)
)
async def reset_setting(
    key: Name, actor: Annotated[AnyUser, staff("change", lambda: DynamicSetting)]
) -> None:
    """Go back to the default."""
    declared(key)
    async with FlagsUnitOfWork() as uow:
        stored = await uow.settings.find(key)
        if stored is not None:
            stored.reset()
            await uow.settings.remove(stored)
            await uow.commit()


async def _flag(uow: FlagsUnitOfWork, name: str) -> Flag:
    """The flag ``name``.

    Raises:
        FlagNotFound: There is none.
    """
    flag = await uow.flags.find(name)
    if flag is None:
        raise FlagNotFound(f"No flag is called {name!r}.")
    return flag

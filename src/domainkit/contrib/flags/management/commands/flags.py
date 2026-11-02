"""``flags``: list and change feature flags and dynamic settings."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from domainkit.contrib.flags.exceptions import FlagNotFound
from domainkit.contrib.flags.models import DynamicSetting, Flag
from domainkit.contrib.flags.store import NAME, declared, declared_settings
from domainkit.contrib.flags.uow import FlagsUnitOfWork
from domainkit.ddd import DomainError
from domainkit.management import ActionCommand, CommandError, action, option

EVERYONE = {"on": True, "off": False, "rules": None}
"""``--everyone`` values: on or off for all, or decided by the rules."""
HIDDEN = "********"
"""What is shown of a secret setting's value."""


class Command(ActionCommand):
    """``list``, ``set``, ``delete`` flags; ``settings`` and ``setting`` values.

    Changes go through ``FlagsUnitOfWork``, so they are audited and drop
    the cached flags, as the staff API's do.
    """

    help = "List or change feature flags (set, delete) and dynamic settings."
    default_action = "list"

    @action(name="list", help="every flag and its rules (the default)")
    async def listed(self) -> None:
        """One line per flag."""
        with _refusals():
            await self._list()

    @action(
        name="set",
        help="create a flag or change its rules",
        options=[
            option("name"),
            option("--everyone", choices=sorted(EVERYONE)),
            option("--percentage", type=int),
            option("--users", nargs="*", metavar="USER_ID"),
            option("--groups", nargs="*", metavar="GROUP"),
            option("--note"),
        ],
        operator=True,
    )
    async def set_flag(self, **options: Any) -> None:  # noqa: ANN401 - the options.
        """Create the flag, or change the rules given."""
        with _refusals():
            await self._set(**options)

    @action(
        help="delete a flag (off for everyone)",
        options=[option("name")],
        operator=True,
    )
    async def delete(self, *, name: str) -> None:
        """Delete the flag."""
        with _refusals():
            await self._delete(name)

    @action(help="every dynamic setting and its value")
    async def settings(self) -> None:
        """One line per dynamic setting."""
        with _refusals():
            await self._settings()

    @action(
        help="set a dynamic setting (JSON)",
        options=[
            option("key"),
            option("value", nargs="?", help="the value, as JSON"),
            option("--reset", action="store_true", help="go back to the default"),
        ],
        operator=True,
    )
    async def setting(self, *, key: str, value: str | None, reset: bool) -> None:
        """Set the setting, or reset it."""
        with _refusals():
            await self._setting(key, value, reset)

    async def _list(self) -> None:
        """One line per flag."""
        async with FlagsUnitOfWork() as uow:
            found = await uow.flags.all()
        for flag in found:
            self.write(_describe(flag))
        if not found:
            self.write("No flags.")

    async def _set(
        self,
        *,
        name: str,
        everyone: str | None,
        percentage: int | None,
        users: list[str] | None,
        groups: list[str] | None,
        note: str | None,
        **options: object,
    ) -> None:
        """Create the flag, or change the rules given; the rest stay."""
        if not NAME.fullmatch(name):
            raise CommandError(f"{name!r} is not a flag name: {NAME.pattern}.")
        async with FlagsUnitOfWork() as uow:
            flag = await uow.flags.find(name)
            if flag is None:
                flag = Flag(id=name)
                await uow.flags.add(flag)
            flag.configure(
                everyone=flag.everyone if everyone is None else EVERYONE[everyone],
                percentage=flag.percentage if percentage is None else percentage,
                users=flag.users if users is None else users,
                groups=flag.groups if groups is None else groups,
                note=flag.note if note is None else note,
            )
            await uow.commit()
        self.write(_describe(flag))

    async def _delete(self, name: str) -> None:
        """Delete the flag."""
        async with FlagsUnitOfWork() as uow:
            flag = await uow.flags.find(name)
            if flag is None:
                raise FlagNotFound(f"No flag is called {name!r}.")
            flag.delete()
            await uow.flags.remove(flag)
            await uow.commit()
        self.write(f"Deleted {name}.")

    async def _settings(self) -> None:
        """One line per declared setting."""
        async with FlagsUnitOfWork() as uow:
            for definition in declared_settings():
                if definition.secret:
                    self.write(f"{definition.key} = {HIDDEN} (secret)")
                    continue
                stored = await uow.settings.find(definition.key)
                default = json.dumps(definition.default_json())
                value = default if stored is None else json.dumps(stored.value)
                self.write(f"{definition.key} = {value} (default {default})")
        if not declared_settings():
            self.write("No dynamic settings are declared.")

    async def _setting(self, key: str, value: str | None, reset: bool) -> None:
        """Store a value, or reset to the default."""
        definition = declared(key)
        if reset == (value is not None):
            raise CommandError("Give a value as JSON, or --reset.")
        async with FlagsUnitOfWork() as uow:
            stored = await uow.settings.find(key)
            if value is None:
                if stored is not None:
                    stored.reset()
                    await uow.settings.remove(stored)
                    await uow.commit()
                default = json.dumps(definition.default_json())
                self.write(
                    f"{key} is back to {HIDDEN if definition.secret else default}."
                )
                return
            try:
                parsed = json.loads(value)
            except ValueError:
                raise CommandError(f"The value of {key} is not JSON.") from None
            checked = definition.checked(parsed)
            if stored is None:
                stored = DynamicSetting(id=key)
                await uow.settings.add(stored)
            stored.change(checked, secret=definition.secret)
            await uow.commit()
        self.write(f"{key} = {HIDDEN if definition.secret else json.dumps(checked)}")


def _describe(flag: Flag) -> str:
    """One line for a flag."""
    if flag.everyone is not None:
        rule = "on for everyone" if flag.everyone else "off for everyone"
    else:
        rule = (
            f"{flag.percentage}% of users, users {list(flag.users)}, "
            f"groups {list(flag.groups)}"
        )
    note = f"  ({flag.note})" if flag.note else ""
    return f"{flag.id}: {rule}{note}"


@contextmanager
def _refusals() -> Iterator[None]:
    """Report a flag or setting the domain refuses as the command's error.

    Raises:
        CommandError: The domain refused.
    """
    try:
        yield
    except DomainError as exc:
        raise CommandError(exc.message) from None

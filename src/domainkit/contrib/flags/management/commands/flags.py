"""``flags``: list and change feature flags and dynamic settings."""

import json
from argparse import ArgumentParser
from typing import Any

from domainkit.contrib.flags.exceptions import FlagNotFound
from domainkit.contrib.flags.models import DynamicSetting, Flag
from domainkit.contrib.flags.store import NAME, declared, declared_settings
from domainkit.contrib.flags.uow import FlagsUnitOfWork
from domainkit.ddd import DomainError
from domainkit.management import BaseCommand, CommandError

EVERYONE = {"on": True, "off": False, "rules": None}
"""``--everyone`` values: on or off for all, or decided by the rules."""


class Command(BaseCommand):
    """``list``, ``set``, ``delete`` flags; ``settings`` and ``setting`` values.

    Changes go through ``FlagsUnitOfWork``, so they are audited and drop
    the cached flags, as the staff API's do.
    """

    help = "List or change feature flags (set, delete) and dynamic settings."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the actions and their options."""
        actions = parser.add_subparsers(dest="action")
        actions.add_parser("list", help="every flag and its rules")
        change = actions.add_parser("set", help="create a flag or change its rules")
        change.add_argument("name")
        change.add_argument("--everyone", choices=sorted(EVERYONE))
        change.add_argument("--percentage", type=int)
        change.add_argument("--users", nargs="*", metavar="USER_ID")
        change.add_argument("--groups", nargs="*", metavar="GROUP")
        change.add_argument("--note")
        remove = actions.add_parser("delete", help="delete a flag (off for everyone)")
        remove.add_argument("name")
        actions.add_parser("settings", help="every dynamic setting and its value")
        value = actions.add_parser("setting", help="set a dynamic setting (JSON)")
        value.add_argument("key")
        value.add_argument("value", nargs="?", help="the value, as JSON")
        value.add_argument(
            "--reset", action="store_true", help="go back to the default"
        )

    async def handle(self, *, action: str | None, **options: Any) -> None:  # noqa: ANN401
        """Run the action (``list`` by default)."""
        try:
            if action == "set":
                await self._set(**options)
            elif action == "delete":
                await self._delete(options["name"])
            elif action == "settings":
                await self._settings()
            elif action == "setting":
                await self._setting(options["key"], options["value"], options["reset"])
            else:
                await self._list()
        except DomainError as exc:
            raise CommandError(exc.message) from None

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
                self.write(f"{key} is back to {json.dumps(definition.default_json())}.")
                return
            try:
                parsed = json.loads(value)
            except ValueError:
                raise CommandError(f"The value of {key} is not JSON.") from None
            checked = definition.checked(parsed)
            if stored is None:
                stored = DynamicSetting(id=key)
                await uow.settings.add(stored)
            stored.change(checked)
            await uow.commit()
        self.write(f"{key} = {json.dumps(checked)}")


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

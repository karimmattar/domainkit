"""``createsuperuser`` and ``changepassword``."""

import asyncio
import getpass
import io
from collections.abc import Callable, Iterable

import pytest

from domainkit.conf.env import project_prefix
from domainkit.contrib.auth import (
    AbstractBaseUser,
    AuthUnitOfWork,
    User,
    check_password,
)
from domainkit.contrib.auth.management.commands import changepassword, createsuperuser
from domainkit.management import CommandError, call_command
from domainkit.management.base import BaseCommand, run_async

STRONG = "vy3!Tq-pX9#mR2"


BYPASS = "Bypass password validation and create user anyway? [y/N]: "
TOO_SHORT = "This password is too short. It must contain at least 8 characters."
WEAK_ERRORS = [
    TOO_SHORT,
    "This password is too common.",
    "This password is entirely numeric.",
]

type Prompts = Callable[..., list[str]]


def answers(values: Iterable[str], asked: list[str]) -> Callable[[str], str]:
    """A prompt that answers with ``values`` in turn; ``^C`` interrupts.

    Each prompt shown is added to ``asked``.
    """
    queue = iter(values)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        value = next(queue)
        if value == "^C":
            raise KeyboardInterrupt
        return value

    return ask


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> Prompts:
    """Script the answers to plain and secret prompts; get the prompts shown."""

    def script(
        command: type, *, plain: Iterable[str] = (), secret: Iterable[str] = ()
    ) -> list[str]:
        asked: list[str] = []
        monkeypatch.setattr(
            command, "ask", staticmethod(answers(plain, asked)), raising=False
        )
        monkeypatch.setattr(command, "ask_secret", staticmethod(answers(secret, asked)))
        return asked

    return script


def usage(command: type[BaseCommand], name: str) -> str:
    """The command's ``--help``, on one line."""
    return " ".join(command().create_parser("manage.py", name).format_help().split())


def stored(username: str) -> AbstractBaseUser | None:
    """The stored user called ``username``."""

    async def find() -> AbstractBaseUser | None:
        async with AuthUnitOfWork() as uow:
            return await uow.users.get_by_natural_key(username)

    return run_async(find())


def create(username: str) -> None:
    """Store an ordinary user."""

    async def add() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.create_user(username, STRONG)
            await uow.commit()

    run_async(add())


@pytest.fixture
def output() -> tuple[io.StringIO, io.StringIO]:
    """Captured stdout and stderr."""
    return io.StringIO(), io.StringIO()


def test_run_async_returns_the_result() -> None:
    """Sync commands get the value back."""

    async def answer() -> int:
        await asyncio.sleep(0)
        return 42

    assert run_async(answer()) == 42


def test_createsuperuser_asks_for_everything(
    auth_project: str,
    prompts: Prompts,
    output: tuple[io.StringIO, io.StringIO],
) -> None:
    """Blank and taken names, mismatched and weak passwords are asked again."""
    create("taken")
    asked = prompts(
        createsuperuser.Command,
        plain=["", "taken", "admin", "admin@example.com", "n", "y"],
        secret=["a", "b", " ", " ", "admin2026", "admin2026", "1234", "1234"],
    )
    stdout, stderr = output
    call_command("createsuperuser", stdout=stdout, stderr=stderr)

    admin = stored("admin")
    assert isinstance(admin, User)
    assert (admin.is_superuser, admin.is_staff) == (True, True)
    assert admin.email == "admin@example.com"
    assert check_password("1234", admin.password.value)
    assert stdout.getvalue() == "Superuser created successfully.\n"
    twice = ["Password: ", "Password (again): "]
    assert asked == [
        *["Username: "] * 3,
        "Email: ",
        *twice * 3,
        BYPASS,
        *twice,
        BYPASS,
    ]
    assert stderr.getvalue().splitlines() == [
        "Error: Username cannot be blank.",
        "Error: That username is already taken.",
        "Error: Your passwords didn't match.",
        "Error: Blank passwords aren't allowed.",
        "Error: The password is too similar to the username.",
        f"Error: {WEAK_ERRORS[0]}",
        *WEAK_ERRORS[1:],
    ]


def test_createsuperuser_takes_options(auth_project: str, prompts: Prompts) -> None:
    """Given fields are not asked for; a taken given name is an error."""
    asked = prompts(createsuperuser.Command, secret=[STRONG, STRONG])
    call_command(
        "createsuperuser",
        "--username",
        " root ",
        "--email",
        "r@x.org",
        stdout=io.StringIO(),
    )
    root = stored("root")
    assert root is not None
    assert root.email == "r@x.org"  # type: ignore[attr-defined]
    assert asked == ["Password: ", "Password (again): "]

    with pytest.raises(CommandError, match=r"^The username 'root' is taken\.$"):
        call_command("createsuperuser", "--username", "root", stdout=io.StringIO())
    with pytest.raises(CommandError, match=r"^The username cannot be blank\.$"):
        call_command("createsuperuser", "--username", " ", stdout=io.StringIO())
    prompts(createsuperuser.Command, secret=[STRONG, STRONG])
    with pytest.raises(CommandError, match=r"^Enter one email address, such as"):
        call_command(
            "createsuperuser",
            "--username",
            "boss",
            "--email",
            "Boss <boss@x.org>",
            stdout=io.StringIO(),
        )
    assert stored("boss") is None


def test_createsuperuser_usage(auth_project: str) -> None:
    """An option per field, labelled; ``--noinput`` has Django's alias."""
    text = usage(createsuperuser.Command, "createsuperuser")

    assert "--username USERNAME the user's Username" in text
    assert "--email EMAIL the user's Email" in text
    assert (
        "--noinput, --no-input ask nothing; read missing values from "
        "<PREFIX>SUPERUSER_<FIELD>" in text
    )
    assert createsuperuser._label("first_name") == "First name"


def test_createsuperuser_without_input(
    auth_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values come from options or variables; passwords are checked."""
    prefix = project_prefix()
    with pytest.raises(CommandError) as missing:
        call_command("createsuperuser", "--noinput", stdout=io.StringIO())
    assert str(missing.value) == (
        f"Give the username with --username or {prefix}SUPERUSER_USERNAME."
    )

    monkeypatch.setenv(f"{prefix}SUPERUSER_USERNAME", "operator")
    for password, errors in (
        ("1234", WEAK_ERRORS),
        ("operator1", ["The password is too similar to the username."]),
    ):
        monkeypatch.setenv(f"{prefix}SUPERUSER_PASSWORD", password)
        with pytest.raises(CommandError) as weak:
            call_command("createsuperuser", "--noinput", stdout=io.StringIO())
        assert str(weak.value) == " ".join(errors)

    monkeypatch.setenv(f"{prefix}SUPERUSER_USERNAME", "ops")

    monkeypatch.setenv(f"{prefix}SUPERUSER_PASSWORD", STRONG)
    call_command("createsuperuser", "--noinput", stdout=io.StringIO())
    ops = stored("ops")
    assert ops is not None
    assert check_password(STRONG, ops.password.value)

    monkeypatch.delenv(f"{prefix}SUPERUSER_PASSWORD")
    call_command(
        "createsuperuser", "--no-input", "--username", "robot", stdout=io.StringIO()
    )
    robot = stored("robot")
    assert robot is not None
    assert not robot.has_usable_password()
    assert robot.email == ""  # type: ignore[attr-defined]

    with pytest.raises(CommandError, match="username"):
        call_command(
            "createsuperuser",
            "--noinput",
            "--username",
            "bad name",
            stdout=io.StringIO(),
        )
    with pytest.raises(CommandError, match="cannot be blank"):
        call_command(
            "createsuperuser", "--noinput", "--username", " ", stdout=io.StringIO()
        )


def test_createsuperuser_can_be_cancelled(auth_project: str, prompts: Prompts) -> None:
    """Ctrl+C at a prompt stops without creating anything."""
    prompts(createsuperuser.Command, plain=["^C"])
    with pytest.raises(CommandError) as cancelled:
        call_command("createsuperuser", stdout=io.StringIO())
    assert str(cancelled.value) == "Operation cancelled."
    assert stored("admin") is None


def test_changepassword(
    auth_project: str,
    prompts: Prompts,
    output: tuple[io.StringIO, io.StringIO],
) -> None:
    """Mismatches and weak passwords are asked again; three strikes stop."""
    create("alice")
    asked = prompts(
        changepassword.Command,
        secret=["new-one", "other", "1234", "1234", "alice2026", "alice2026"]
        + ["n3w-Passw0rd!"] * 2,
    )
    stdout, stderr = output
    with pytest.raises(CommandError) as failed:
        call_command("changepassword", "alice", stdout=stdout, stderr=stderr)

    assert str(failed.value) == (
        "Aborting password change for user 'alice' after 3 attempts."
    )
    assert asked == ["Password: ", "Password (again): "] * 3
    assert stdout.getvalue() == "Changing password for user 'alice'\n"
    assert stderr.getvalue().splitlines() == [
        "Passwords do not match. Please try again.",
        *WEAK_ERRORS,
        "The password is too similar to the username.",
    ]

    prompts(changepassword.Command, secret=["n3w-Passw0rd!"] * 2)
    stdout = io.StringIO()
    call_command("changepassword", "alice", stdout=stdout)
    alice = stored("alice")
    assert alice is not None
    assert check_password("n3w-Passw0rd!", alice.password.value)
    assert stdout.getvalue().splitlines() == [
        "Changing password for user 'alice'",
        "Password changed successfully for user 'alice'",
    ]

    prompts(changepassword.Command, secret=["^C"])
    with pytest.raises(CommandError) as cancelled:
        call_command("changepassword", "alice", stdout=io.StringIO())
    assert str(cancelled.value) == "Operation cancelled."


def test_changepassword_usage(auth_project: str) -> None:
    """The username is optional."""
    text = usage(changepassword.Command, "changepassword")

    assert "[username]" in text
    assert "username whose password (default: your login name)" in text
    assert (
        "--temporary, --no-temporary the user changes it at the first login "
        "(default: for a user named, with AUTH.PASSWORD_CHANGE_ON_FIRST_USE)"
    ) in text


def test_changepassword_finds_the_user(
    auth_project: str,
    prompts: Prompts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The login name is the default; unknown users are an error."""
    with pytest.raises(CommandError, match="'ghost' does not exist"):
        call_command("changepassword", "ghost", stdout=io.StringIO())

    create("operator")
    monkeypatch.setattr(getpass, "getuser", lambda: "operator")
    prompts(changepassword.Command, secret=[STRONG, STRONG])
    call_command("changepassword", stdout=io.StringIO())

    with pytest.raises(CommandError, match="'ghost' does not exist"):
        run_async(changepassword.Command()._save("ghost", STRONG, temporary=False))

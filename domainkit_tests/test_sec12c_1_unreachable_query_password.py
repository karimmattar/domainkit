"""SEC-12c-1: ``migrate``'s one line prints a password given in the URL's query.

``DatabaseUnreachable`` masks the URL with ``render_as_string(hide_password=True)``,
which hides only the userinfo password. libpq-style URLs carry credentials in
the query just as well (``?password=``), and asyncpg uses it (in the second
case the query's password wins over the userinfo's); the command's one line
then prints it as it is, into the deploy log the line was written for.
"""

import io
from typing import Any

import pytest

from domainkit.conf import override_settings
from domainkit.management import CommandError, call_command

SECRET = "s3cret-in-query"


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+asyncpg://app@127.0.0.1:1/app?password={SECRET}",
        f"postgresql+asyncpg://app:x@127.0.0.1:1/app?user=app&password={SECRET}",
    ],
)
@pytest.mark.usefixtures("database_access")
def test_a_password_in_the_query_is_masked(url: str) -> None:
    """No credential of the URL appears in the one-line refusal."""
    with (
        override_settings(DATABASES={"default": {"URL": url}}),
        pytest.raises(CommandError) as refused,
    ):
        run("showmigrations")

    said = str(refused.value)
    assert "cannot be reached" in said
    assert SECRET not in said, said


@pytest.mark.usefixtures("database_access")
def test_a_password_with_an_unencoded_at_sign_is_masked() -> None:
    """``p@ss`` written as it is: the URL's host becomes ``ss@db``, then printed.

    The URL cannot be reached (its host is wrong), the very case the one
    line is for, and it names the end of the password.
    """
    url = "postgresql+asyncpg://app:Hunter2@Tail-of-the-pw@127.0.0.1:1/app"
    with (
        override_settings(DATABASES={"default": {"URL": url}}),
        pytest.raises(CommandError) as refused,
    ):
        run("showmigrations")

    said = str(refused.value)
    assert "cannot be reached" in said
    assert "Tail-of-the-pw" not in said, said

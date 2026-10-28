"""SEC-12a-3: a fixture loads password "hashes" the configured hashers never make.

ADR 0100's 0.12 amendment makes a fixture's password go through
``PasswordHash``, "so the object's checks still apply: a raw password in
place of a hash is refused". ``PasswordHash`` only checks the text's shape
(``[a-z][a-z0-9_]*$.+``), so with production's hashers
(``PBKDF2PasswordHasher``, 1,500,000 iterations) ``loaddata
--include-guarded`` stores, as a user's password:

- ``plaintext$hunter2``: a raw password, kept in clear in the users table,
  of an algorithm no configured hasher has;
- ``pbkdf2_sha256$1$...``: a hash of one iteration, as the test hasher
  (``FastPBKDF2PasswordHasher``, which staging and production refuse in
  ``PASSWORD_HASHERS``) makes. It is a working login, and a copy of the
  table gives its password up at once; it is only made again at that
  user's next login, if one ever comes.

A fixture dumped from a test or development database brings both into
production. A hash that no configured hasher identifies, or that one
would not make (``must_update``), must be refused as a raw password is.
"""

import uuid
from pathlib import Path

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.auth.hashers import FastPBKDF2PasswordHasher, check_password
from domainkit.contrib.auth.tables import users
from domainkit.management import CommandError
from domainkit_tests.test_data_commands import project, run  # noqa: F401 - fixture.
from domainkit_tests.test_data_guards import fixture_file, user_row

PRODUCTION_HASHERS = {
    "PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"]
}
WEAK = FastPBKDF2PasswordHasher().encode("correct-horse-battery", "s" * 22)


@pytest.mark.parametrize(
    "password",
    ["plaintext$hunter2", WEAK],
    ids=["raw-password", "one-iteration"],
)
def test_a_fixture_cannot_load_a_hash_the_hashers_would_not_make(
    project: str,  # noqa: F811 - the fixture.
    tmp_path: Path,
    password: str,
) -> None:
    """Refused, as ``correct-horse-battery`` is."""
    with override_settings(AUTH=PRODUCTION_HASHERS):
        assert check_password("correct-horse-battery", WEAK)  # A working login.
        row = user_row(id=str(uuid.uuid4()), username="eve", password=password)
        path = fixture_file(tmp_path / "users.json", (users, [row]))
        with pytest.raises(CommandError, match=r"domainkit_users\.password"):
            run("loaddata", str(path), "--include-guarded")

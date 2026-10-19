"""Migration tooling: empty revisions, plans, offline SQL, fakes, and squashing."""

import io
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, String, Table

from domainkit.conf import override_settings
from domainkit.db import migrations as migrations_module
from domainkit.db import squashing
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.db.squashing import SquashRefused, squash
from domainkit.management import CommandError, call_command


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


metadata = MetaData(naming_convention=NAMING_CONVENTION)
Table(
    "framework_test_shelves",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(20), nullable=False),
)


@pytest.fixture
def project(
    tmp_path: Path, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """An empty database, a revision directory, and one table to migrate."""
    monkeypatch.setattr(migrations_module, "metadata", metadata)
    versions = tmp_path / "migrations"
    with override_settings(
        MIGRATIONS_DIR=versions, DATABASES={"default": {"URL": database_url}}
    ):
        yield versions


def add_table(name: str) -> Table:
    """Add a table to the models, as a new aggregate would."""
    return Table(name, metadata, Column("id", Integer, primary_key=True))


@pytest.fixture
def clean_metadata() -> Iterator[None]:
    """Remove tables a test added."""
    names = set(metadata.tables)
    yield
    for name in set(metadata.tables) - names:
        metadata.remove(metadata.tables[name])


def created(output: str) -> Path:
    """The file a ``Created <path>.`` line names."""
    match = re.search(r"Created (\S+?\.py)", output)
    assert match is not None, output
    return Path(match[1])


# makemigrations --empty -----------------------------------------------------------


def test_empty_revisions_are_written_for_data_migrations(project: Path) -> None:
    """An empty revision follows the last one, and does nothing until filled."""
    first = created(run("makemigrations", "-m", "shelves"))

    empty = created(run("makemigrations", "--empty", "-m", "backfill names"))

    text = empty.read_text(encoding="utf-8")
    assert empty.name.startswith("0002_")
    assert empty.name.endswith("_backfill_names.py")
    assert f"down_revision: str | None = {first.name[:9]!r}" in text.replace('"', "'")
    assert "    pass" in text
    assert run("migrate") == "Database 'default' is at head.\n"
    assert created(run("makemigrations", "--empty", database="default")).exists()


def test_empty_revisions_need_one_target(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several targets need --database; messages are checked as usual."""
    monkeypatch.setattr(
        migrations_module, "migration_targets", lambda: ("default", "reports")
    )
    with pytest.raises(CommandError, match=r"--database \('default', 'reports'\)"):
        run("makemigrations", "--empty")
    with pytest.raises(CommandError, match="one line of text"):
        run("makemigrations", "--empty", "-m", 'a"""b')
    monkeypatch.setattr(migrations_module, "migration_targets", tuple)
    with pytest.raises(CommandError, match=r"--database \(none\)"):
        run("makemigrations", "--empty")


def test_empty_is_exclusive_with_the_other_modes(project: Path) -> None:
    """--empty cannot be combined with --check or --merge."""
    with pytest.raises(SystemExit):
        run("makemigrations", "--empty", "--check")


# migrate --plan, --sql, --fake ----------------------------------------------------


def test_plans_list_what_would_run(project: Path) -> None:
    """Upgrades oldest first, downgrades newest first; nothing runs."""
    first = created(run("makemigrations", "-m", "shelves")).name[:9]
    second = created(run("makemigrations", "--empty", "-m", "fill")).name[:9]

    assert run("migrate", "--plan") == (
        f"Database 'default':\n  apply  {first}  Shelves.\n  apply  {second}  Fill.\n"
    )
    assert run("showmigrations").count("[ ]") == 2
    run("migrate")
    assert run("migrate", "--plan") == "Database 'default': nothing to do.\n"
    assert run("migrate", "base", "--plan", "--downgrade") == (
        f"Database 'default':\n  revert {second}  Fill.\n  revert {first}  Shelves.\n"
    )


def test_plans_and_sql_need_no_consent_in_deployments(
    project: Path, tmp_path: Path
) -> None:
    """Nothing runs, so staging and production plan and print downgrades freely."""
    # Always SQLite: staging also demands TLS from network databases.
    local = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'deployed.db'}"}}
    with override_settings(DATABASES=local):
        run("makemigrations", "-m", "shelves")
        run("migrate")
        with override_settings(ENVIRONMENT="staging"):
            assert "revert" in run("migrate", "base", "--plan", "--downgrade")
            assert "DROP TABLE" in run("migrate", "base", "--sql", "--downgrade")
            with pytest.raises(CommandError, match="--allow-data-loss"):
                run("migrate", "base", "--fake", "--downgrade")


def test_sql_is_rendered_offline(project: Path) -> None:
    """The statements from the database's revision, or another, to the target."""
    first = created(run("makemigrations", "-m", "shelves")).name[:9]

    upgrade = run("migrate", "--sql")
    assert "CREATE TABLE framework_test_shelves" in upgrade
    assert run("showmigrations").startswith("[ ]")  # Nothing ran.
    run("migrate")
    assert "CREATE TABLE" not in run("migrate", "--sql")
    assert "CREATE TABLE framework_test_shelves" in run(
        "migrate", "--sql", "--from", "base"
    )
    downgrade = run("migrate", "base", "--sql", "--downgrade")
    assert "DROP TABLE framework_test_shelves" in downgrade
    assert first in downgrade
    with pytest.raises(CommandError, match="--from goes with --sql"):
        run("migrate", "--from", "base")


def test_sql_refuses_a_database_at_several_heads(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start must be one revision."""
    run("makemigrations", "-m", "shelves")

    async def two(database: object) -> tuple[str, ...]:
        return ("0001_aaaa", "0001_bbbb")

    monkeypatch.setattr(migrations_module, "_current_heads", two)
    with pytest.raises(CommandError, match="is at 2 heads"):
        run("migrate", "--sql")


def test_fakes_record_without_running(project: Path) -> None:
    """Up and down, the version moves and the schema does not."""
    first = created(run("makemigrations", "-m", "shelves")).name[:9]
    run("makemigrations", "--empty", "-m", "fill")

    assert run("migrate", "--fake") == (
        "Database 'default' is recorded at head; nothing ran.\n"
    )
    assert run("showmigrations").count("[X]") == 2
    run("migrate", first, "--fake", "--downgrade")
    assert [line[:3] for line in run("showmigrations").splitlines()] == ["[X]", "[ ]"]
    with pytest.raises(CommandError, match="is not below"):
        run("migrate", "nope", "--fake", "--downgrade")
    run("migrate", "base", "--fake", "--downgrade")
    assert run("showmigrations").count("[ ]") == 2
    run("migrate")  # The table was never made, so the real upgrade works.


def test_modes_are_exclusive(project: Path) -> None:
    """--plan, --sql, and --fake are one at a time."""
    with pytest.raises(SystemExit):
        run("migrate", "--plan", "--fake")


# squashmigrations -----------------------------------------------------------------


def write(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in the file at ``path``."""
    text = path.read_text(encoding="utf-8")
    assert old in text, text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.fixture
def history(project: Path, clean_metadata: None) -> list[Path]:
    """Three applied revisions: a table, another table, and a data migration."""
    first = created(run("makemigrations", "-m", "shelves"))
    run("migrate")
    add_table("framework_test_bins")
    second = created(run("makemigrations", "-m", "bins"))
    third = created(run("makemigrations", "--empty", "-m", "fill"))
    write(
        third,
        '    """Apply this revision."""\n    pass',
        '    """Apply this revision."""\n    # Name every shelf.\n'
        "    op.execute(\"UPDATE framework_test_shelves SET name = 'x'\")",
    )
    run("migrate")
    return [first, second, third]


def test_squashing_keeps_the_newest_id_and_both_directions(
    history: list[Path], project: Path
) -> None:
    """One file does what three did, up and down; databases stay where they were."""
    newest = history[-1].name[:9]

    output = run("squashmigrations", "-m", "initial")

    squashed = created(output.splitlines()[0])
    assert output.splitlines()[1:] == [f"  {path.name[:9]}" for path in history]
    assert sorted(path.name for path in project.glob("*.py")) == [squashed.name]
    text = squashed.read_text(encoding="utf-8")
    assert f'revision: str = "{newest}"' in text
    assert "down_revision: str | None = None" in text
    assert "    # Name every shelf." in text
    assert run("showmigrations") == f"[X] {newest}  Initial.\n"
    assert run("migrate", "base", "--downgrade") == "Database 'default' is at base.\n"
    assert run("migrate") == "Database 'default' is at head.\n"
    assert run("makemigrations", "--check") == "No changes detected.\n"


def test_a_partial_squash_keeps_later_revisions(
    history: list[Path], project: Path
) -> None:
    """Squashing up to a middle revision; the next one still follows it."""
    first, second, third = (path.name[:9] for path in history)

    output = run("squashmigrations", second, "--start", first)

    assert output.splitlines()[1:] == [f"  {first}", f"  {second}"]
    assert [line.split()[1] for line in run("showmigrations").splitlines()] == [
        second,
        third,
    ]
    run("migrate", "base", "--downgrade")
    run("migrate")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--start", "nope"), "Can't locate revision"),
        (("--start", "base"), "Name revisions, not 'base'"),
        (("-m", 'x"""'), "one line of text"),
    ],
)
def test_squashing_refuses_unknown_runs(
    history: list[Path], arguments: tuple[str, ...], message: str
) -> None:
    """Unknown ids and bad messages are refused; nothing changes."""
    with pytest.raises(CommandError, match=message):
        run("squashmigrations", *arguments)


def test_squashing_needs_a_run_in_order(history: list[Path]) -> None:
    """The start must come before the end, and the run hold two revisions."""
    first, second, third = (path.name[:9] for path in history)

    with pytest.raises(CommandError, match=f"{third} is not before {first}"):
        run("squashmigrations", first, "--start", third)
    with pytest.raises(CommandError, match="at least two revisions"):
        run("squashmigrations", second, "--start", second)


def test_squashing_refuses_databases_inside_the_run(history: list[Path]) -> None:
    """A database at a revision the squash replaces would be lost."""
    second = history[1].name[:9]
    run("migrate", second, "--downgrade")

    with pytest.raises(CommandError, match=f"is at {second}, which the squash"):
        run("squashmigrations")
    assert run("squashmigrations", second).startswith("Created ")


def test_squashing_refuses_branches(history: list[Path], project: Path) -> None:
    """Two heads need merging first; merges and branch points are refused."""
    from alembic import command

    first, second, _ = (path.name[:9] for path in history)
    command.revision(
        migrations_module.alembic_config(),
        message="side",
        head=first,
        splice=True,
        rev_id="0002_side",
    )
    with pytest.raises(CommandError, match="has 2 heads"):
        run("squashmigrations")
    run("makemigrations", "--merge")
    with pytest.raises(CommandError, match="merges branches"):
        run("squashmigrations")
    with pytest.raises(CommandError, match=f"{first} starts a branch"):
        run("squashmigrations", second)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "def downgrade() -> None:",
            "HELPER = 1\n\n\ndef downgrade() -> None:",
            "code besides",
        ),
        (
            "def downgrade() -> None:",
            "@staticmethod\ndef downgrade() -> None:",
            "code besides",
        ),
        (
            'def downgrade() -> None:\n    """Revert this revision."""\n    pass\n',
            "",
            "lacks an upgrade or",
        ),
        (
            '    """Revert this revision."""\n    pass',
            '    """Revert this revision."""\n    raise NotImplementedError',
            "cannot be reverted",
        ),
        (
            "branch_labels: str | Sequence[str] | None = None",
            "branch_labels = ('x',)",
            "branch labels",
        ),
        (
            "import sqlalchemy as sa",
            "import sqlalchemy as sa\nimport json as op",
            "different things as 'op'",
        ),
    ],
)
def test_squashing_refuses_what_it_cannot_copy(
    history: list[Path], old: str, new: str, message: str
) -> None:
    """Module code, decorated or missing functions, raising downgrades, clashes."""
    write(history[-1], old, new)

    with pytest.raises(CommandError, match=message):
        run("squashmigrations")
    assert all(path.exists() for path in history)


def test_squashing_merges_imports(history: list[Path]) -> None:
    """Each import once; the same name bound to the same thing is fine."""
    write(
        history[1],
        "import sqlalchemy as sa",
        "import sqlalchemy as sa\nimport os.path\nfrom json import dumps",
    )
    write(
        history[1],
        'def upgrade() -> None:\n    """Apply this revision."""\n',
        "def upgrade() -> None:\n",
    )
    write(
        history[2],
        "import sqlalchemy as sa",
        "import sqlalchemy as sa\nimport os.path\nimport os.path as os_path",
    )

    text = created(run("squashmigrations").splitlines()[0]).read_text(encoding="utf-8")

    assert text.count("import os.path\n") == 1
    assert "from json import dumps" in text
    assert 'op.create_table(\n        "framework_test_bins"' in text


def test_a_squash_that_fails_its_checks_puts_the_files_back(
    history: list[Path], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files are restored when the new one does not compile or join the history."""
    originals = {path: path.read_text(encoding="utf-8") for path in history}

    def broken(*args: object) -> str:
        return "def broken(:\n"

    def disjoined(target: str, run: object) -> None:
        raise SquashRefused("The squashed revision does not join the history.")

    monkeypatch.setattr(squashing, "_render", broken)
    with pytest.raises(CommandError, match="would not compile"):
        run("squashmigrations")
    monkeypatch.undo()
    monkeypatch.setattr(migrations_module, "metadata", metadata)
    monkeypatch.setattr(squashing, "_check_replacement", disjoined)
    with pytest.raises(CommandError, match="does not join"):
        run("squashmigrations")

    assert {path: path.read_text(encoding="utf-8") for path in history} == originals
    assert sorted(project.glob("*.py")) == sorted(history)


def test_replacements_are_checked_against_the_history(
    history: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history that no longer loads, or no longer joins, is refused."""
    scripts = ScriptDirectory.from_config(migrations_module.alembic_config())
    run_ = [scripts.get_revision(path.name[:9]) for path in history]
    fake = [SimpleNamespace(revision="nope", down_revision=None)]

    with pytest.raises(SquashRefused, match="does not load"):
        squashing._check_replacement("default", fake)  # type: ignore[arg-type]
    with pytest.raises(SquashRefused, match="does not join"):
        squashing._check_replacement("default", [run_[1], run_[2]])


def test_squash_returns_the_file_and_the_revisions(history: list[Path]) -> None:
    """The Python API, without the command."""
    path, replaced = squash("default", message="everything")

    assert path.name.endswith("_everything.py")
    assert replaced == [revision.name[:9] for revision in history]

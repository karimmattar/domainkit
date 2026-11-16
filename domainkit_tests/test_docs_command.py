"""``domainkit docs``: the pages the package names, for the release installed.

The wheel holds no docs/: docstrings, messages, and ``check``'s hints name
``docs/live.md`` and ``ADR 0057``, which the command links under the
release's tag (ADR 0152, user report D2). ``test_docs.py`` checks that every
page and decision the code names links to a file of the tag.
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import domainkit
from domainkit.management import CommandError, call_command
from domainkit.management.commands import docs
from domainkit.management.commands.docs import link

TAG = "https://github.com/karimmattar/domainkit/blob/v2.3.0/docs"
MAIN = "https://github.com/karimmattar/domainkit/blob/main/docs"


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("docs/live.md", f"{TAG}/live.md"),
        ("live.md", f"{TAG}/live.md"),
        ("docs/reference/servers.md", f"{TAG}/reference/servers.md"),
        ("docs/databases.md#ledgers-immudb", f"{TAG}/databases.md#ledgers-immudb"),
        ("docs/audit/0.9.0.md", f"{TAG}/audit/0.9.0.md"),
        ("  docs/live.md\n", f"{TAG}/live.md"),
    ],
)
def test_a_page_links_to_itself_under_the_release_tag(
    reference: str, expected: str
) -> None:
    """As the code names it, or without ``docs/``, with its anchor."""
    assert link(reference, "2.3.0") == expected


@pytest.mark.parametrize("reference", ["ADR 0057", "ADR0057", "adr 0143"])
def test_a_decision_links_to_the_index_of_the_releases_decisions(
    reference: str,
) -> None:
    """Its row there links the record, and says what amended or replaced it."""
    assert link(reference, "2.3.0") == f"{TAG}/adr/README.md"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.4.0rc1", "https://github.com/karimmattar/domainkit/blob/v2.4.0rc1/docs"),
        ("0.0.0+unknown", MAIN),
        ("2.4.0.dev3", MAIN),
    ],
)
def test_a_pre_release_links_to_its_tag_and_a_checkout_to_main(
    version: str, expected: str
) -> None:
    """Every version a release may have is tagged ``v<version>``."""
    assert link("docs/live.md", version) == f"{expected}/live.md"


@pytest.mark.parametrize(
    "reference",
    [
        "live",
        "docs/live",
        "ADR 57",
        "ADRs 0116 and 0117",
        "docs/../SECURITY.md",
        "docs/.hidden.md",
        "docs/live.md#",
        "https://example.com/docs/live.md",
        "",
    ],
)
def test_a_reference_that_names_no_page_and_no_decision_is_refused(
    reference: str,
) -> None:
    """With the forms the command takes, rather than a link to nothing."""
    with pytest.raises(CommandError) as refused:
        link(reference, "2.3.0")

    assert str(refused.value) == (
        f"{reference.strip()!r} names no page and no decision: give a page, as "
        "docs/live.md, with its #anchor if any, or a decision, as 'ADR 0057'."
    )


def links(*references: str) -> str:
    """Run ``docs`` and return what it printed."""
    output = io.StringIO()
    call_command("docs", *references, stdout=output)
    return output.getvalue()


def test_the_command_prints_a_link_per_reference_for_the_release_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the order given; with none, the first page of the documentation."""
    monkeypatch.setattr(docs, "__version__", "2.3.0")

    assert links("docs/live.md", "ADR 0057") == (
        f"{TAG}/live.md\n{TAG}/adr/README.md\n"
    )
    assert links() == f"{TAG}/index.md\n"


def test_a_refused_reference_prints_no_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every reference is checked before the first link is printed."""
    monkeypatch.setattr(docs, "__version__", "2.3.0")
    output = io.StringIO()

    with pytest.raises(CommandError, match="'nowhere' names no page"):
        call_command("docs", "docs/live.md", "nowhere", stdout=output)

    assert output.getvalue() == ""


def test_the_domainkit_program_links_a_page_without_a_project(tmp_path: Path) -> None:
    """As after ``pip install domainkit``: no settings, no project, no docs/."""
    clean = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DOMAINKIT_") and name != "MUTANT_UNDER_TEST"
    }
    done = subprocess.run(
        [sys.executable, "-m", "domainkit", "docs", "docs/live.md", "ADR 0057"],
        cwd=tmp_path,
        env=clean,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        link("docs/live.md", domainkit.__version__),
        link("ADR 0057", domainkit.__version__),
    ]

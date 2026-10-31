"""docs/fintech.md answers a bank's first review: a page and a test each.

Every test the page names, as ``file::test``, exists: a renamed or removed
test fails here, not in a bank's review (FIN-74).
"""

import ast
import re
from pathlib import Path

from domainkit_tests.test_docs import DOCS, section_of

ROOT = Path(__file__).parents[1]
PAGE = DOCS / "fintech.md"
NAMED = re.compile(r"`([\w./-]+\.py(?:-tpl)?)::(\w+)`")
"""A test as the page names it: `domainkit_tests/test_x.py::test_y`."""
FIRST_REVIEW = (
    "Threat model",
    "Provenance",
    "KMS",
    "Maker-checker with step-up",
    "Verifiable audit with operator identity",
    "Written idempotency semantics",
    "A card-number column unreadable in a dump",
)
"""The bank's list, in its order."""


def page() -> str:
    """The fintech page."""
    return PAGE.read_text(encoding="utf-8")


def rows(section: str) -> dict[str, list[str]]:
    """The cells of each row of the table under ``section``, by first cell."""
    table = section_of(page(), section)
    found = {}
    for line in table.splitlines():
        if not line.startswith("| ") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        found[cells[0]] = cells[1:]
    return found


def defined_tests(path: Path) -> set[str]:
    """The test functions ``path`` defines at its top level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def test_every_test_the_fintech_page_names_exists() -> None:
    """Each ``file::test`` is a test function of that file, in this repository."""
    named = NAMED.findall(page())
    missing = [
        f"{file}::{test}"
        for file, test in named
        if not (ROOT / file).is_file() or test not in defined_tests(ROOT / file)
    ]

    assert len(named) > 60
    assert missing == []


def test_each_item_of_the_first_review_has_a_page_a_setting_and_a_test() -> None:
    """The bank's list, each with a linked page, a setting or command, and tests."""
    answered = rows("## The first-review list")

    assert list(answered)[1:] == list(FIRST_REVIEW)  # The header row first.
    for item in FIRST_REVIEW:
        where, setting, tests = answered[item]
        assert re.search(r"\]\([\w./-]+(#[\w-]+)?\)", where), item
        assert setting, item
        assert len(NAMED.findall(tests)) >= 2, item


def test_every_fintech_audit_item_answered_is_on_the_page() -> None:
    """The fintech audit's ids, and its re-audit's, each with a row."""
    audit = (DOCS / "audit" / "0.9.0.md").read_text(encoding="utf-8")
    cited = {
        line.split("|")[1].strip()
        for heading in ("### The fintech audit", "### The fintech re-audit")
        for line in section_of(audit, heading).splitlines()
        if re.match(r"\| (FIN-\d+|N\d+) \|", line)
    }
    answered = rows("## The fintech audit's other items")
    del answered["Id"]  # The header.

    assert {"FIN-74", "N8"} <= cited
    assert sorted(cited - set(answered)) == []
    for item, (_, where, _, tests) in answered.items():
        assert where, item
        assert NAMED.search(tests) or "above" in tests, item


def test_the_threat_model_covers_moving_money() -> None:
    """Its section on a transfer names each control's page, by STRIDE."""
    model = (DOCS / "threat-model.md").read_text(encoding="utf-8")
    moving = section_of(model, "## Moving money")
    threats = re.findall(r"^\| \*\*(\w[\w ]*?)\*\*", moving, re.MULTILINE)

    assert threats == [
        "Spoofing",
        "Tampering",
        "Repudiation",
        "Tampering",
        "Information disclosure",
        "Denial of service",
        "Elevation of privilege",
    ]
    for control in (
        "auth/step-up.md",
        "contrib/approvals.md",
        "contrib/audit.md",
        "contrib/idempotency.md",
        "contrib/keys.md",
        "contrib/limits.md",
        "money.md",
        "fintech.md",
    ):
        assert f"]({control}" in moving, control


def test_the_page_is_linked_from_the_readme_and_the_site() -> None:
    """Readers find it: the README's list of guides, and the site's navigation."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert "/docs/fintech.md)" in readme
    assert "      - fintech.md\n" in nav

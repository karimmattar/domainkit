"""The how-to recipes work exactly as written (DOCS-05).

Each page under ``docs/recipes`` is walked as the tutorial is
(``test_walkthroughs.py``): in a new directory, every command as a user
would run it and every edit as a reader would make it, a block titled
``... (new file)`` being a file to create. Each ends with ruff and the
project's tests, the page's own among them.
"""

import re
from pathlib import Path

import pytest

from domainkit_tests.test_walkthroughs import Walk, steps

RECIPES = Path(__file__).parents[1] / "docs" / "recipes"
PASSED = {
    "payments.md": 12,
    "exactly-once.md": 8,
    "jwt-keys.md": 4,
    "behind-a-proxy.md": 7,
    "read-replica.md": 9,
}
"""Each recipe's project's tests, the page's own included, after its last step."""
CHECKED = {
    "jwt-keys.md": "KEYS_API_",
    "behind-a-proxy.md": "EDGE_API_",
    "read-replica.md": "SHOP_API_",
}
"""The recipes that run ``check`` with production settings, and their prefix."""


def test_every_recipe_is_walked() -> None:
    """A new page fails until this module knows what it should pass."""
    assert sorted(path.name for path in RECIPES.glob("*.md")) == sorted(PASSED)


@pytest.mark.parametrize("recipe", sorted(PASSED))
def test_a_recipe_works_as_written(recipe: str, tmp_path: Path) -> None:
    """Its commands and edits, in order; then its tests pass, lint-clean."""
    shell = Walk(tmp_path)

    assert shell.play(steps((RECIPES / recipe).read_text(encoding="utf-8"))) == []

    [tested] = [out for line, out in shell.printed.items() if line == "pytest"]
    assert re.findall(r"(\d+) passed", tested) == [str(PASSED[recipe])]
    assert "failed" not in tested
    if prefix := CHECKED.get(recipe):
        [checked] = [
            out
            for line, out in shell.printed.items()
            if line.startswith(f"{prefix}SETTINGS_MODULE=config.settings.production ")
        ]
        assert checked.startswith("System check identified no issues")
        assert (
            "It takes a request's scheme from the X-Forwarded-Proto header of "
            "TRUSTED_PROXIES only" in checked
        )

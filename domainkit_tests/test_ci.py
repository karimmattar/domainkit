"""The weekly mutation run covers every module, each in exactly one shard."""

import ast
import re
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SOURCE = ROOT / "src"


def shards() -> dict[str, list[str]]:
    """Each shard of the mutation job, and the mutant names it runs."""
    job = WORKFLOW.read_text().split("\n  mutation:\n", 1)[1]
    job = job.split("\n    steps:\n", 1)[0] + "\n"
    found: dict[str, list[str]] = {}
    for name, quoted, folded in re.findall(
        r"- name: (\S+)\n\s+mutants: (?:\"([^\"]*)\"|>-\n((?:\s{14}\S.*\n)+))", job
    ):
        found[name] = (quoted or folded).split()
    return found


def mutated_modules() -> list[str]:
    """Every module with a function or class mutmut can mutate."""
    modules = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if not any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            for node in tree.body
        ):
            continue
        parts = path.relative_to(SOURCE).with_suffix("").parts
        modules.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return modules


def test_every_mutated_module_is_in_exactly_one_shard() -> None:
    """No module is left out of the weekly run, or mutated twice."""
    patterns = [
        (shard, pattern) for shard, names in shards().items() for pattern in names
    ]
    assert len(shards()) == 8  # Every shard was read.
    for module in mutated_modules():
        mutant = f"{module}.x_f__mutmut_1"  # How mutmut names a mutant.
        matching = [
            shard for shard, pattern in patterns if fnmatchcase(mutant, pattern)
        ]
        assert len(matching) == 1, (module, matching)


def test_every_pattern_names_a_module() -> None:
    """A pattern that matches nothing is a typo, or a module that moved."""
    modules = mutated_modules()
    for shard, names in shards().items():
        for pattern in names:
            assert any(
                fnmatchcase(f"{module}.x_f__mutmut_1", pattern) for module in modules
            ), (shard, pattern)

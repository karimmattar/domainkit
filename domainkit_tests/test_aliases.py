"""Names renamed in a patch release keep working under their old names.

A patch release may add the consistent name but not take the old one away
(``docs/versioning.md``); 0.10 deprecates these, and the release after
removes them.
"""

import ast
import importlib
from pathlib import Path

import domainkit.ddd
import domainkit.ddd.query
from domainkit.contrib.evidence import Evidence
from domainkit.ddd import F, Selection
from domainkit.ddd.messages import Query as QueryMessage


def test_selection_is_the_aggregate_query_under_its_new_name() -> None:
    """``ddd.query.Query`` is ``Selection``; ``ddd.Query`` stays the message."""
    assert domainkit.ddd.query.Query is Selection
    assert domainkit.ddd.query.Selection is Selection
    assert domainkit.ddd.Query is QueryMessage
    assert "Selection" in domainkit.ddd.__all__
    assert "Query" in domainkit.ddd.__all__


def test_a_selection_built_by_the_old_name_is_a_selection() -> None:
    """Both names build, compare, and copy as one class."""
    old = domainkit.ddd.query.Query(F("status") == "open", limit=5)
    new = Selection(F("status") == "open", limit=5)

    assert old == new
    assert type(old.where(F("total") > 1)) is Selection
    assert type(old.replace(limit=1)) is Selection
    assert repr(new).startswith("Selection(")


def test_the_evidence_model_module_keeps_its_old_name() -> None:
    """``evidence.model`` re-exports what ``evidence.models`` defines."""
    old = importlib.import_module("domainkit.contrib.evidence.model")
    new = importlib.import_module("domainkit.contrib.evidence.models")
    names = [
        "DIGEST",
        "FORMAT",
        "KIND",
        "SUBJECT",
        "Evidence",
        "EvidenceConflict",
        "EvidenceNotFound",
        "EvidenceReceipt",
        "EvidenceTampered",
        "RecordedEvidence",
        "sha256",
    ]
    tree = ast.parse(Path(new.__file__ or "").read_text(encoding="utf-8"))
    defined = {
        target.id if isinstance(target, ast.Name) else ""
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    } | {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert {name for name in defined if not name.startswith("_")} == set(names)
    for name in names:
        assert getattr(old, name) is getattr(new, name), name
    assert Evidence.__module__ == "domainkit.contrib.evidence.models"

"""Names renamed in a patch release keep working under their old names.

A patch release may add the consistent name but not take the old one away
(``docs/versioning.md``); 0.10 deprecates these, and the release after
removes them.
"""

import importlib

import pytest

import domainkit.ddd
import domainkit.ddd.query
from domainkit.contrib.evidence import Evidence
from domainkit.ddd import F, Selection
from domainkit.ddd.messages import Query as QueryMessage
from domainkit.deprecation import DomainkitDeprecationWarning

OLD_QUERY = (
    r"domainkit\.ddd\.query\.Query is deprecated: use domainkit\.ddd\.Selection\. "
    r"The old name is removed in 0\.11\."
)


def test_selection_is_the_aggregate_query_under_its_new_name() -> None:
    """``ddd.query.Query`` is ``Selection``, and warns; ``ddd.Query`` is the message."""
    with pytest.warns(DomainkitDeprecationWarning, match=OLD_QUERY):
        assert domainkit.ddd.query.Query is Selection
    with pytest.warns(DomainkitDeprecationWarning, match=OLD_QUERY):
        from domainkit.ddd.query import Query
    assert Query is Selection
    assert "Query" not in domainkit.ddd.query.__all__
    with pytest.raises(AttributeError, match="has no attribute 'Querry'"):
        _ = domainkit.ddd.query.Querry
    assert domainkit.ddd.query.Selection is Selection
    assert domainkit.ddd.Query is QueryMessage
    assert "Selection" in domainkit.ddd.__all__
    assert "Query" in domainkit.ddd.__all__


def test_a_selection_built_by_the_old_name_is_a_selection() -> None:
    """Both names build, compare, and copy as one class."""
    with pytest.warns(DomainkitDeprecationWarning, match=OLD_QUERY):
        old = domainkit.ddd.query.Query(F("status") == "open", limit=5)
    new = Selection(F("status") == "open", limit=5)

    assert old == new
    assert type(old.where(F("total") > 1)) is Selection
    assert type(old.replace(limit=1)) is Selection
    assert repr(new).startswith("Selection(")


def test_the_evidence_model_module_keeps_its_old_name() -> None:
    """``evidence.model`` still has every public name it defined in 0.9.1."""
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
    for name in names:
        assert getattr(old, name) is getattr(new, name), name
    assert Evidence.__module__ == "domainkit.contrib.evidence.models"

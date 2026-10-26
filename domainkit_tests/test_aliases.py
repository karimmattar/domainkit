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
    """``evidence.model`` still has every public name of 0.9.1, each with a warning."""
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
        with pytest.warns(DomainkitDeprecationWarning) as caught:
            found = getattr(old, name)
        assert found is getattr(new, name), name
        assert len(caught) == 1
        assert str(caught[0].message) == (
            f"domainkit.contrib.evidence.model is deprecated; import {name} from "
            "domainkit.contrib.evidence (or its models). Removed in 0.11."
        )
        assert caught[0].filename == __file__
    assert Evidence.__module__ == "domainkit.contrib.evidence.models"


def test_the_evidence_model_module_has_no_other_names() -> None:
    """A name it never had is an ``AttributeError``, with no warning."""
    old = importlib.import_module("domainkit.contrib.evidence.model")

    with pytest.raises(AttributeError, match="has no attribute 'Ledger'"):
        old.Ledger  # noqa: B018 - the access is the test.
    assert "Evidence" not in dir(old)

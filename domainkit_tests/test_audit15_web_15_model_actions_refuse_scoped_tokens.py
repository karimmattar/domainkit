"""WEB-15: ``ModelActions`` refuses every credential limited to scopes.

``ModelActions(Model)`` is the rule the resource guide and the generated
project give each ``ResourceRouter``. Its rule, ``HasModelPermission``,
checks the credentials' scopes (it asks ``HasPermissions``), but does not
say so: it leaves ``bounds_scopes`` False. With ``SCOPED_CREDENTIALS =
"refuse"`` (the default since 0.11), ``check_scopes_bounded`` then sees a
route that "declares no rule that checks scopes" and answers 403 to a
token whose scopes cover the route exactly: a personal token scoped to
``shop.view_gadget`` cannot list or read gadgets.
"""

import pytest

from domainkit.http.access import SCOPES_UNBOUNDED
from domainkit.resources import ModelActions
from domainkit.resources.access import HasModelPermission
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, make_gadget
from domainkit_tests.test_resources import (
    GADGETS,
    LOCATION,
    as_,
    client_of,
    gadgets,
)


@pytest.fixture
def store() -> InMemoryGadgets:
    """Two gadgets."""
    return InMemoryGadgets([make_gadget("a"), make_gadget("b")])


def test_model_permission_rule_says_it_checks_scopes() -> None:
    """The rule checks scopes through ``HasPermissions``; it must say so."""
    rule = ModelActions(GADGETS).rules_for("view")[-1]

    assert isinstance(rule, HasModelPermission)
    assert rule.checks_scopes()


def test_a_token_scoped_to_view_reads_a_model_actions_resource(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A token scoped to ``shop.view_gadget`` lists and reads; it does not write."""
    use_auth()
    client = client_of(gadgets(store))
    gadget = next(iter(store.items.values()))
    scoped = as_("viewer", **{"X-Scopes": "shop.view_gadget"})

    listed = client.get("/gadgets", headers=scoped)
    read = client.get(f"{LOCATION}{gadget.id}", headers=scoped)

    assert listed.json().get("detail") != SCOPES_UNBOUNDED
    assert (listed.status_code, read.status_code) == (200, 200)

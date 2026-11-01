"""WEB-20: a command's ``returns=`` schema shows fields the ``FieldPolicy`` hides.

A ``FieldPolicy(visible={"price": "shop.view_cost"})`` leaves ``price`` out
of every read, list, query, and default write answer for a caller without
``shop.view_cost``. A command or batch route that answers with another
schema (``returns=``, documented for summaries and other views) is
rendered by ``presenting.shown_as``, which never asks the policy: the
same caller reads ``price`` in the command's answer. ``schema_for(Gadget)``
given as ``returns`` is enough, as each call makes a new class, not the
resource's own.
"""

import pytest

from domainkit.resources import FieldPolicy, schema_for
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import Gadget, InMemoryGadgets, make_gadget
from domainkit_tests.test_resources import (
    LOCATION,
    RenameGadget,
    RenameIn,
    as_,
    client_of,
    gadgets,
    rename,
)

HIDDEN = FieldPolicy(visible={"price": "shop.view_cost"})


@pytest.fixture
def store() -> InMemoryGadgets:
    """One gadget, at 3.00."""
    return InMemoryGadgets([make_gadget("a", price="3.00")])


def test_a_command_answers_without_the_fields_the_policy_hides(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``changer`` may not see the price, in a read or in a command's answer."""
    use_auth()
    resource = gadgets(store, policy=HIDDEN)
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/rename",
        returns=schema_for(Gadget),
    )
    resource.batch_route(
        RenameGadget, service=rename, body=RenameIn, returns=schema_for(Gadget)
    )
    client = client_of(resource)
    (gadget,) = store.items
    caller = as_("changer")

    read = client.get(f"{LOCATION}{gadget}", headers=caller).json()
    renamed = client.post(
        f"{LOCATION}{gadget}/rename", json={"name": "b"}, headers=caller
    ).json()
    batch = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(gadget), "name": "c"}]},
        headers=caller,
    ).json()

    assert "price" not in read
    assert "price" not in renamed
    assert "price" not in batch["results"][0]["item"]

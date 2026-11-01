"""WEB-22: ``If-Match: *`` satisfies ``require_if_match``.

``require_if_match=True`` is how a resource refuses blind writes: every
change must name the version it was made from, or it is ``428``, so two
clerks editing one record cannot overwrite each other unseen. ``*`` names
no version (``expected_versions`` answers None, "any"), yet it passes the
requirement and the write applies over whatever is stored. A client that
sends ``If-Match: *`` on every write, to get past the 428, has none of
the protection the resource asked for.
"""

import pytest

from domainkit.resources import ALL_FIELDS
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, make_gadget
from domainkit_tests.test_resources import (
    LOCATION,
    RenameGadget,
    RenameIn,
    as_,
    client_of,
    gadgets,
    rename,
)


@pytest.fixture
def store() -> InMemoryGadgets:
    """One gadget."""
    return InMemoryGadgets([make_gadget("a")])


def test_a_required_if_match_must_name_a_version(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``*`` is refused as no ``If-Match`` is: 428, and nothing changes."""
    use_auth()
    resource = gadgets(store, require_if_match=True, crud=True, writable=ALL_FIELDS)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    (gadget,) = store.items
    blind = as_("changer", **{"If-Match": "*"})

    renamed = client.post(
        f"{LOCATION}{gadget}/rename", json={"name": "x"}, headers=blind
    )
    patched = client.patch(f"{LOCATION}{gadget}", json={"stock": 9}, headers=blind)

    assert (renamed.status_code, patched.status_code) == (428, 428)
    assert store.items[gadget].name == "a"

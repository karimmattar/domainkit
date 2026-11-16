"""The testing page's example of an act authorized in a test, run as written.

``docs/testing.md`` ("Moving money") shows ``authorize_act`` for a user
who enrolled an authenticator app; this module holds that function
verbatim, checks the page still shows it, and runs it against the sample
products (user report A17).
"""

import inspect
from contextlib import ExitStack
from pathlib import Path
from typing import Final
from uuid import UUID

from domainkit.clock import FrozenClock
from domainkit.clock import freeze_time as frozen_at
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import register_action
from domainkit.contrib.auth.testing import create_user
from domainkit.http import get_asgi_application
from domainkit.testing import FreezeTime, TestClient, authorize_act, enrol_totp
from domainkit_tests.conftest import Api
from domainkit_tests.sample_action_resources import RELABEL_PRODUCT
from domainkit_tests.test_action_authorisations_resources import name_of, stock

PAGE: Final = Path(__file__).parents[1] / "docs" / "testing.md"
ROUTES: Final = "domainkit_tests.sample_action_resources.router"
PASSWORD = "correct-horse-battery-42"


def test_relabelling_is_authorized_once(
    client: TestClient, freeze_time: FreezeTime, product_id: UUID
) -> None:
    """Asked at the twin, confirmed in the test, spent by the act once."""
    freeze_time("2027-01-04T10:00:00Z")  # Codes need a clock that moves on.
    alice = client.run(
        create_user, "shop.view_product", "shop.change_product", password=PASSWORD
    )
    as_alice = client.authenticated_as(alice)
    enrol_totp(as_alice, PASSWORD)  # A way to confirm: asking needs one.
    act = f"/products/{product_id}/relabel"
    authorization = authorize_act(as_alice, act, json={"name": "Desk lamp"})
    sent = {"Action-Authorization": authorization}
    relabelled = as_alice.post(act, json={"name": "Desk lamp"}, headers=sent)
    again = as_alice.post(act, json={"name": "Desk lamp"}, headers=sent)
    assert (relabelled.status_code, again.status_code) == (200, 403)


test_relabelling_is_authorized_once.__test__ = False  # type: ignore[attr-defined]
"""The page's function needs its fixtures: run below, not collected."""


def test_the_page_shows_the_example_this_module_runs() -> None:
    """The page's block is the function this module runs, character for character."""
    assert inspect.getsource(test_relabelling_is_authorized_once) in PAGE.read_text()


def test_the_example_authorizes_an_act_once(identity: Api) -> None:
    """Run as written: a user with an app, the twin, confirmed in the test, once."""
    with ExitStack() as stack:
        stack.enter_context(override_settings(ROOT_ROUTER=ROUTES))
        register_action(RELABEL_PRODUCT, domain="shop")
        client = stack.enter_context(TestClient(get_asgi_application()))

        def freeze_time(moment: str) -> FrozenClock:
            return stack.enter_context(frozen_at(moment))

        lamp = stock("Lamp")
        test_relabelling_is_authorized_once(client, freeze_time, lamp)

    assert name_of(lamp) == "Desk lamp"

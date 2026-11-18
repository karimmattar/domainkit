"""Fixtures: a clock for the codes, a maker, and an approver."""

import pytest
from domainkit.testing import FreezeTime, FrozenClock, TestClient

from tests.people import APPROVER, MAKER, Person


@pytest.fixture(autouse=True)
def clock(freeze_time: FreezeTime) -> FrozenClock:
    """One clock for the codes and the authorizations.

    Each authenticator code moves it to the next 30-second step.
    """
    return freeze_time("2027-01-04T10:00:00Z")


@pytest.fixture
def maker(client: TestClient) -> Person:
    """A clerk who sends payouts to suppliers."""
    return Person(client, *MAKER)


@pytest.fixture
def approver(client: TestClient) -> Person:
    """Another person, who may approve large payouts."""
    return Person(client, *APPROVER)

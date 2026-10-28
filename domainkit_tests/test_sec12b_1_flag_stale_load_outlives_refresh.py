"""SEC-12b-1: a load that read before a flag change stores after its refresh.

``invalidate`` replaces the flags' entry (``Cache.refresh``) and counts the
change (``VERSION_KEY``), so a reload that raced another change loads
again. But a replica's ordinary miss (``get_or_set``) that read the
database before the change, and stores after the refresh, is counted by
nobody: its rows, from before the change, overwrite the refreshed entry,
and every replica sharing the cache is served the flag as it was, for a
whole ``CACHE_TTL``. A kill switch turned off stays on, where the settings
say processes sharing a Redis cache "see it at once".
"""

import asyncio

import pytest

from domainkit.contrib.flags.store import current, invalidate
from domainkit_tests.test_flags import (  # noqa: F401 - fixtures.
    declared,
    project,
)
from domainkit_tests.test_flags_replicas import (  # noqa: F401 - fixtures.
    Loads,
    Replicas,
    replicas,
    set_everyone,
)

pytestmark = pytest.mark.usefixtures("project")


def test_a_flag_turned_off_is_not_served_on_after_its_change_lands(
    replicas: tuple[Replicas, Loads],  # noqa: F811 - the fixture.
) -> None:
    """Replica 1 misses and reads ``kill`` on; it is turned off and refreshed."""
    shared, loads = replicas

    async def scenario() -> bool:
        await set_everyone("kill", on=True)
        loads.held = True
        reader = shared.run(1, current())  # A miss: load 0 reads "on", held.
        await loads.read[0].wait()
        loads.held = False
        await set_everyone("kill", on=False)  # The kill switch, committed.
        await shared.run(0, invalidate())  # Load 1 reads "off" and stores it.
        loads.release(0)  # The miss stores what it read before the change.
        await reader
        rules = (await shared.run(2, current())).flags
        return rules["kill"]["everyone"]

    served = asyncio.run(scenario())

    assert served is False, (
        "every replica is served the flag as it was before it was turned off"
    )

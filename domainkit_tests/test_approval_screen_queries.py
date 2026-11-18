"""An approval screen reads its groups' names in one statement (audit A28-PERF-07).

The screens of ``auth.set_groups``, ``auth.reactivate`` and a group's
permissions name each group a change holds. However many there are, they
are read at once: a change of thirty groups reads as many statements as a
change of one.
"""

from uuid import uuid4

import pytest

from domainkit.contrib.approvals.builtin import _group_names
from domainkit.management.base import run_async
from domainkit.testing.queries import CapturedQueries
from domainkit_tests.test_auth_admin import make_group


@pytest.mark.usefixtures("auth_project")
def test_group_names_are_read_at_once_however_many() -> None:
    """One group, or thirty and one gone: the same statements; the gone one left out."""
    one = make_group("auditors", "shop.view_product")
    many = [make_group(f"team-{number}", "shop.view_product") for number in range(30)]
    gone = uuid4()

    with CapturedQueries() as alone:
        named = run_async(_group_names([one]))
    with CapturedQueries() as together:
        all_named = run_async(_group_names([*many, gone, many[0]]))

    assert named == {one: "auditors"}
    assert all_named == {group: f"team-{n}" for n, group in enumerate(many)}
    assert len(together) == len(alone)

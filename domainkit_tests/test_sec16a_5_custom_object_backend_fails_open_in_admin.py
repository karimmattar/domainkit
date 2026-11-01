"""SEC-16a-5: the admin API's object-grant bound fails open for custom backends.

AUTH-14's fix bounds staff by ``BaseBackend.object_grants``: what each
backend grants a user or group on single objects. ``BaseBackend``'s
default answers nothing, so a backend that grants on objects and does not
override the new hook (every such backend written for 0.12, which had no
hook, and the module's own ``OwnerBackend`` example) counts as granting
nothing. A staff member with ``auth.change_user`` alone then sets the
password of any holder of such grants and logs in as them: AUTH-14
exactly, for every project whose object permissions are not
``contrib.objectpermissions``. Nothing warns: no check compares a
backend that answers ``has_perm(user, perm, obj)`` with one that answers
``object_grants``. For inactive users and other backends ``_may_change``
fails closed ("only superusers"); here it fails open.

The backend below keeps mandates (user, permission, object) in a table,
as a bank's account-mandate backend would.
"""

from collections.abc import Iterator
from uuid import UUID

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.auth import admin
from domainkit.contrib.auth.backends import BaseBackend, has_perm
from domainkit.contrib.auth.models import AnyUser
from domainkit.contrib.auth.settings import auth_settings
from domainkit.ddd import PermissionDenied
from domainkit_tests.test_object_permissions import (
    CHANGE,
    person,
    products,
    project,  # noqa: F401 - the fixture.
    run,
)

NEW_PASSWORD = "Zq8!wL-d4#Kp7v"
MANDATES: set[tuple[UUID, str, UUID]] = set()
"""(user id, permission, object id): what each user may do on one object."""


class MandateBackend(BaseBackend):
    """Grants on single objects from a mandate table; nothing on whole models."""

    async def get_all_permissions(
        self,
        user: AnyUser,
        obj: object | None = None,
    ) -> frozenset[str]:
        """The permissions ``user`` holds on ``obj`` by mandate."""
        if obj is None or not user.is_active:
            return frozenset()
        object_id = getattr(obj, "id", None)
        return frozenset(
            permission
            for holder, permission, target in MANDATES
            if holder == user.id and target == object_id
        )


@pytest.fixture
def mandates(project: str) -> Iterator[None]:  # noqa: F811 - the fixture.
    """``AUTH.BACKENDS``: the model backend, then the mandates."""
    MANDATES.clear()
    backends = [
        "domainkit.contrib.auth.backends.ModelBackend",
        f"{__name__}.MandateBackend",
    ]
    auth = auth_settings().model_dump()
    with override_settings(AUTH={**auth, "BACKENDS": backends}):
        yield
    MANDATES.clear()


@pytest.mark.usefixtures("mandates")
def test_staff_cannot_take_over_a_holder_of_a_custom_backends_grant() -> None:
    """Setting the password of a mandate holder needs that mandate, or a superuser."""
    (vault,) = products("vault")
    owner = person("owner")
    MANDATES.add((owner.id, CHANGE, vault.id))
    helpdesk = person("helpdesk", "auth.change_user", is_staff=True)
    assert run(has_perm(owner, CHANGE, vault))
    assert not run(has_perm(helpdesk, CHANGE, vault))

    with pytest.raises(PermissionDenied):
        run(
            admin.set_password(
                owner.id, admin.AdminPasswordIn(password=NEW_PASSWORD), helpdesk
            )
        )

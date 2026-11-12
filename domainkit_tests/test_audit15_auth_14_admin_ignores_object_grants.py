"""AUTH-14: the admin API bounds staff by model permissions only, not object grants.

``admin.may_change`` and ``_may_grant`` compare what the actor holds with
``get_all_permissions(user)``, which asks every backend with ``obj=None``;
``ObjectPermissionBackend`` answers nothing then. So a staff member with
``auth.change_user`` alone:

- sets the password of (or the address of) a user who holds object grants
  the staff member lacks, and logs in as them ("setting a password is as
  good as becoming the user", says the module);
- joins a group that holds object grants and no model permissions, and so
  holds what ``grant_permission`` refuses to give anyone themselves.
"""

import pytest

from domainkit.contrib.auth import admin
from domainkit.contrib.auth.backends import has_perm
from domainkit.contrib.objectpermissions import grant_permission
from domainkit.ddd import PermissionDenied
from domainkit_tests.test_auth_admin import make_group
from domainkit_tests.test_object_permissions import (
    CHANGE,
    person,
    products,
    project,  # noqa: F401 - the fixture.
    reload,
    run,
)

NEW_PASSWORD = "Zq8!wL-d4#Kp7v"


def test_staff_cannot_take_over_a_user_with_object_grants_they_lack(
    project: str,  # noqa: F811 - the fixture.
) -> None:
    """Setting the password of a holder of object grants needs those grants."""
    (vault,) = products("vault")
    root = person("root", is_superuser=True)
    owner = person("owner")
    run(grant_permission(root, CHANGE, vault, user=owner.id))
    helpdesk = person("helpdesk", "auth.change_user", is_staff=True)
    assert run(has_perm(owner, CHANGE, vault))
    assert not run(has_perm(helpdesk, CHANGE, vault))

    with pytest.raises(PermissionDenied):
        run(
            admin.set_password(
                owner.id, admin.AdminPasswordIn(password=NEW_PASSWORD), helpdesk
            )
        )


def test_staff_cannot_join_a_group_whose_object_grants_they_lack(
    project: str,  # noqa: F811 - the fixture.
) -> None:
    """A group's object grants count as what joining it hands out."""
    (vault,) = products("vault")
    root = person("root", is_superuser=True)
    keepers = make_group("vault-keepers")
    run(grant_permission(root, CHANGE, vault, group=keepers))
    helpdesk = person("helpdesk", "auth.change_user", is_staff=True)

    joined = None
    try:
        run(
            admin.set_user_groups(
                helpdesk.id, admin.GroupsIn(groups=["vault-keepers"]), helpdesk
            )
        )
    except PermissionDenied:
        joined = False
    else:
        joined = run(has_perm(reload("helpdesk"), CHANGE, vault))
    assert joined is False, "helpdesk granted itself change_product on the vault"

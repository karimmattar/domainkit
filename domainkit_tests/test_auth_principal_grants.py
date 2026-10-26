"""A request's user answers authorization as the user 0.9 built did (PERF-03).

0.9 built every group, permission, and content type of a kept principal
for each request; 0.10 builds them when first read, and answers
``has_perm`` from the keys worked out once with the principal. The tests
below build both, from random grant sets, and require the same answers,
before and after the user or its groups change.
"""

import copy
import random
from typing import Any
from uuid import UUID

import pytest

from domainkit.contrib.auth import User, backends
from domainkit.contrib.auth.models import (
    GRANTED,
    AbstractBaseUser,
    Group,
    PasswordHash,
    Permission,
)
from domainkit.contrib.auth.principals import (
    IMMUTABLE,
    GrantedPermission,
    Membership,
    Principal,
    _detached,
    lend_password,
)
from domainkit.contrib.contenttypes import ContentType
from domainkit_tests.conftest import UseAuth


def seeded(seed: int) -> random.Random:
    """A generator of reproducible grant sets (not of secrets)."""
    return random.Random(seed)  # noqa: S311 - reproducible test data.


HIDDEN = PasswordHash("pbkdf2_sha256$1$salt$a-hash-no-repr-shows")
DOMAINS = ("shop", "billing", "alpha")
POOL = tuple(
    GrantedPermission(
        id=number,
        codename=f"{verb}_{model}",
        name=f"Can {verb} {model}",
        content_type_id=10 + DOMAINS.index(domain) * 3 + models.index(model),
        domain=domain,
        model=model,
    )
    for number, (domain, verb, model, models) in enumerate(
        (domain, verb, model, ("product", "order", "item"))
        for domain in DOMAINS
        for model in ("product", "order", "item")
        for verb in ("view", "change")
    )
)
"""Eighteen permissions over three domains, two of each content type."""
ASKED = tuple(sorted({each.key for each in POOL} | {"shop.delete_product", "x.y"}))


def built_eagerly(principal: Principal) -> AbstractBaseUser:
    """The user 0.9's ``Principal.user()`` built: every object, at once."""
    content_types: dict[int, ContentType] = {}
    permissions: dict[int, Permission] = {}

    def permission(granted: GrantedPermission) -> Permission:
        found = permissions.get(granted.id)
        if found is None:
            kind = content_types.get(granted.content_type_id)
            if kind is None:
                kind = content_types[granted.content_type_id] = _detached(
                    ContentType,
                    {
                        "id": granted.content_type_id,
                        "domain": granted.domain,
                        "model": granted.model,
                    },
                )
            found = permissions[granted.id] = _detached(
                Permission,
                {
                    "id": granted.id,
                    "codename": granted.codename,
                    "name": granted.name,
                    "content_type_id": granted.content_type_id,
                },
                content_type=kind,
            )
        return found

    groups = {
        _detached(
            Group,
            {"id": group.id, "name": group.name, "version": group.version},
            permissions={permission(each) for each in group.permissions},
        )
        for group in principal.groups
    }
    user: AbstractBaseUser = _detached(
        principal.model,
        {
            name: value if isinstance(value, IMMUTABLE) else copy.deepcopy(value)
            for name, value in principal.columns
        },
        groups=groups,
        user_permissions={permission(each) for each in principal.permissions},
    )
    lend_password(user, principal.password, usable=principal.usable_password)
    return user


def random_principal(rng: random.Random) -> Principal:
    """A user with random flags, groups (overlapping), and permissions."""
    groups = tuple(
        Membership(
            UUID(int=rng.getrandbits(128)),
            f"group-{index}",
            rng.randrange(1, 9),
            tuple(sorted(rng.sample(POOL, rng.randrange(0, 7)), key=lambda p: p.id)),
        )
        for index in range(rng.randrange(0, 5))
    )
    active = rng.random() < 0.85
    return Principal(
        model=User,
        is_active=active,
        columns=(
            ("id", UUID(int=rng.getrandbits(128))),
            ("version", 1),
            ("username", "alice"),
            ("is_active", active),
            ("is_superuser", rng.random() < 0.1),
        ),
        groups=groups,
        permissions=tuple(
            sorted(rng.sample(POOL, rng.randrange(0, 6)), key=lambda p: p.id)
        ),
        usable_password=True,
        password=HIDDEN,
    )


async def answers(user: AbstractBaseUser) -> dict[str, Any]:
    """Everything authorization asks a user, and what the backends answer."""
    return {
        "every": user.get_all_permissions(),
        "direct": user.get_user_permissions(),
        "by_group": user.get_group_permissions(),
        "has": {key: user.has_perm(key) for key in ASKED},
        "all_of": user.has_perms(ASKED[:3]),
        "domains": {domain: user.has_domain_perms(domain) for domain in DOMAINS},
        "backends": {key: await backends.has_perm(user, key) for key in ASKED[::3]},
        "ahas": await user.ahas_perms(ASKED[:2]),
    }


def change(user: AbstractBaseUser, rng: random.Random) -> None:
    """One random change a handler might make, the same for both users.

    ``rng`` decides; each user finds its own objects by id.
    """
    action = rng.choice(("revoke", "leave", "grant", "group_revoke", "active"))
    if action == "active":
        user.set_active(not user.is_active)
    elif action == "revoke" and user.user_permissions:
        user.revoke(min(user.user_permissions, key=lambda p: p.id))
    elif action == "leave" and user.groups:
        user.leave(min(user.groups, key=lambda g: g.name))
    elif action == "grant":
        reachable = [each for group in user.groups for each in group.permissions]
        if reachable:
            user.grant(min(reachable, key=lambda p: p.id))
    elif action == "group_revoke":
        holding = [group for group in user.groups if group.permissions]
        if holding:
            group = min(holding, key=lambda g: g.name)
            group.revoke(max(group.permissions, key=lambda p: p.id))


@pytest.mark.anyio
async def test_built_later_or_at_once_users_answer_alike(use_auth: UseAuth) -> None:
    """300 random grant sets, each changed three times: the same answers."""
    use_auth()
    rng = seeded(20261026)
    for _ in range(300):
        principal = random_principal(rng)
        old, new = built_eagerly(principal), principal.user()
        assert GRANTED in new.__dict__
        assert await answers(new) == await answers(old)
        assert "groups" not in new.__dict__  # Answered from the keys alone.
        assert "user_permissions" not in new.__dict__
        seed = rng.getrandbits(32)
        for step in range(3):
            change(old, seeded(seed + step))
            change(new, seeded(seed + step))
            assert await answers(new) == await answers(old)


@pytest.mark.anyio
async def test_a_group_read_answers_from_its_keys_until_its_permissions_are(
    use_auth: UseAuth,
) -> None:
    """Groups built, their permissions not yet: still the keys, then the objects."""
    use_auth()
    rng = seeded(7)
    principal = random_principal(rng)
    while not any(group.permissions for group in principal.groups):
        principal = random_principal(rng)
    user = principal.user()

    names = sorted(group.name for group in user.groups)

    assert names == sorted(group.name for group in principal.groups)
    assert all("permissions" not in group.__dict__ for group in user.groups)
    assert {group.name: group.permission_keys() for group in user.groups} == {
        group.name: group.keys for group in principal.groups
    }
    for group in user.groups:
        assert {each.key for each in group.permissions} == group.permission_keys()
    assert await answers(user) == await answers(built_eagerly(principal))


def test_users_with_the_same_grants_share_their_keys(use_auth: UseAuth) -> None:
    """Keys are worked out once per grant set, not once per user or request."""
    use_auth()
    rng = seeded(3)
    first = random_principal(rng)
    again = Principal(
        model=first.model,
        is_active=first.is_active,
        columns=first.columns,
        groups=first.groups,
        permissions=first.permissions,
        usable_password=True,
        password=HIDDEN,
    )

    for name in ("user_permissions", "groups", "*"):
        assert again.granted[name] is first.granted[name]
    assert first.user().__dict__[GRANTED] is first.granted
    empty = Principal(User, True, (), (), (), True, HIDDEN)
    assert empty.granted["*"] is empty.granted["groups"]
    assert not empty.granted["*"]

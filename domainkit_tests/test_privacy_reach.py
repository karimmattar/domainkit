"""Erasure and export reach every store of a person the framework keeps.

The audit found the phones, identity, and devices apps keeping a person's
numbers, provider accounts, and keys that ``privacy erase`` and ``export``
never reached (A6-1).
"""

import uuid
from typing import Any, Final

from sqlalchemy import insert, select

from domainkit.apps import registry
from domainkit.apps.hooks import on_erase_subject
from domainkit.contrib.auth.integration import utc_now
from domainkit.db import connections, metadata
from domainkit.management.base import run_async
from domainkit_tests.conftest import IDENTITY, IDENTITY_DOMAINS, Migrated, Shape
from domainkit_tests.support.auth import create_user

KEEPERS: Final = Shape(
    "privacy_reach",
    IDENTITY.message,
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [
            *IDENTITY_DOMAINS,
            "domainkit.contrib.notifications",
            "domainkit.contrib.phones",
            "domainkit.contrib.identity",
            "domainkit.contrib.devices",
            "domainkit.contrib.privacy",
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_phones_routes.router",
    },
)


def test_a_persons_numbers_identities_and_devices_are_exported_and_erased(
    migrated: Migrated,
) -> None:
    """Each app answers for what it keeps; nothing of it is left once erased."""
    from domainkit.contrib.devices.tables import push_tokens
    from domainkit.contrib.identity.tables import identities
    from domainkit.contrib.phones.tables import phones
    from domainkit.contrib.privacy.operations import erase_subject, export_subject

    with migrated(KEEPERS):
        ann = create_user("ann", email="ann@example.com")

        async def lifecycle() -> tuple[Any, list[Any]]:
            now = utc_now()
            async with connections.engine().begin() as connection:
                await connection.execute(
                    insert(phones).values(
                        id=uuid.uuid4(),
                        user_id=ann,
                        phone="+33612345678",
                        verified_at=now,
                    )
                )
                await connection.execute(
                    insert(identities).values(
                        id=uuid.uuid4(),
                        user_id=ann,
                        provider="idp",
                        subject="1098765",
                        email="ann@example.com",
                        created_at=now,
                    )
                )
                await connection.execute(
                    insert(push_tokens).values(
                        id=uuid.uuid4(),
                        user_id=ann,
                        digest="d" * 64,
                        token="device-token",
                        platform="fcm",
                        created_at=now,
                        last_seen_at=now,
                    )
                )
            exported = await export_subject(str(ann))
            await erase_subject(str(ann))
            left: list[Any] = []
            async with connections.engine().connect() as connection:
                for table in (phones, identities, push_tokens):
                    left += (
                        await connection.execute(
                            select(table.c.id).where(table.c.user_id == ann)
                        )
                    ).all()
            return exported, left

        exported, left = run_async(lifecycle())

    copies = exported["copies"]
    assert copies["phones"] == {
        "phones": [
            {
                "phone": "+33612345678",
                "verified_at": copies["phones"]["phones"][0]["verified_at"],
            }
        ]
    }
    assert copies["identity"]["identities"][0]["subject"] == "1098765"
    assert copies["devices"] == {
        "push_tokens": [
            {
                "platform": "fcm",
                "created_at": copies["devices"]["push_tokens"][0]["created_at"],
            }
        ]
    }
    assert left == []


def test_every_table_naming_a_user_belongs_to_a_domain_that_erases(
    migrated: Migrated,
) -> None:
    """A store of a person's rows with no erase hook is how A6-1 happened."""
    with migrated(KEEPERS):
        erasing = {hook.domain for hook in on_erase_subject}
        installed = {config.label for config in registry}
        keeping = {
            table.info.get("domain")
            for table in metadata.sorted_tables
            if "user_id" in table.c and table.info.get("domain") in installed
        }

    assert keeping - erasing - {"auth"} == set()

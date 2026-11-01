"""``resealmfa``: move every TOTP secret to ``SECRET_KEY``, before a fallback goes.

TOTP secrets are encrypted under a key derived from ``SECRET_KEY``
(``domainkit.encryption.SecretBox``), and a login moves its user's secret
off a key of ``SECRET_KEY_FALLBACKS``. Users who do not log in keep theirs
there, so before dropping a fallback, run this: it seals every secret a
fallback opens again under ``SECRET_KEY``. ``--check`` only counts, and
fails while a secret is still under a fallback, or under no key held
(AUTH-17).
"""

from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Final

from domainkit.encryption import DecryptionFailed
from domainkit.management.base import BaseCommand, CommandError, run_async

from ...mfa import secret_box
from ...uow import AuthUnitOfWork

BATCH_SIZE: Final = 500
"""Authenticators read, and sealed again, per transaction."""


@dataclass(slots=True)
class Resealed:
    """What a pass over the secrets found, and did."""

    current: int = 0
    """Under ``SECRET_KEY`` already."""
    fallback: int = 0
    """Under a fallback (sealed again, unless only checked)."""
    unreadable: int = 0
    """Under no key held: their users need ``resetmfa``."""


class Command(BaseCommand):
    """Seal TOTP secrets again under ``SECRET_KEY``, or count where they are."""

    help = (
        "Encrypt every TOTP secret again under SECRET_KEY, so a key of "
        "SECRET_KEY_FALLBACKS can be dropped; --check only counts."
    )
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """``--check``, and the batch size."""
        parser.add_argument(
            "--check",
            action="store_true",
            help="change nothing; fail while a secret is under a fallback or no key",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=BATCH_SIZE,
            help=f"authenticators per transaction ({BATCH_SIZE})",
        )

    def handle(self, *, check: bool, batch_size: int) -> None:
        """Count, or seal again, and say what was found."""
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")
        if check:
            found = run_async(reseal(batch_size, write=False))
        else:
            found = run_async(self._reseal(batch_size))
        moved = "under a fallback" if check else "sealed again under SECRET_KEY"
        self.write(
            f"TOTP secrets: {found.current} under SECRET_KEY, {found.fallback} "
            f"{moved}, {found.unreadable} under no key held."
        )
        if found.unreadable:
            raise CommandError(
                f"{found.unreadable} TOTP secret(s) open under no key held: their "
                "users cannot use their authenticator; turn it off with resetmfa."
            )
        if check and found.fallback:
            raise CommandError(
                f"{found.fallback} TOTP secret(s) are still under a key of "
                "SECRET_KEY_FALLBACKS; run 'manage.py resealmfa' before dropping it."
            )

    async def _reseal(self, batch_size: int) -> Resealed:
        """``reseal``, recorded as the operator's action."""
        async with self.recorded("mfa_reseal") as details:
            found = await reseal(batch_size, write=True)
            details.update(
                current=found.current,
                resealed=found.fallback,
                unreadable=found.unreadable,
            )
        return found


async def reseal(batch_size: int, *, write: bool) -> Resealed:
    """Go through every TOTP secret; with ``write``, seal a fallback's again."""
    box = secret_box()
    found = Resealed()
    after = None
    while True:
        async with AuthUnitOfWork() as uow:
            page = await uow.mfa.sealed(after=after, limit=batch_size)
            for device in page:
                associated = device.user_id.bytes
                try:
                    secret, current = box.decrypt(device.secret, associated=associated)
                except DecryptionFailed:
                    found.unreadable += 1
                    continue
                if current:
                    found.current += 1
                    continue
                found.fallback += 1
                if write:
                    sealed = box.encrypt(secret, associated=associated)
                    await uow.mfa.reseal(device.id, sealed)
            await uow.commit()
        if len(page) < batch_size:
            return found
        after = page[-1].id

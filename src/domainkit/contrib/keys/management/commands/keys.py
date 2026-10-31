"""``keys``: make and rewrap data keys, count what they seal, seal it again."""

import json
import re
from argparse import ArgumentParser

from domainkit import clock
from domainkit.apps import registry
from domainkit.contrib.keys import KeysSettings, key_status, reencrypt
from domainkit.contrib.keys.operations import BATCH_SIZE
from domainkit.contrib.keys.settings import KEY_ID_PATTERN
from domainkit.keys import KeyProviderError
from domainkit.keys.ring import keyring
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Manage the data keys of ``KEYS`` and what they seal."""

    help = (
        "Make a data key (generate), wrap DATA_KEYS under the KEK's newest "
        "version (rewrap), count what each key seals (status), or seal "
        "everything under CURRENT_KEY (reencrypt)."
    )
    operator_actions = True
    """A re-encryption is audited as the operator who ran it."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the four actions and their options."""
        actions = parser.add_subparsers(dest="action", required=True)
        generate = actions.add_parser("generate", help="make a data key, wrapped")
        generate.add_argument(
            "--id", dest="key_id", help="its id in DATA_KEYS (default: today's date)"
        )
        actions.add_parser(
            "rewrap", help="print DATA_KEYS wrapped under the KEK's newest version"
        )
        status = actions.add_parser("status", help="count what each key seals")
        status.add_argument(
            "--check",
            action="store_true",
            help="fail while anything is sealed under a retired key (keys.W001)",
        )
        again = actions.add_parser(
            "reencrypt", help="seal everything still under a retired key again"
        )
        again.add_argument(
            "--batch-size",
            type=int,
            default=BATCH_SIZE,
            help=f"rows per transaction (default: {BATCH_SIZE})",
        )

    async def handle(
        self,
        *,
        action: str,
        key_id: str | None = None,
        check: bool = False,
        batch_size: int = BATCH_SIZE,
        **options: object,
    ) -> None:
        """Run the action."""
        try:
            if action == "generate":
                await self._generate(key_id or clock.today().isoformat())
            elif action == "rewrap":
                await self._rewrap()
            elif action == "status":
                await self._status(check=check)
            else:
                await self._reencrypt(batch_size)
        except KeyProviderError as exc:
            raise CommandError(str(exc)) from None

    async def _generate(self, key_id: str) -> None:
        """Print a new data key, wrapped, as a ``DATA_KEYS`` entry."""
        if not re.fullmatch(KEY_ID_PATTERN, key_id):
            raise CommandError(
                "A key's id is 1 to 32 letters, digits, '_' and '-', such as 2026-10."
            )
        ring = keyring()
        if key_id in ring.ids:
            raise CommandError(
                f"DATA_KEYS already holds {key_id!r}; choose another id."
            )
        made = await ring.provider.generate_data_key()
        self.write(json.dumps({key_id: made.wrapped}))
        self.write(
            f"Add it to KEYS['DATA_KEYS'] in every process, then set "
            f"KEYS['CURRENT_KEY'] = {key_id!r} and run 'manage.py keys reencrypt'."
        )

    async def _rewrap(self) -> None:
        """Print ``DATA_KEYS``, each wrapped again by the provider."""
        config = registry.settings(KeysSettings)
        provider = keyring().provider
        again = {
            key_id: await provider.rewrap(wrapped)
            for key_id, wrapped in config.DATA_KEYS.items()
        }
        self.write(json.dumps(again, indent=2))
        self.write("Replace KEYS['DATA_KEYS'] with these: the data keys are the same.")

    async def _status(self, *, check: bool) -> None:
        """Print what each key seals; ``check`` fails while a retired key still does."""
        ring = keyring()
        await ring.load()
        status = await key_status(ring)
        retired = ", ".join(status.retired) or "none"
        self.write(f"Current key: {status.current}; retired: {retired}.")
        for column in status.columns:
            counts = ", ".join(
                f"{key}: {count}" for key, count in column.by_key.items()
            )
            line = (
                f"{column.database} {column.table}.{column.column}: {counts}; "
                f"subjects' keys: {column.subjects}"
            )
            if column.other:
                line += f"; under no key held: {column.other}"
            if column.stale_indexes:
                line += f"; indexes to remake: {column.stale_indexes}"
            self.write(line)
        for database, wrapped in status.subject_keys.items():
            listed = ", ".join(
                f"{key}: {count}" for key, count in sorted(wrapped.items())
            )
            self.write(f"{database} subjects' keys wrapped under {listed or 'none'}")
        if check and status.on_retired:
            raise CommandError(
                f"keys.W001: {status.on_retired} value(s), index(es), or subject "
                "key(s) are still under a retired key; run 'manage.py keys reencrypt'."
            )

    async def _reencrypt(self, batch_size: int) -> None:
        """Move everything to the current key, recorded as the operator's action."""
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")
        ring = keyring()
        await ring.load()
        async with self.recorded("keys_reencrypt", current=ring.current) as details:
            done = await reencrypt(ring, batch_size=batch_size)
            details.update(
                values=done.values,
                indexes=done.indexes,
                subject_keys=done.subject_keys,
                failed=done.failed,
            )
        self.write(
            f"Sealed {done.values} value(s) again, remade {done.indexes} blind "
            f"index(es), and wrapped {done.subject_keys} subject key(s) under "
            f"{ring.current}."
        )
        if done.failed:
            raise CommandError(
                f"{done.failed} value(s) did not open: tampered, or sealed under a "
                "key the ring no longer holds. They were left as they are."
            )

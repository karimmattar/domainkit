"""Keys for encrypted fields: data keys under a KMS, and per-person keys.

Install ``domainkit.contrib.keys`` and say where the key-encryption key
is::

    KEYS = {
        "PROVIDER": "aws-kms",
        "AWS_KMS": {"KEY_ID": "arn:aws:kms:...", "REGION": "eu-west-1"},
        "DATA_KEYS": {"2026-10": "aws-kms:AQIDAHh..."},   # manage.py keys generate
    }

Then mark fields ``Encrypted`` (``domainkit.ddd``): ``table_for`` stores
them sealed, bound to their row, and the ORM opens them as rows load. A
field sealed for a person is sealed under that person's own key, which
``erase_subject`` (``domainkit.contrib.privacy``) destroys.

- ``manage.py keys generate``: a new data key, wrapped, for ``DATA_KEYS``;
- ``manage.py keys rewrap``: ``DATA_KEYS`` wrapped under the KEK's newest version;
- ``manage.py keys status``: which key seals how many values;
- ``manage.py keys reencrypt``: move everything to ``CURRENT_KEY``.

See ``docs/contrib/keys.md``, ADR 0119 and ADR 0120.
"""

from .operations import (
    ColumnStatus,
    KeyStatus,
    Reencrypted,
    key_status,
    reencrypt,
    ring_for,
    shred_subject,
)
from .settings import AwsKmsSettings, KeysSettings, VaultSettings
from .store import KeyStoreConflict, TableSubjectKeyStore

__all__ = [
    "AwsKmsSettings",
    "ColumnStatus",
    "KeyStatus",
    "KeyStoreConflict",
    "KeysSettings",
    "Reencrypted",
    "TableSubjectKeyStore",
    "VaultSettings",
    "key_status",
    "reencrypt",
    "ring_for",
    "shred_subject",
]

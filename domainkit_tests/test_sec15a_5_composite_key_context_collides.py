r"""SEC-15a-5: two rows of a composite key can share a sealing context.

``context`` joins the parts of the primary key with ``\x1f``, unescaped,
so ``("a\x1fb", "c")`` and ``("a", "b\x1fc")`` give the same associated
data: a value copied from one of those rows to the other opens there,
which ADR 0120 says cannot happen. Text keys come from callers (an
external reference, a merchant's own id), and nothing refuses ``\x1f``.
"""

import pytest
from sqlalchemy import Column, MetaData, String, Table

from domainkit.db.encrypted import EncryptedColumns, context
from domainkit.encryption import DecryptionFailed
from domainkit.keys import LocalKeyProvider
from domainkit.keys.ring import Keyring

accounts = Table(
    "sec15a_accounts",
    MetaData(),
    Column("merchant", String(64), primary_key=True),
    Column("reference", String(64), primary_key=True),
    *EncryptedColumns("iban"),
)


def test_a_value_of_one_composite_key_does_not_open_in_another() -> None:
    """Sealed for one row, refused in the other, whose key joins the same."""
    ring = Keyring(LocalKeyProvider([b"k" * 32]), {}, "dev", known={"dev": b"d" * 32})
    sealed = ring.seal(
        b"DE89370400440532013000", context(accounts.c.iban, ["m\x1fx", "r"])
    )
    with pytest.raises(DecryptionFailed):
        ring.open(sealed, context(accounts.c.iban, ["m", "x\x1fr"]))

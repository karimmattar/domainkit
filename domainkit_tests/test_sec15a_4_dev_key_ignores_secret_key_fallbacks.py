"""SEC-15a-4: rotating SECRET_KEY destroys every value under the ``dev`` data key.

With the local provider and no ``DATA_KEYS`` (allowed anywhere with
``ALLOW_LOCAL_KEK``), the ring's one data key, ``dev``, is
``derive_key("keys.development-data-key")``: from ``SECRET_KEY`` alone,
never from ``SECRET_KEY_FALLBACKS``. Rotating ``SECRET_KEY`` the way the
rest of domainkit documents it (the old one moved to the fallbacks) then
leaves every encrypted field, and every subject's key, which is wrapped
under ``dev``, unreadable: crypto-shredding of the whole database. The
local KEK itself does honour the fallbacks.
"""

from pydantic import SecretStr

from domainkit.conf import override_settings
from domainkit.contrib.keys import KeysSettings, ring_for

OLD = SecretStr("old-secret-key-" + "o" * 40)
NEW = SecretStr("new-secret-key-" + "n" * 40)
CONTEXT = b"cards_cardholders\x00card_number\x00ann"


def test_a_value_under_the_dev_key_opens_after_secret_key_rotates() -> None:
    """Sealed under SECRET_KEY, opened once it is a fallback."""
    with override_settings(SECRET_KEY=OLD, SECRET_KEY_FALLBACKS=[]):
        sealed = ring_for(KeysSettings()).seal(b"4111111111111111", CONTEXT)
    with override_settings(SECRET_KEY=NEW, SECRET_KEY_FALLBACKS=[OLD]):
        ring = ring_for(KeysSettings())
        assert ring.open(sealed, CONTEXT) == b"4111111111111111"


def test_a_value_under_the_dev_key_opens_once_a_data_key_is_added() -> None:
    """Following "Rotating: a data key": generate one, add it, make it current."""
    import secrets

    from domainkit.keys import LocalKeyProvider

    with override_settings(SECRET_KEY=OLD, SECRET_KEY_FALLBACKS=[]):
        sealed = ring_for(KeysSettings()).seal(b"4111111111111111", CONTEXT)
        wrapped = LocalKeyProvider.from_secret_key().wrap_now(secrets.token_bytes(32))
        ring = ring_for(KeysSettings(DATA_KEYS={"2026-11": wrapped}))
        assert "dev" in ring.ids, "the dev key left the ring: nothing can move off it"
        assert ring.open(sealed, CONTEXT) == b"4111111111111111"

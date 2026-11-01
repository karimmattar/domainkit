"""SEC-15a-6: production accepts Vault over plain HTTP, token and data keys in clear.

``VAULT.URL`` takes ``http://`` (for a development Vault), and nothing
refuses it where deployed: every call then sends ``X-Vault-Token`` in
clear, and ``decrypt`` answers every data key in clear, at each start.
``keys.E001`` refuses the far weaker-sounding local KEK there, but not
this, which hands the data keys to anyone on the path.
"""

from domainkit import setup
from domainkit.apps import registry
from domainkit.checks import CheckMessage
from domainkit.conf import override_settings
from domainkit_tests.conftest import ALPHA, domain_problems


def test_production_refuses_vault_over_plain_http() -> None:
    """A deployed check names ``VAULT.URL`` over http."""
    registry.clear()
    keys = {
        "PROVIDER": "vault",
        "VAULT": {"URL": "http://vault.internal:8200", "KEY": "k", "TOKEN": "t"},
        "DATA_KEYS": {"a": "vault:v1:x"},
    }
    with override_settings(
        INSTALLED_DOMAINS=["domainkit.contrib.keys", ALPHA], KEYS=keys
    ):
        setup()
        with override_settings(ENVIRONMENT="production"):
            problems = domain_problems("keys", None)
    registry.clear()
    found = [p.id for p in problems if isinstance(p, CheckMessage)]
    assert found, "Vault over http passes every check in production"

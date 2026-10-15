"""The user model in use (``AUTH.USER_MODEL``) and its table."""

from typing import TYPE_CHECKING

from sqlalchemy import Table

from domainkit.apps import registry
from domainkit.exceptions import ImproperlyConfigured

from .models import AbstractBaseUser

if TYPE_CHECKING:
    from .apps import AuthConfig

LABEL = "auth"


def get_user_model() -> type[AbstractBaseUser]:
    """The user class (Django's ``get_user_model``).

    Raises:
        ImproperlyConfigured: ``domainkit.contrib.auth`` is not installed.
    """
    return _config().user_model


def get_user_table() -> Table:
    """The table the user class is mapped onto."""
    return _config().user_table


def _config() -> "AuthConfig":
    """The installed auth app (not a project's own domain labelled ``auth``)."""
    from .apps import AuthConfig

    try:
        config = registry.get_domain_config(LABEL)
    except LookupError:
        pass
    else:
        if isinstance(config, AuthConfig):
            return config
    raise ImproperlyConfigured("Install 'domainkit.contrib.auth' in INSTALLED_DOMAINS.")

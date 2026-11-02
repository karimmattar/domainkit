"""The user model in use (``AUTH.USER_MODEL``) and its table."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, get_type_hints

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Table

from domainkit.apps import registry
from domainkit.exceptions import ImproperlyConfigured

from .exceptions import InvalidUserField
from .models import AbstractBaseUser

if TYPE_CHECKING:
    from .apps import AuthConfig

LABEL = "auth"

FieldValue = str | bool | int | float | None
"""A value a client may give a user field."""


def get_user_model() -> type[AbstractBaseUser]:
    """The user class (Django's ``get_user_model``).

    Raises:
        ImproperlyConfigured: ``domainkit.contrib.auth`` is not installed.
    """
    return _config().user_model


def get_user_table() -> Table:
    """The table the user class is mapped onto."""
    return _config().user_table


def typed_fields(
    model: type[AbstractBaseUser], fields: Mapping[str, FieldValue]
) -> dict[str, object]:
    """``fields``, each converted to the type the user model declares for it.

    A date given as text becomes a ``date``, a number an ``int``; the
    field's markers are checked when the user is made or changed.

    Raises:
        InvalidUserField: A value does not fit its field.
    """
    types = get_type_hints(model)
    typed = {}
    for name, value in fields.items():
        try:
            typed[name] = TypeAdapter(types[name]).validate_python(value)
        except ValidationError as error:
            reason = error.errors()[0]["msg"]
            raise InvalidUserField(f"{name}: {reason}.") from None
    return typed


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

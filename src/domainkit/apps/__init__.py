"""Installed domains (``django.apps``): configuration classes and the registry."""

from domainkit.apps.config import DomainConfig
from domainkit.apps.registry import DomainRegistry, registry

__all__ = ["DomainConfig", "DomainRegistry", "registry"]

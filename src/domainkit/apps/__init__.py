"""Installed domains (``django.apps``): configuration classes and the registry."""

from domainkit.apps.config import DomainConfig, Requirement
from domainkit.apps.registry import DomainRegistry, registry

__all__ = ["DomainConfig", "DomainRegistry", "Requirement", "registry"]

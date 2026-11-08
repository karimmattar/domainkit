"""Every router domainkit's apps ship can be mounted in one project, side by side."""

import pkgutil
from importlib import import_module

from fastapi import APIRouter
from fastapi.routing import APIRoute

import domainkit.contrib

SKIPPED = ("management", "migrations", "testing", "tasks")
"""Modules with no routes, whose import registers commands, tasks, or fixtures."""


def shipped_routes() -> dict[str, set[str]]:
    """The documented routes of every app's routers: each name, by its endpoints."""
    names: dict[str, set[str]] = {}
    for info in pkgutil.walk_packages(domainkit.contrib.__path__, "domainkit.contrib."):
        if any(part in SKIPPED for part in info.name.split(".")):
            continue
        try:
            module = import_module(info.name)
        except ModuleNotFoundError:  # An extra this environment lacks.
            continue
        for value in vars(module).values():
            if not isinstance(value, APIRouter):
                continue
            for route in value.routes:
                if isinstance(route, APIRoute) and route.include_in_schema:
                    endpoint = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
                    names.setdefault(route.name, set()).add(endpoint)
    return names


def test_no_two_routes_share_a_name() -> None:
    """Operation ids are route names (``OPENAPI_OPERATION_IDS``): one route each.

    Two apps' routes of one name make ``check`` refuse a project that
    mounts both (openapi.E001), as the docs have projects do.
    """
    names = shipped_routes()

    shared = {name: ends for name, ends in names.items() if len(ends) > 1}

    assert shared == {}
    assert "list_sign_ins" in names  # ciba's routes were found.
    assert "bind_device" in names  # devices' too.

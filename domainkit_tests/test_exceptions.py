"""One root for the framework's exceptions, and errors mapped at the HTTP layer."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import httpx2
import pytest
from starlette.exceptions import HTTPException

import domainkit
from domainkit.contrib.auth.hashers import PasswordChecksBusy
from domainkit.ddd.exceptions import DomainError, NotFound
from domainkit.ddd.fields import InvalidQuery
from domainkit.deadlines import DeadlineExceeded
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import DomainkitError, ImproperlyConfigured, ServiceBusy
from domainkit.http.client import CircuitOpen
from domainkit.http.errors import APIError, busy_refusal
from domainkit.http.websockets import ConnectionClosed
from domainkit.ledger.base import LedgerKeyNotFound
from domainkit.resources.batches import failure

ROOT: Final = Path(__file__).parents[1]

NEEDS_DOMAINS: Final = frozenset({"domainkit.contrib.audit.tasks"})
"""Modules that read their app's settings on import; they define no class."""
SCRIPT: Final = """
import importlib, json, pkgutil, sys, warnings
import domainkit
from domainkit.exceptions import DomainkitError
warnings.simplefilter("ignore", DeprecationWarning)
skipped = ("templates", "migration_env", "__main__")
found, outside = 0, []
for info in pkgutil.walk_packages(domainkit.__path__, "domainkit."):
    if set(skipped) & set(info.name.split(".")) or info.name in sys.argv[1:]:
        continue
    for value in list(vars(importlib.import_module(info.name)).values()):
        if (
            isinstance(value, type)
            and issubclass(value, BaseException)
            and value.__module__ == info.name
            and not issubclass(value, Warning)
        ):
            found += 1
            if not issubclass(value, DomainkitError):
                outside.append(f"{info.name}.{value.__qualname__}")
print(json.dumps({"found": found, "outside": outside}))
"""
"""Import every module in a process of its own, and list the exceptions it
defines outside the root: importing a deprecated module here would hide its
warning from the test that expects it."""


def test_every_framework_exception_is_a_domainkit_error() -> None:
    """``except DomainkitError`` catches whatever the framework raises (DSGN-06).

    Warnings are the one exception: a ``DomainkitDeprecationWarning`` is a
    ``DeprecationWarning``, filtered as warnings are, not an error.
    """
    package = Path(domainkit.__file__).parents[1]
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DOMAINKIT_")
    }
    environment["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(package)])
    done = subprocess.run(  # noqa: S603 - the test interpreter and a fixed script.
        [sys.executable, "-c", SCRIPT, *sorted(NEEDS_DOMAINS)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    walked = json.loads(done.stdout.splitlines()[-1])
    assert walked["found"] > 100
    assert walked["outside"] == []
    assert not issubclass(DomainkitDeprecationWarning, DomainkitError)


def test_modules_left_out_define_no_classes() -> None:
    """The modules the walk cannot import define no class it could miss."""
    root = Path(domainkit.__file__).parent
    for name in NEEDS_DOMAINS:
        path = root.joinpath(*name.split(".")[1:]).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not [node for node in tree.body if isinstance(node, ast.ClassDef)]


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (ImproperlyConfigured, Exception),
        (NotFound, DomainError),
        (InvalidQuery, ValueError),
        (DeadlineExceeded, TimeoutError),
        (ConnectionClosed, OSError),
        (LedgerKeyNotFound, LookupError),
        (APIError, HTTPException),
        (CircuitOpen, httpx2.TransportError),
        (PasswordChecksBusy, ServiceBusy),
    ],
)
def test_the_root_is_added_beside_the_bases_each_error_had(
    error: type[BaseException], kept: type[BaseException]
) -> None:
    """``except ValueError`` and the like still catch what they caught."""
    assert issubclass(error, kept)
    assert issubclass(error, DomainkitError)


def test_a_busy_service_is_no_http_error_and_maps_to_503() -> None:
    """The hashers raise ``ServiceBusy``; the HTTP layer gives it a status."""
    busy = PasswordChecksBusy(7)

    assert not isinstance(busy, HTTPException)
    assert str(busy) == busy.message
    refusal = busy_refusal(busy)
    assert (refusal.status_code, refusal.code, refusal.detail) == (
        503,
        "password_checks_busy",
        "Too many password checks at once; try again in a moment.",
    )
    assert refusal.headers == {"Retry-After": "7"}
    plain = ServiceBusy("The printer is busy.")
    assert (plain.code, plain.retry_after) == ("service_busy", 1)


def test_a_busy_service_in_a_batch_is_a_503_result() -> None:
    """A batch item that meets a busy service fails as a request would."""
    result = failure(PasswordChecksBusy(2))

    assert result is not None
    assert result.error is not None
    assert (result.status, result.error.code) == (503, "password_checks_busy")
    assert result.error.detail == (
        "Too many password checks at once; try again in a moment."
    )

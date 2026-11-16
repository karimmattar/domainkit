"""Checks of uploads for the tests, at a module's top level.

A check runs in a process of its own, which loads it by name: these
import little, so that the process starts fast. ``held`` reads a path
from the file, notes its process id beside it, and waits until a file of
that path appears, so that a test holds a check while it runs.
"""

import os
import signal
import time
from pathlib import Path
from typing import IO

from domainkit.exceptions import ImproperlyConfigured
from domainkit.inspection import FileRefused


def no_secrets(file: IO[bytes]) -> None:
    """Refuse a file with ``SECRET`` in it, with a code of its own."""
    if b"SECRET" in file.read():
        raise FileRefused("Files with secrets are not accepted.", code="secret")


def held(file: IO[bytes]) -> None:
    """Note ``<path>.started`` (this process's id), then wait for ``<path>``."""
    path = Path(file.read().decode())
    path.with_suffix(".started").write_text(str(os.getpid()))
    deadline = time.monotonic() + 30
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


def spin(file: IO[bytes]) -> None:
    """Never end: only a limit stops it."""
    while True:
        pass


def hog(file: IO[bytes]) -> None:
    """Take a gigabyte."""
    bytearray(1024 * 1024 * 1024)


def over_cpu(file: IO[bytes]) -> None:
    """End as the system's CPU limit ends a process."""
    os.kill(os.getpid(), signal.SIGXCPU)


def unconfigured(file: IO[bytes]) -> None:
    """Want a library that is not installed."""
    raise ImproperlyConfigured("This check needs a library.")


def broken(file: IO[bytes]) -> None:
    """Fail as a bug does."""
    raise KeyError("a bug")


def chatty(file: IO[bytes]) -> None:
    """Print, which must not reach the answer; take the file."""
    print("noise on standard output")  # noqa: T201 - what the test sends astray.


def out_of_memory(file: IO[bytes]) -> None:
    """Fail as an allocation past the limit does."""
    raise MemoryError

"""Checks of uploads for the tests, at a module's top level.

A check runs in a process of its own, which loads it by name: these
import little, so that the process starts fast. ``held`` reads a path
from the file, notes its process id beside it, and waits until a file of
that path appears, so that a test holds a check while it runs.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import IO

from domainkit.exceptions import ImproperlyConfigured
from domainkit.i18n.lazy import (
    LazyString,
    gettext_lazy,
    ngettext_lazy,
    pgettext_lazy,
)
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


def asleep(file: IO[bytes]) -> None:
    """Sleep as many seconds as the file says, on no CPU; then take it.

    Only the clock ends it sooner: a test gives it ten times its limit, so a
    clock ten times late lets it take the file.
    """
    time.sleep(float(file.read()))


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


def surroundings(file: IO[bytes]) -> None:
    """Refuse with what the process sees: its environment, session, OOM score."""
    try:
        with open("/proc/self/oom_score_adj") as score:  # noqa: PTH123
            oom = score.read().strip()
    except OSError:
        oom = "none"
    seen = {"session": os.getsid(0), "environment": sorted(os.environ), "oom": oom}
    raise FileRefused(json.dumps(seen), code="seen")


def measured(file: IO[bytes]) -> None:
    """Refuse with the size of what it was handed."""
    raise FileRefused(str(len(file.read())), code="measured")


def lazy(file: IO[bytes]) -> None:
    """Refuse with a lazy message, which needs settings to be text."""
    raise FileRefused(gettext_lazy("Files with secrets are not accepted."))


def lazy_count(file: IO[bytes]) -> None:
    """Refuse in the plural form for the secrets the file holds."""
    found = file.read().count(b"SECRET")
    raise FileRefused(
        ngettext_lazy("The file holds a secret.", "The file holds secrets.", found)
    )


def lazy_in_context(file: IO[bytes]) -> None:
    """Refuse with a lazy message looked up in a context."""
    raise FileRefused(pgettext_lazy("upload", "Refused."))


def lazy_of_its_own(file: IO[bytes]) -> None:
    """Refuse with a lazy string of a function of its own, which needs no settings."""
    raise FileRefused(LazyString(str.upper, "files with secrets are not accepted."))


def formatted(file: IO[bytes]) -> None:
    """Refuse with a lazy message filled in, which makes it text at once."""
    raise FileRefused(gettext_lazy("At most {n} pages.").format(n=3))


def quoted(file: IO[bytes]) -> None:
    """Refuse with the file's own text, its undecodable bytes kept as surrogates."""
    raise FileRefused(file.read().decode("utf-8", "surrogateescape"))


def lazy_otherwise(file: IO[bytes]) -> None:
    """Refuse with a lazy string made by hand from a lookup, which needs settings."""
    from domainkit.i18n import translation

    raise FileRefused(LazyString(translation.gettext, "Refused."))


def exits(file: IO[bytes]) -> None:
    """End the process as a script does."""
    sys.exit(3)


def crashes(file: IO[bytes]) -> None:
    """End as a library's crash does."""
    os.kill(os.getpid(), signal.SIGSEGV)


def killed(file: IO[bytes]) -> None:
    """End as the out-of-memory killer, or anyone with SIGKILL, ends a process."""
    os.kill(os.getpid(), signal.SIGKILL)


def shouts(file: IO[bytes]) -> None:
    """Write 100 KB to standard error, then leave without an answer."""
    sys.stderr.write("noise " * 20_000 + "the end")
    sys.stderr.flush()
    os._exit(4)

"""SEC-12c-2: the image ``init`` writes bakes in the env file its own comment names.

The generated Dockerfile tells the operator to run the image with
``--env-file production.env`` (the file holding SECRET_KEY, DATABASE_URL with
its password, REDIS_URL), and then copies the whole build context with
``COPY . .``. The generated ``.dockerignore`` keeps out ``.env`` only, so a
``production.env`` (or ``.env.production``) next to the Dockerfile, where that
command reads it from, goes into the image: anyone who can pull the image
reads the production secrets in ``/app``. ``.gitignore`` does not list it
either.
"""

import fnmatch
import io
import re
from pathlib import Path

import pytest

from domainkit.management import call_command


def ignored(patterns: list[str], path: str) -> bool:
    """Whether ``.dockerignore`` keeps ``path`` (relative, ``/``-separated) out.

    Docker's rules: each pattern is matched against the path and each of its
    parent directories; the last matching pattern wins, ``!`` re-includes.
    """
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    keep_out = False
    for raw in patterns:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        pattern = pattern.removeprefix("!").strip("/")
        if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
            keep_out = not negate
    return keep_out


@pytest.mark.parametrize("secrets", ["production.env", ".env.production"])
def test_the_env_file_the_image_runs_with_stays_out_of_it(
    tmp_path: Path, secrets: str
) -> None:
    """What ``docker run --env-file production.env`` reads is not copied in."""
    project = tmp_path / "billing"
    call_command("init", "billingAPI", str(project), stdout=io.StringIO())
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
    patterns = (project / ".dockerignore").read_text(encoding="utf-8").splitlines()

    # The fix names an env file outside the project, so outside the build
    # context; one kept next to the Dockerfile anyway stays out of the image.
    named = re.findall(r"--env-file (\S+)", dockerfile)
    assert named
    assert all(path.startswith("/etc/") for path in named)
    assert "COPY . ." in dockerfile
    assert ignored(patterns, ".env")  # The rule itself works.
    assert ignored(patterns, secrets), f"{secrets} is copied into /app"

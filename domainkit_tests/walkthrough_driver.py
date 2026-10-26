"""Send a walkthrough's ``curl`` lines to a generated project's application.

Run as ``python walkthrough_driver.py <results>`` in the project's
directory, with the lines as a JSON list on stdin; it writes one JSON result
per line to ``<results>`` (stdout carries the application's logs). The
application is served in this process through Starlette's test client, at
the address ``runserver`` listens on, so no server or port is needed and
``curl`` need not be installed. Only the shapes the docs use are understood,
and anything else is refused, so a page cannot drift into a command this
harness would run differently from a shell:

- ``curl [-s] [-i] [-X METHOD] [-H "Name: value"]... [-d BODY] URL``
- ``NAME=$(curl ... | python -c "...")``: the body piped to the Python line,
  whose output (stripped) is kept as ``$NAME`` for later lines.

A trailing ``# 401`` (any three digits) states the status the line expects;
without one, it must succeed (2xx).
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVER = "http://127.0.0.1:8000"
"""Where ``runserver`` listens by default: the pages' URLs start with it."""
_ASSIGNMENT = re.compile(r"(?P<name>[A-Z_]+)=\$\((?P<command>.*)\)")
_VARIABLE = re.compile(r"\$(?P<name>[A-Z_]+)\b")
_EXPECTED = re.compile(r"\s+#\s*(?P<status>\d{3})(?:\s.*)?$")


class Refused(Exception):
    """A line the harness does not understand."""


def expected_status(line: str) -> tuple[str, int | None]:
    """Split off a trailing ``# NNN``: the line, and the status it expects."""
    match = _EXPECTED.search(line)
    if match is None:
        return line, None
    return line[: match.start()], int(match["status"])


def expand(line: str, variables: dict[str, str]) -> str:
    """Replace ``$NAME`` with the value an earlier line kept."""

    def value(match: re.Match[str]) -> str:
        if match["name"] not in variables:
            raise Refused(f"${match['name']} was never set: {line}")
        return variables[match["name"]]

    return _VARIABLE.sub(value, line)


def curl(client: Any, words: list[str]) -> tuple[int, str]:  # noqa: ANN401 - TestClient.
    """Send what ``curl`` would; return the status and what it would print."""
    if not words or words[0] != "curl":
        raise Refused(f"not a curl command: {shlex.join(words)}")
    method, headers, body, url, include = None, {}, None, None, False
    options = iter(words[1:])
    for word in options:
        if word in {"-s", "--silent"}:
            continue
        if word in {"-i", "--include"}:
            include = True
        elif word in {"-X", "--request"}:
            method = next(options)
        elif word in {"-H", "--header"}:
            name, _, header = next(options).partition(":")
            headers[name.strip()] = header.strip()
        elif word in {"-d", "--data"}:
            body = next(options)
        elif word.startswith("-") or url is not None:
            raise Refused(f"curl {word!r} is not understood here")
        else:
            url = word
    if url is None or not url.startswith(SERVER + "/"):
        raise Refused(f"curl needs a URL under {SERVER}: {shlex.join(words)}")
    if body is not None and "content-type" not in {name.lower() for name in headers}:
        headers["Content-Type"] = "application/x-www-form-urlencoded"  # As curl.
    response = client.request(
        method or ("POST" if body is not None else "GET"),
        url.removeprefix(SERVER),
        headers=headers,
        content=body,
    )
    printed = response.text
    if include:
        lines = [f"HTTP/1.1 {response.status_code} {response.reason_phrase}"]
        lines += [f"{name}: {value}" for name, value in response.headers.items()]
        printed = "\n".join(lines) + "\n\n" + printed
    return response.status_code, printed


def run_line(client: Any, line: str, variables: dict[str, str]) -> dict[str, Any]:  # noqa: ANN401 - TestClient.
    """Run one line; keep what it assigns; return its status and output."""
    command, expected = expected_status(line)
    assignment = _ASSIGNMENT.fullmatch(command.strip())
    if assignment is None:
        status, printed = curl(client, shlex.split(expand(command, variables)))
    else:
        request, _, pipe = assignment["command"].partition(" | ")
        python = shlex.split(pipe)
        if len(python) != 3 or python[:2] != ["python", "-c"]:
            raise Refused(f"only '| python -c \"...\"' may follow curl: {line}")
        status, printed = curl(client, shlex.split(expand(request, variables)))
        kept = subprocess.run(  # noqa: S603 - this interpreter, the page's own line.
            [sys.executable, "-c", python[2]],
            input=printed,
            capture_output=True,
            text=True,
            check=False,
        )
        printed = kept.stdout + kept.stderr
        variables[assignment["name"]] = kept.stdout.strip()
    return {"line": line, "status": status, "expected": expected, "output": printed}


def main() -> None:
    """Serve the project in this process and run every line read from stdin."""
    project = Path.cwd()
    os.environ.setdefault("DOMAINKIT_PROJECT_ROOT", str(project))
    sys.path.insert(0, str(project / "src"))
    from config.asgi import application  # type: ignore[import-not-found]
    from starlette.testclient import TestClient

    variables: dict[str, str] = {}
    lines = json.loads(sys.stdin.read())
    with (
        TestClient(application, base_url=SERVER) as client,
        Path(sys.argv[1]).open("w", encoding="utf-8") as results,
    ):
        for line in lines:
            results.write(json.dumps(run_line(client, line, variables)) + "\n")


if __name__ == "__main__":
    main()

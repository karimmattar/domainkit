"""Send a walkthrough's ``curl`` lines to a generated project's application.

Run as ``python walkthrough_driver.py <results>`` in the project's
directory, with ``{"lines": [...], "typed": {...}, "variables": {...},
"exported": [...]}`` as JSON on stdin; it writes one JSON result per line
to ``<results>``, then the shell's variables and exported names as they
are after the last line (stdout carries the application's logs). ``typed``
is what the user types at each ``read``; ``variables`` and ``exported``
are what the shell held before, so a page goes on from the one before it. The
application is served in this process through Starlette's test client, at
the address ``runserver`` listens on, so no server or port is needed and
``curl`` need not be installed. Only the shapes the docs use are understood,
and anything else is refused, so a page cannot drift into a command this
harness would run differently from a shell:

- ``curl [-s] [-i] [-X METHOD] [-H "Name: value"]... [-d BODY] URL``, where
  ``-d @-`` sends what was piped in, as curl does (line breaks dropped);
- ``NAME=$([python -c "..." |] curl ... | python -c "...")``: each part's
  output piped to the next, the last one's (stripped) kept as ``$NAME``;
- ``read -rsp "Prompt: " NAME && export NAME``: what the user typed for
  ``NAME``, exported to the Python lines that follow, as a shell would;
  ``{"totp": "SECRET"}`` typed is the code the user's authenticator app
  shows for the secret ``$SECRET`` holds, a step's code once, as the
  service takes it: after a step given already, the next one's;
- ``unset NAME``: ``NAME`` forgotten, for the lines that follow.

A trailing ``# 401`` (any three digits) states the status the line expects;
without one, it must succeed (2xx).
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER = "http://127.0.0.1:8000"
"""Where ``runserver`` listens by default: the pages' URLs start with it."""
_ASSIGNMENT = re.compile(r"(?P<name>[A-Z_]+)=\$\((?P<command>.*)\)")
_VARIABLE = re.compile(r"\$(?P<name>[A-Z_]+)\b")
_READ = re.compile(r'read -rsp "[^"]*" (?P<name>[A-Z_]+) && export (?P=name)')
_UNSET = re.compile(r"unset (?P<name>[A-Z_]+)")
_EXPECTED = re.compile(r"\s+#\s*(?P<status>\d{3})(?:\s.*)?$")


STEP = 30
"""Seconds an authenticator app shows each code (RFC 6238's default)."""


class Refused(Exception):
    """A line the harness does not understand."""


def typed_value(
    typed: str | Mapping[str, str],
    variables: dict[str, str],
    steps: dict[str, int],
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """What the user types: ``typed``, or the code of the app it names.

    An app's code is of a step the service has not seen from it (``steps``
    holds the last one given, by secret), as the service refuses a step
    twice. It takes the step after its own too (``AUTH.MFA.WINDOW``, 1):
    a second code in one step is the next step's, typed at once, and only
    a third waits for the clock.
    """
    if isinstance(typed, str):
        return typed
    from domainkit.testing import totp_code

    secret = variables[typed["totp"]]
    step = max(int(now() // STEP), steps.get(secret, -1) + 1)
    while step > int(now() // STEP) + 1:
        sleep(max(0.0, (step - 1) * STEP - now()))
    steps[secret] = step
    return totp_code(secret, datetime.fromtimestamp(step * STEP, UTC))


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


def curl(client: Any, words: list[str], piped: str = "") -> tuple[int, str]:  # noqa: ANN401 - TestClient.
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
            if body == "@-":
                body = piped.replace("\r", "").replace("\n", "")
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


def python(words: list[str], piped: str) -> str:
    """Run ``python -c "..."`` on what was piped in; return what it printed."""
    done = subprocess.run(  # noqa: S603 - this interpreter, the page's own line.
        [sys.executable, "-c", words[2]],
        input=piped,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout + done.stderr


def pipeline(command: str, variables: dict[str, str]) -> list[list[str]]:
    """The parts of ``a | b | c``: one curl, and ``python -c "..."`` lines.

    Raises:
        Refused: Another program, no curl or two, or curl last.
    """
    parts = [shlex.split(expand(part, variables)) for part in command.split(" | ")]
    for words in parts:
        if words[0] != "curl" and (len(words) != 3 or words[:2] != ["python", "-c"]):
            raise Refused(f'only curl and python -c "..." may be piped: {command}')
    if [words[0] for words in parts].count("curl") != 1 or parts[-1][0] == "curl":
        raise Refused(f'one curl, piped to python -c "...": {command}')
    return parts


def run_line(
    client: Any,  # noqa: ANN401 - TestClient.
    line: str,
    variables: dict[str, str],
    typed: Mapping[str, str | Mapping[str, str]] | None = None,
    exported: set[str] | None = None,
    steps: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run one line; keep what it assigns; return its status and output.

    ``exported`` gains the names a ``read`` exports and loses those
    ``unset`` forgets; ``steps`` the steps of the codes an app gave.
    """
    exported = set() if exported is None else exported
    steps = {} if steps is None else steps
    command, expected = expected_status(line)
    if prompt := _READ.fullmatch(command.strip()):
        name = prompt["name"]
        if typed is None or name not in typed:
            raise Refused(f"nothing was typed for ${name}: {line}")
        value = typed_value(typed[name], variables, steps)
        variables[name] = os.environ[name] = value
        exported.add(name)
        return {"line": line, "status": None, "expected": None, "output": ""}
    if forgotten := _UNSET.fullmatch(command.strip()):
        variables.pop(forgotten["name"], None)
        os.environ.pop(forgotten["name"], None)
        exported.discard(forgotten["name"])
        return {"line": line, "status": None, "expected": None, "output": ""}
    assignment = _ASSIGNMENT.fullmatch(command.strip())
    if assignment is None:
        status, printed = curl(client, shlex.split(expand(command, variables)))
    else:
        printed, status = "", 0
        for words in pipeline(assignment["command"], variables):
            if words[0] == "curl":
                status, printed = curl(client, words, printed)
            else:
                printed = python(words, printed)
        variables[assignment["name"]] = printed.strip()
    return {"line": line, "status": status, "expected": expected, "output": printed}


def main() -> None:
    """Serve the project in this process and run every line read from stdin."""
    project = Path.cwd()
    os.environ.setdefault("DOMAINKIT_PROJECT_ROOT", str(project))
    sys.path.insert(0, str(project / "src"))
    from config.asgi import application  # type: ignore[import-not-found]
    from starlette.testclient import TestClient

    given = json.loads(sys.stdin.read())
    variables: dict[str, str] = given["variables"]
    exported = set(given["exported"])
    steps: dict[str, int] = given["steps"]
    with (
        TestClient(application, base_url=SERVER) as client,
        Path(sys.argv[1]).open("w", encoding="utf-8") as results,
    ):
        for line in given["lines"]:
            result = run_line(client, line, variables, given["typed"], exported, steps)
            results.write(json.dumps(result) + "\n")
        state = {"variables": variables, "exported": sorted(exported), "steps": steps}
        results.write(json.dumps(state) + "\n")


if __name__ == "__main__":
    main()

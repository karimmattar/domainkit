"""Rebuild the data a phone's screen reads typed text with (ADR 0180).

``domainkit.contrib.auth.screens.holds_link`` reads two files the package
keeps beside it:

- ``tlds-alpha-by-domain.txt``: IANA's list of top-level domains, as
  published, its version on the first line;
- ``confusable-letters.txt``: the letters of other scripts that read as
  Latin ones, derived from Unicode's ``confusables.txt`` (UTS #39), with
  Unicode's notice.

Each release refreshes both (docs/versioning.md, "Making a release"): a
top-level domain delegated since the last copy is not read as one until
then.

Usage: ``python3 .github/scripts/screen_data.py``, in the repository: it
downloads IANA's list, Unicode's confusables and its license, and writes
the two files. ``--tlds``, ``--confusables`` and ``--license`` read local
copies instead; ``--target`` writes elsewhere.
"""

import argparse
import collections
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

TLDS_URL = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"
CONFUSABLES_URL = "https://www.unicode.org/Public/security/latest/confusables.txt"
LICENSE_URL = "https://www.unicode.org/license.txt"
TARGET = Path("src/domainkit/contrib/auth")
LEAST_DOMAINS = 1000
"""Fewer domains than this is a list cut short: IANA's holds some 1,400."""
SCRIPTS = ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE", "COPTIC", "LISU")
"""The scripts whose letters stand in for Latin ones in a spoofed name."""
BOM = chr(0xFEFF)
VERSION = re.compile(r"^# Version \d+, Last Updated .+$")
DOMAIN = re.compile(r"^(?:[A-Z0-9]+|XN--[A-Z0-9-]+)$")
UNICODE_VERSION = re.compile(r"^# Version: (\S+)$", re.MULTILINE)
HEADER = """\
# Letters of other scripts that read as Latin ones: a character, then the
# Latin letters it reads as. domainkit.contrib.auth.screens reads a
# top-level domain typed in them as the Latin one (holds_link).
#
# Derived from Unicode's confusables.txt, version {version} (UTS #39,
# https://www.unicode.org/Public/security/latest/confusables.txt): each
# character of the Latin, Cyrillic, Greek, Armenian, Cherokee, Coptic, and
# Lisu scripts that it maps to one Latin letter, after NFKC, case folding,
# and its combining marks dropped. Used under the Unicode License V3:
#
"""


def top_level_domains(text: str, least: int = LEAST_DOMAINS) -> str:
    """IANA's list, checked and kept as published: its version, then a domain a line.

    Raises:
        ValueError: It does not start with its version, a line is not a
            domain in capitals, or it holds fewer than ``least``.
    """
    lines = text.removeprefix(BOM).splitlines()
    if not lines or not VERSION.match(lines[0]):
        raise ValueError("IANA's list starts with its version line.")
    domains = lines[1:]
    if wrong := [each for each in domains if not DOMAIN.match(each)]:
        raise ValueError(f"Not a top-level domain: {wrong[0]!r}.")
    if len(domains) < least:
        raise ValueError(f"{len(domains)} top-level domains: the list is cut short.")
    return "\n".join(lines) + "\n"


def confusable_letters(confusables: str, notice: str) -> str:
    """``confusable-letters.txt``: each letter of ``SCRIPTS`` that reads as Latin.

    A character that ``confusables`` maps to one Latin letter, after NFKC,
    case folding, and its combining marks dropped, by code point, with the
    letters it reads as; Unicode's ``notice`` in the header.

    Raises:
        ValueError: ``confusables`` names no version.
    """
    version = UNICODE_VERSION.search(confusables.removeprefix(BOM))
    if version is None:
        raise ValueError("confusables.txt names its version: '# Version: X.Y.Z'.")
    found: dict[str, set[str]] = collections.defaultdict(set)
    for line in confusables.removeprefix(BOM).splitlines():
        data = line.split("#", 1)[0].strip()
        if not data:
            continue
        source, target = (part.split() for part in data.split(";")[:2])
        if len(source) != 1 or len(target) != 1:
            continue
        letter = chr(int(target[0], 16))
        if not (letter.isascii() and letter.isalpha()):
            continue
        key = _bare(unicodedata.normalize("NFKC", chr(int(source[0], 16))).casefold())
        if len(key) != 1 or key.isascii():
            continue
        if unicodedata.name(key, "").startswith(SCRIPTS):
            found[key].add(letter.lower())
    header = HEADER.format(version=version[1]) + "".join(
        f"# {line}".rstrip() + "\n" for line in notice.strip().splitlines()
    )
    body = "".join(
        f"{ord(key):04X} {' '.join(sorted(found[key]))}\n" for key in sorted(found)
    )
    return f"{header}\n{body}"


def _bare(text: str) -> str:
    """``text`` without its combining marks."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(each for each in decomposed if unicodedata.category(each)[0] != "M")


def _read(path: Path | None, url: str) -> str:
    """The local copy at ``path``, or what ``url`` serves."""
    if path is not None:
        return path.read_text(encoding="utf-8")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URLs.
        text: str = response.read().decode("utf-8")
    return text


def main(argv: list[str]) -> int:
    """Write both files from the downloads, or from the copies named."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tlds", type=Path, help="a copy of IANA's list")
    parser.add_argument("--confusables", type=Path, help="a copy of confusables.txt")
    parser.add_argument("--license", type=Path, help="a copy of Unicode's license")
    parser.add_argument("--target", type=Path, default=TARGET)
    args = parser.parse_args(argv)
    tlds = top_level_domains(_read(args.tlds, TLDS_URL))
    letters = confusable_letters(
        _read(args.confusables, CONFUSABLES_URL), _read(args.license, LICENSE_URL)
    )
    (args.target / "tlds-alpha-by-domain.txt").write_text(tlds, encoding="ascii")
    (args.target / "confusable-letters.txt").write_text(letters, encoding="utf-8")
    print(f"{tlds.splitlines()[0]}; {letters.count(chr(10))} lines of letters.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

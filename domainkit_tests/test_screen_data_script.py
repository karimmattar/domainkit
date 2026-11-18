"""The script that refreshes a screen's data reads its sources offline (ADR 0180).

``.github/scripts/screen_data.py`` rebuilds the two files ``holds_link``
reads: IANA's top-level domains, kept as published, and the letters of
other scripts that read as Latin ones, from Unicode's ``confusables.txt``.
Each release runs it (docs/versioning.md); these tests read small copies.
"""

import importlib
import io
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

ROOT: Final = Path(__file__).parents[1]
SCRIPTS: Final = ROOT / ".github" / "scripts"
SHIPPED: Final = ROOT / "src" / "domainkit" / "contrib" / "auth"
LIST: Final = "# Version 2026111700, Last Updated Tue Nov 17 07:07:01 2026 UTC\n"
CONFUSABLES: Final = "".join(
    f"{line}\n"
    for line in (
        "# confusables.txt",
        "# Version: 18.0.0",
        "0441 ;\t0063 ;\tMA\t# Cyrillic es, as c",
        "03C3 ;\t006F ;\tMA\t# Greek sigma, as o",
        "03C3 ;\t0063 ;\tMA\t# and as c",
        "1D6A8 ;\t0041 ;\tMA\t# bold capital alpha: alpha once in NFKC",
        "0627 ;\t006C ;\tMA\t# Arabic alef: a script left out",
        "0417 ;\t0033 ;\tMA\t# Cyrillic ze, as a digit",
        "FF21 ;\t0041 ;\tMA\t# full-width A: Latin once in NFKC",
        "0072 006E ;\t006D ;\tMA\t# r and n, as m: two characters",
        "",
    )
)
NOTICE: Final = "UNICODE LICENSE V3\n\nCopyright notice.\n"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script, imported as CI's scripts are: beside its siblings."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("screen_data")


def test_iana_list_is_kept_as_published_once_checked(script: ModuleType) -> None:
    """A version line, then a domain a line in capitals; one cut short is refused."""
    text = LIST + "AAA\nCOM\nXN--P1AI\n"

    assert script.top_level_domains(text, least=3) == text
    assert script.top_level_domains(chr(0xFEFF) + text, least=3) == text
    for wrong in ("AAA\nCOM\n", LIST + "com\n", LIST + "A.B\n"):
        with pytest.raises(ValueError, match=r"version|domain"):
            script.top_level_domains(wrong, least=1)
    with pytest.raises(ValueError, match="cut short"):
        script.top_level_domains(text)
    shipped = (SHIPPED / "tlds-alpha-by-domain.txt").read_text(encoding="ascii")
    assert script.top_level_domains(shipped) == shipped


def test_letters_are_those_that_read_as_one_latin_letter(script: ModuleType) -> None:
    """Of the scripts named, after NFKC and case folding; Unicode's notice kept."""
    written = script.confusable_letters(chr(0xFEFF) + CONFUSABLES, NOTICE)
    header, _, body = written.partition("\n\n0")

    assert "version 18.0.0 (UTS #39" in header
    assert "# UNICODE LICENSE V3\n#\n# Copyright notice." in header
    assert "0" + body == "03B1 a\n03C3 c o\n0441 c\n"
    with pytest.raises(ValueError, match="version"):
        script.confusable_letters("0441 ;\t0063 ;\tMA\n", NOTICE)
    shipped = (SHIPPED / "confusable-letters.txt").read_text(encoding="utf-8")
    assert shipped.startswith(written.split("version", 1)[0])


def test_the_script_writes_both_files_from_local_copies(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--tlds``, ``--confusables``, ``--license``, ``--target``: no download."""
    domains = LIST + "".join(f"D{each}\n" for each in range(1000))
    (tmp_path / "tlds.txt").write_text(domains, encoding="ascii")
    (tmp_path / "confusables.txt").write_text(CONFUSABLES, encoding="utf-8")
    (tmp_path / "license.txt").write_text(NOTICE, encoding="utf-8")
    copies = ("tlds.txt", "confusables.txt", "license.txt")
    flags = ("--tlds", "--confusables", "--license")
    argv = [
        part
        for flag, name in zip(flags, copies, strict=True)
        for part in (flag, str(tmp_path / name))
    ]

    assert script.main([*argv, "--target", str(tmp_path)]) == 0
    assert (tmp_path / "tlds-alpha-by-domain.txt").read_text() == domains
    assert (
        (tmp_path / "confusable-letters.txt")
        .read_text(encoding="utf-8")
        .endswith("0441 c\n")
    )
    assert "Version 2026111700" in capsys.readouterr().out


def test_without_copies_the_script_reads_each_source_from_its_url(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IANA's and Unicode's URLs, over HTTPS; faked here, as no test goes out."""
    asked: list[str] = []

    class Served(io.BytesIO):
        """A response: its body, closed at the end of the block."""

    def urlopen(url: str, timeout: float) -> Served:
        asked.append(url)
        return Served(b"served")

    monkeypatch.setattr(script.urllib.request, "urlopen", urlopen)

    assert script._read(None, script.TLDS_URL) == "served"
    assert script._read(None, script.CONFUSABLES_URL) == "served"
    assert asked == [script.TLDS_URL, script.CONFUSABLES_URL]
    assert all(url.startswith("https://") for url in asked)

"""``fill_docx``: placeholders Word split into runs, filled; archive and XML bounded.

The documents are built here, part by part, as Word writes them: a
placeholder spread over runs by a spelling mark, text boxes kept twice
(``mc:Choice`` and ``mc:Fallback``), headers, footers, and footnotes.
"""

import contextlib
import io
import os
import struct
import sys
import tracemalloc
import unicodedata
import warnings
import zipfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from lxml import etree

from domainkit.documents import (
    ArchiveLimits,
    DocumentError,
    PlaceholderError,
    Placeholders,
    clean_html,
    fill_docx,
    overlay_pdf,
    placeholders,
)
from domainkit.documents import docx as docx_module

W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NAMESPACES: Final = (
    f'xmlns:w="{W}" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'mc:Ignorable="w14 wp14"'
)
WORDML: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml"
MAIN: Final = f"{WORDML}.document.main+xml"
PNG: Final = bytes(range(256)) * 8
"""Bytes that hardly compress, as an image's."""


def part(root: str, body: str) -> str:
    """A part of WordprocessingML: ``body`` in ``root``, with Word's namespaces."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:{root} {NAMESPACES}>{body}</w:{root}>"
    )


def docx(
    body: str,
    *,
    header: str | None = None,
    footer: str | None = None,
    footnotes: str | None = None,
    main: str = MAIN,
    extra: Mapping[str, bytes] | None = None,
) -> bytes:
    """A Word document of ``body``'s paragraphs, and the other parts given."""
    parts = {"word/document.xml": part("document", f"<w:body>{body}</w:body>")}
    types = {"word/document.xml": main}
    for name, root, text in (
        ("word/header1.xml", "hdr", header),
        ("word/footer1.xml", "ftr", footer),
        ("word/footnotes.xml", "footnotes", footnotes),
    ):
        if text is not None:
            parts[name] = part(root, text)
            kind = {"hdr": "header", "ftr": "footer"}.get(root, root)
            types[name] = f"{WORDML}.{kind}+xml"
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="{kind}"/>'
        for name, kind in types.items()
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{overrides}</Types>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        for name, text in parts.items():
            archive.writestr(name, text)
        archive.writestr("word/styles.xml", part("styles", ""))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


def read(document: bytes, name: str = "word/document.xml") -> bytes:
    """One part of ``document``."""
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        return archive.read(name)


def paragraphs(document: bytes, name: str = "word/document.xml") -> list[str]:
    """Each paragraph's own text: a line break as a newline, a tab as a tab."""
    root = etree.fromstring(read(document, name))
    found = []
    for paragraph in root.iter(f"{{{W}}}p"):
        text = ""
        for node in paragraph.iter(f"{{{W}}}t", f"{{{W}}}br", f"{{{W}}}tab"):
            if next(node.iterancestors(f"{{{W}}}p")) is not paragraph:
                continue
            text += {"t": node.text or "", "br": "\n", "tab": "\t"}[
                etree.QName(node).localname
            ]
        found.append(text)
    return found


def runs(*texts: str, bold_first: bool = True) -> str:
    """A paragraph of one run per text; the first bold, a spelling mark between."""
    made = []
    for number, text in enumerate(texts):
        props = "<w:rPr><w:b/></w:rPr>" if bold_first and number == 0 else ""
        made.append(f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>')
    return "<w:p>" + '<w:proofErr w:type="spellStart"/>'.join(made) + "</w:p>"


# What is filled -----------------------------------------------------------------------


def test_a_placeholder_split_over_runs_takes_its_first_runs_formatting() -> None:
    """Word split ``{{client_name}}`` in three; the value goes to the bold run."""
    template = docx(runs("Dear {{cli", "ent_na", "me }}, welcome."))

    filled = fill_docx(template, {"client_name": "Salma"})

    assert paragraphs(filled) == ["Dear Salma, welcome."]
    root = etree.fromstring(read(filled))
    bold, plain, rest = root.iter(f"{{{W}}}r")
    assert bold.findtext(f"{{{W}}}t") == "Dear Salma"
    assert bold.find(f"{{{W}}}rPr/{{{W}}}b") is not None
    assert (plain.findtext(f"{{{W}}}t"), rest.findtext(f"{{{W}}}t")) == (
        "",
        ", welcome.",
    )


def test_prefixes_and_the_rest_of_the_archive_stay_as_they_were() -> None:
    """Word reads ``mc:Ignorable`` by prefix; other parts are not rewritten."""
    template = docx(runs("{{a}}"), extra={"word/media/image1.png": PNG})

    filled = fill_docx(template, {"a": "x"})

    xml = read(filled).decode()
    assert xml.startswith(
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n<w:document "
    )
    assert 'mc:Ignorable="w14 wp14"' in xml
    assert 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"' in xml
    assert "<w:b/>" in xml
    with (
        zipfile.ZipFile(io.BytesIO(template)) as before,
        zipfile.ZipFile(io.BytesIO(filled)) as after,
    ):
        assert after.namelist() == before.namelist()
        for name in before.namelist():
            if name != "word/document.xml":
                assert after.read(name) == before.read(name), name
        assert after.getinfo("word/styles.xml").date_time == (
            before.getinfo("word/styles.xml").date_time
        )


def test_several_placeholders_share_runs_and_repeat() -> None:
    """Replaced from the last, so each one's place holds; a name may come twice."""
    template = docx(
        runs("{{a}} and {{", "b}}{{a", "}} then {{ c.d }}.")
        + runs("{{b}}", bold_first=False)
    )

    filled = fill_docx(template, {"a": "1", "b": "22", "c.d": "333"})

    assert paragraphs(filled) == ["1 and 221 then 333.", "22"]


def test_tables_text_boxes_headers_footers_and_notes_are_filled() -> None:
    """A text box is two copies, Word's and the fallback; both are filled.

    A box's paragraphs are its own: the paragraph that holds the box reads
    its placeholder around it.
    """
    box = (
        "<w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx><w:txbxContent>'
        + runs("in {{box}}")
        + "</w:txbxContent></wps:txbx></wps:wsp></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>"
        + runs("in {{box}}")
        + "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )
    body = (
        f"<w:p><w:r><w:t>{{{{out</w:t></w:r>{box}<w:r><w:t>er}}}}</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc>" + runs("{{cell}}") + "</w:tc></w:tr></w:tbl>"
    )
    template = docx(
        body,
        header=runs("Ref. {{ref}}"),
        footer=runs("Page of {{client}}"),
        footnotes="<w:footnote>" + runs("{{note}}") + "</w:footnote>",
    )
    values = {
        "outer": "O",
        "box": "B",
        "cell": "C",
        "ref": "R-1",
        "client": "Salma",
        "note": "N",
    }

    filled = fill_docx(template, values)

    assert paragraphs(filled) == ["O", "in B", "in B", "C"]
    assert paragraphs(filled, "word/header1.xml") == ["Ref. R-1"]
    assert paragraphs(filled, "word/footer1.xml") == ["Page of Salma"]
    assert paragraphs(filled, "word/footnotes.xml") == ["N"]


def test_arabic_names_and_values_are_filled_as_they_are() -> None:
    """Arabic placeholders and values; the right-to-left run keeps its marks.

    Word shapes the letters when it lays the page out (or LibreOffice,
    through ``office_to_pdf``): the document holds the characters only.
    """
    rtl = (
        '<w:p><w:pPr><w:bidi/></w:pPr><w:r><w:rPr><w:rtl/><w:lang w:bidi="ar-SA"/>'
        "</w:rPr><w:t>السيد {{ اسم_</w:t></w:r><w:r><w:rPr><w:rtl/></w:rPr>"
        "<w:t>العميل }} المحترم، رقم {{رقم.العقد}}</w:t></w:r></w:p>"
    )
    filled = fill_docx(docx(rtl), {"اسم_العميل": "سلمى الخطيب", "رقم.العقد": "٢٠٢٦/٤٥"})

    assert paragraphs(filled) == ["السيد سلمى الخطيب المحترم، رقم ٢٠٢٦/٤٥"]
    xml = read(filled).decode()
    assert (
        '<w:rtl/><w:lang w:bidi="ar-SA"/></w:rPr>'
        '<w:t xml:space="preserve">السيد سلمى الخطيب'
    ) in xml
    assert "<w:bidi/>" in xml


def test_values_are_text_never_markup() -> None:
    """``<``, ``&``, and quotes are escaped as written: no element is added."""
    value = '</w:t></w:r><w:r><w:t>"injected" & <b>'
    filled = fill_docx(docx(runs("{{x}}")), {"x": value})

    assert paragraphs(filled) == [value]
    root = etree.fromstring(read(filled))
    assert len(list(root.iter(f"{{{W}}}r"))) == 1


def test_line_breaks_and_tabs_become_words() -> None:
    """A value's lines are Word's line breaks; Windows line ends too."""
    filled = fill_docx(
        docx(runs(" To: {{address}}. ")),
        {"address": "12 Nile St.\r\nCairo\n\tEgypt\r"},
    )

    assert paragraphs(filled) == [" To: 12 Nile St.\nCairo\n\tEgypt\n. "]
    xml = read(filled).decode()
    assert (
        '<w:t xml:space="preserve"> To: 12 Nile St.</w:t><w:br/>'
        '<w:t xml:space="preserve">Cairo</w:t><w:br/><w:tab/>'
        '<w:t xml:space="preserve">Egypt</w:t><w:br/><w:t xml:space="preserve">. </w:t>'
    ) in xml


def test_a_document_without_placeholders_is_given_back_whole() -> None:
    """Nothing to fill, nothing given: every part as it was."""
    template = docx(runs("No placeholder here { } {x}."))

    filled = fill_docx(template, {})

    assert read(filled) == read(template)
    assert paragraphs(filled) == ["No placeholder here { } {x}."]


# What is refused ----------------------------------------------------------------------


def test_every_disagreement_is_refused_at_once() -> None:
    """No contract goes out with a placeholder left; nor a value lost to a typo."""
    template = docx(
        runs("{{client}} {{ client name }}")
        + runs("{{city")
        + runs("total }} {{amount}}")
        + runs("{{date}}", bold_first=False),
        header=runs("{{ref}}"),
    )

    with pytest.raises(PlaceholderError) as refused:
        fill_docx(template, {"client": "Salma", "amount": "10", "cleint": "x"})

    error = refused.value
    assert error.missing == ("date", "ref")
    assert error.unused == ("cleint",)
    assert error.malformed == ("{{ client name }}", "{{city", "total }}")
    assert str(error) == (
        "The placeholders and the values do not agree: no value for {{date}}, "
        "{{ref}}; no placeholder takes cleint; not a placeholder: "
        "'{{ client name }}', '{{city', 'total }}'."
    )
    assert isinstance(error, DocumentError)
    assert isinstance(error, ValueError)


def test_unused_values_may_be_allowed() -> None:
    """One set of values for several templates: those a template lacks are fine."""
    filled = fill_docx(docx(runs("{{a}}")), {"a": "1", "b": "2"}, allow_unused=True)

    assert paragraphs(filled) == ["1"]
    with pytest.raises(PlaceholderError) as refused:
        fill_docx(docx(runs("{{a}} {{c}}")), {"a": "1"}, allow_unused=True)
    assert (refused.value.missing, refused.value.unused) == (("c",), ())


def test_a_draft_leaves_what_has_no_value_or_makes_no_placeholder_as_written() -> None:
    """For a preview: what is left to fill or to fix shows where it is.

    Each ``allow_`` lets one kind through; the others are still refused.
    """
    template = docx(
        runs("Dear {{cli", "ent}}, of {{ city }}")
        + runs("{{ client name }} and {{", "date}}")
        + runs("total }}", bold_first=False),
    )
    values = {"client": "Salma"}

    draft = fill_docx(template, values, allow_missing=True, allow_malformed=True)

    assert paragraphs(draft) == [
        "Dear Salma, of {{ city }}",
        "{{ client name }} and {{date}}",
        "total }}",
    ]
    with pytest.raises(PlaceholderError) as missing:
        fill_docx(template, values, allow_malformed=True)
    with pytest.raises(PlaceholderError) as malformed:
        fill_docx(template, values, allow_missing=True)
    assert (missing.value.missing, missing.value.malformed) == (("city", "date"), ())
    assert (malformed.value.missing, malformed.value.malformed) == (
        (),
        ("{{ client name }} and {{date}}", "total }}"),
    )


def test_a_malformed_placeholder_is_quoted_briefly() -> None:
    """Forty characters from an opening brace, or up to a closing one."""
    with pytest.raises(PlaceholderError) as opened:
        fill_docx(docx(runs("x" * 100 + "{{" + "y" * 100)), {})
    with pytest.raises(PlaceholderError) as closed:
        fill_docx(docx(runs("{{a}}" + "x" * 100 + "}}" + "y" * 100)), {"a": "1"})

    assert opened.value.malformed == ("{{" + "y" * 38,)
    assert closed.value.malformed == ("x" * 38 + "}}",)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (12, TypeError),
        (None, TypeError),
        ("a\x00b", DocumentError),
        ("\x0b", DocumentError),
    ],
)
def test_a_value_is_text_xml_can_hold(value: Any, error: type[Exception]) -> None:  # noqa: ANN401 - any value.
    """Numbers and dates are formatted by the caller; control characters refused."""
    with pytest.raises(error, match="The value of 'x'"):
        fill_docx(docx(runs("{{x}}")), {"x": value})


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: b"%PDF-1.7", "is not a Word document"),
        (lambda: zip_of({"word/document.xml": b"<x/>"}), "is not a Word document"),
        (
            lambda: docx(
                "", main="application/vnd.ms-word.document.macroEnabled.main+xml"
            ),
            "not as a template or with macros",
        ),
        (
            lambda: docx("", main=f"{WORDML}.template.main+xml"),
            "not as a template or with macros",
        ),
    ],
)
def test_only_a_word_document_is_filled(
    make: Callable[[], bytes], message: str
) -> None:
    """Not a ZIP, no content types, a template's or a macro document's body."""
    with pytest.raises(DocumentError, match=message):
        fill_docx(make(), {})


class Pipe:
    """A named pipe nobody writes to: whatever opens it to read waits for good.

    So does a parse that opens it, as one of ``/dev/stdin`` would: its test
    then ends by its timeout, and fails. No thread watches it, so nothing
    else that opens the pipe meanwhile is taken for the parser.
    """

    def __init__(self, path: Path) -> None:
        """Make the pipe at ``path``."""
        self.path = path
        os.mkfifo(path)

    def close(self) -> None:
        """Free a reader that waits, if any, and remove the pipe.

        A pipe left behind would hold whatever reads the temporary files.
        """
        with contextlib.suppress(OSError):  # ENXIO: no reader waits.
            os.close(os.open(self.path, os.O_WRONLY | os.O_NONBLOCK))
        self.path.unlink()


@pytest.fixture
def pipe(tmp_path: Path) -> Iterator[Pipe]:
    """A named pipe that holds whatever opens it."""
    made = Pipe(tmp_path / "pipe")
    yield made
    made.close()


OUTSIDE: Final = (
    '<!DOCTYPE w:document [<!ENTITY % p SYSTEM "file://{pipe}"> %p;]>',
    '<!DOCTYPE w:document [<!ENTITY % p SYSTEM "{pipe}"> %p;]>',
    '<!DOCTYPE w:document SYSTEM "file://{pipe}">',
)
"""DTDs that name an outside file: a parameter entity, by URL and by path,
and an outside subset."""


@pytest.mark.parametrize(
    ("doctype", "text"),
    [
        ('<!DOCTYPE w:document [<!ENTITY x SYSTEM "file:///etc/passwd">]>', "&x;"),
        ('<!DOCTYPE w:document SYSTEM "https://attacker.example/w.dtd">', "{{a}}"),
        (
            '<!DOCTYPE w:document [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;">]>',
            "&b;&b;&b;",
        ),
        ("<!doctype w:document>", "{{a}}"),
        ('<!-- <!ENTITY x "y"> -->', "{{a}}"),
    ],
)
def test_a_part_with_a_dtd_is_refused_unread(doctype: str, text: str) -> None:
    """No entity is expanded and nothing is fetched: Word writes no DTD."""
    body = part("document", f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>")
    xml = body.replace("?>\n", f"?>\n{doctype}\n", 1)
    template = replaced(docx(""), "word/document.xml", xml.encode())

    with pytest.raises(DocumentError, match="declares a DTD or an entity"):
        fill_docx(template, {"a": "1"})


@pytest.mark.timeout(10, method="signal")
@pytest.mark.parametrize("doctype", OUTSIDE)
@pytest.mark.parametrize("name", ["word/document.xml", "[Content_Types].xml"])
def test_a_dtd_naming_an_outside_file_is_refused_before_it_is_opened(
    pipe: Pipe, doctype: str, name: str
) -> None:
    """A parser without a DTD still opens an outside subset or parameter entity.

    A pipe that never ends (``/dev/stdin``, ``/proc/self/fd/1``) would hold
    the parse, and its thread, for good: the part is refused unread.
    """
    template = docx(runs("{{a}}"))
    original = read(template, name).decode()
    root = "w:document" if name == "word/document.xml" else "Types"
    declared = doctype.format(pipe=pipe.path).replace("w:document", root)
    xml = original.replace("?>\n", f"?>\n{declared}\n", 1)
    if not xml.startswith("<?xml"):
        xml = declared + xml

    with pytest.raises(DocumentError, match="declares a DTD or an entity"):
        fill_docx(replaced(template, name, xml.encode()), {"a": "1"})


@pytest.mark.timeout(10, method="signal")
@pytest.mark.parametrize("doctype", OUTSIDE)
def test_the_parser_resolves_an_outside_reference_to_nothing(
    pipe: Pipe, doctype: str
) -> None:
    """The second layer, under the check of the bytes: nothing is opened."""
    xml = doctype.format(pipe=pipe.path).replace("w:document", "r") + "<r/>"

    root = etree.fromstring(xml.encode(), docx_module._parser(etree))

    assert root.tag == "r"  # Parsed, the pipe not waited on.


@pytest.mark.timeout(10, method="signal")
@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_a_part_not_in_utf_8_is_not_read(pipe: Pipe, codec: str) -> None:
    """UTF-16 would hide a DTD from the check of the bytes: it is refused as such.

    With its byte order mark or without, the refusal says what it is.
    """
    xml = (
        f'<?xml version="1.0" encoding="{codec.upper()}"?>'
        f'<!DOCTYPE w:document SYSTEM "file://{pipe.path}">'
        f'<w:document xmlns:w="{W}"><w:body/></w:document>'
    )
    template = replaced(docx(""), "word/document.xml", xml.encode(codec))

    with pytest.raises(DocumentError, match="is in UTF-16 or UTF-32; it is read in"):
        fill_docx(template, {})


def test_a_part_in_utf_8_with_a_byte_order_mark_is_read() -> None:
    """Some tools write UTF-8's mark: the part is read all the same."""
    template = docx(runs("{{a}}"))
    marked = replaced(template, "word/document.xml", b"\xef\xbb\xbf" + read(template))

    assert paragraphs(fill_docx(marked, {"a": "1"})) == ["1"]


def test_an_entity_without_a_dtd_is_not_well_formed() -> None:
    """An undeclared entity stops the parse: nothing is guessed."""
    xml = part("document", "<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body>")
    template = replaced(docx(""), "word/document.xml", xml.encode())

    with pytest.raises(DocumentError, match="is not well-formed XML"):
        fill_docx(template, {})


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (ArchiveLimits(max_bytes=100), "larger than 100 bytes"),
        (ArchiveLimits(max_parts=3), "more than 3 files"),
        (ArchiveLimits(max_part_bytes=1000), "inflates past 1,000 bytes in a file"),
        (ArchiveLimits(max_total_bytes=3000), "or 3,000 in all"),
    ],
)
def test_the_archive_is_read_within_its_limits(
    limits: ArchiveLimits, message: str
) -> None:
    """The stated sizes are checked before anything is inflated."""
    template = docx(runs("{{a}}"), extra={"word/media/image1.png": PNG})

    with pytest.raises(DocumentError, match=message):
        fill_docx(template, {"a": "1"}, limits=limits)


def test_a_zip_bomb_is_refused_by_its_ratio() -> None:
    """A part that inflates a thousandfold, past 100 KiB: Apache POI's rule."""
    bomb = docx(runs("{{a}}"), extra={"word/media/zeros.bin": bytes(2 * 1024 * 1024)})

    with pytest.raises(DocumentError, match="inflates more than 100 times"):
        fill_docx(bomb, {"a": "1"})
    fill_docx(bomb, {"a": "1"}, limits=ArchiveLimits(max_ratio=10_000))


def test_an_encrypted_or_twice_named_file_is_refused() -> None:
    """Which of two files of one name Word reads is not for us to guess."""
    template = docx(runs("{{a}}"))
    with zipfile.ZipFile(io.BytesIO(template)) as archive:
        files = [(info, archive.read(info)) for info in archive.infolist()]
    twice = io.BytesIO()
    with zipfile.ZipFile(twice, "w") as archive, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns of the duplicate.
        for info, data in files:
            archive.writestr(info.filename, data)
        archive.writestr("WORD/document.xml", b"<other/>")
    locked = bytearray(
        zip_of({**{i.filename: d for i, d in files}, "word/s.bin": b"x"})
    )
    central = locked.rindex(b"word/s.bin") - 46  # The central directory's entry.
    assert locked[central : central + 4] == b"PK\x01\x02"
    locked[central + 8] |= 0x1  # Its flags: encrypted.

    with pytest.raises(DocumentError, match="two files of one name"):
        fill_docx(twice.getvalue(), {"a": "1"})
    with pytest.raises(DocumentError, match="an encrypted file"):
        fill_docx(bytes(locked), {"a": "1"})


def test_a_damaged_archive_is_refused() -> None:
    """A file whose bytes no longer match its CRC-32."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/note.txt", b"original text")
    damaged = out.getvalue().replace(b"original text", b"ORIGINAL text")

    with pytest.raises(DocumentError, match="archive is damaged"):
        fill_docx(damaged, {})


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("lxml", lambda: fill_docx(b"", {})),
        ("pypdf", lambda: overlay_pdf(b"", b"")),
        ("nh3", lambda: clean_html("")),
    ],
)
def test_each_part_names_the_extra_it_needs(
    monkeypatch: pytest.MonkeyPatch, module: str, call: Callable[[], object]
) -> None:
    """Without ``domainkit[documents]``, the error says how to install it."""
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(ImportError) as missing:
        call()

    assert str(missing.value) == (
        f'domainkit.documents needs {module}: pip install "domainkit[documents]".'
    )


def stating(document: bytes, name: str, size: int) -> bytes:
    """``document`` whose central directory states ``size`` bytes for ``name``."""
    data = bytearray(document)
    entry = data.rindex(name.encode()) - 46
    assert data[entry : entry + 4] == b"PK\x01\x02"
    struct.pack_into("<I", data, entry + 24, size)
    return bytes(data)


def test_a_file_is_inflated_no_further_than_its_stated_size() -> None:
    """A bomb that states 10 bytes costs 10: ``ZipFile.read`` inflated it whole.

    The archive's own sizes pass every limit; the file then fails its
    checksum, and memory never held what it would have inflated to.
    """
    bomb = stating(
        docx(runs("{{a}}"), extra={"word/media/bomb.bin": bytes(32 * 1024 * 1024)}),
        "word/media/bomb.bin",
        10,
    )
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match="archive is damaged"):
            fill_docx(bomb, {"a": "1"})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_only_stored_and_deflated_files_are_read(method: int) -> None:
    """Word writes no other method; bzip2 and LZMA are refused uninflated."""
    template = docx(runs("{{a}}"))
    with zipfile.ZipFile(io.BytesIO(template)) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("word/media/a.bin", b"x" * 100, compress_type=method)

    with pytest.raises(DocumentError, match="otherwise than stored or deflated"):
        fill_docx(out.getvalue(), {"a": "1"})


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/cron.d/x",
        "/abs/x",
        "word\\..\\x.txt",
        "C:x",
        "word//x.xml",
        "./word/x.xml",
        "word/./x.xml",
        "word/\x01.xml",
        "",
    ],
)
def test_a_file_is_named_by_a_plain_relative_path(name: str) -> None:
    """What the archive is written back with is never a path that climbs or roots."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns of an odd name.
        template = replaced(docx(runs("{{a}}")), name, b"x")

    with pytest.raises(DocumentError, match="not a plain relative path"):
        fill_docx(template, {"a": "1"})


def test_folders_are_kept_and_names_compared_as_people_read_them() -> None:
    """A folder's entry is a plain path; two spellings of one name are refused."""
    with_folder = replaced(docx(runs("{{a}}")), "word/media/", b"")
    composed = unicodedata.normalize("NFC", "word/résumé.xml")
    decomposed = unicodedata.normalize("NFD", "word/résumé.xml")
    twice = replaced(replaced(docx(""), composed, b"<a/>"), decomposed, b"<b/>")

    with zipfile.ZipFile(io.BytesIO(fill_docx(with_folder, {"a": "1"}))) as archive:
        assert "word/media/" in archive.namelist()
    with pytest.raises(DocumentError, match="two files of one name"):
        fill_docx(twice, {})


@pytest.mark.timeout(20, method="signal")
def test_many_placeholders_in_one_text_cost_no_more_than_they_do() -> None:
    """Each text that changes is built once.

    Rebuilt for each placeholder, a text of 300,000 took a minute; built
    once, a fraction of a second. The work runs before the placeholders
    and values are compared, so a refused template would have cost it too.
    """
    count = 300_000
    body = "<w:p><w:r><w:t>" + "{{a}} " * count + "</w:t></w:r></w:p>"
    limits = ArchiveLimits(max_ratio=100_000)

    filled = fill_docx(docx(body), {"a": "x"}, limits=limits)

    assert paragraphs(filled) == ["x " * count]


@pytest.mark.timeout(20, method="signal")
def test_paragraphs_nested_deep_cost_no_more_than_flat_ones() -> None:
    """Each text is grouped with its paragraph in one walk.

    Read paragraph by paragraph, 250 nested paragraphs over 100,000 texts
    took minutes; in one walk, a fraction of a second.
    """
    depth, texts = 250, 100_000
    body = (
        "<w:p>" * depth + "<w:r>" + "<w:t>x</w:t>" * texts + "</w:r>" + "</w:p>" * depth
    )
    template = docx(body + runs("{{a}}"))

    filled = fill_docx(template, {"a": "1"}, limits=ArchiveLimits(max_ratio=10_000))

    body = etree.fromstring(read(filled))[0]
    assert "".join(body[-1].itertext()) == "1"
    assert len(body[0].findall(f".//{{{W}}}t")) == texts


def listed_only(count: int, *, stated: int | None = None, zip64: bool = False) -> bytes:
    """An archive whose central directory lists ``count`` empty files, and nothing else.

    Its end record states ``stated`` files (``count`` unless given); a
    ZIP64 one states them in its ZIP64 end record.
    """
    entries = b"".join(
        struct.pack(
            "<4s6H3I5H2I",
            b"PK\x01\x02",
            20,
            20,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            len(name),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + name
        for name in (f"{number:x}".encode() for number in range(count))
    )
    files = count if stated is None else stated
    ending = b""
    if zip64:
        ending = struct.pack(
            "<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, files, files, len(entries), 0
        ) + struct.pack("<4sIQI", b"PK\x06\x07", 0, len(entries), 1)
        files = 0xFFFF
    end = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, files, files, len(entries), 0, 0)
    return entries + ending + end


@pytest.mark.parametrize(
    ("archive", "message"),
    [
        (lambda: listed_only(100_000, zip64=True), "holds more than 1,000 files"),
        (lambda: listed_only(30_000, stated=1), "list of files is larger than"),
    ],
)
def test_the_files_are_counted_before_zipfile_lists_them(
    archive: Callable[[], bytes], message: str
) -> None:
    """``zipfile`` reads every entry first: a long list is refused from the end record.

    A list that states one file but holds thousands is bounded by its size.
    """
    document = archive()
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match=message):
            fill_docx(document, {})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024


def test_a_list_that_understates_its_files_is_counted_again() -> None:
    """A short list that states one file and holds 1,500 is counted once read."""
    with pytest.raises(DocumentError, match="holds more than 1,000 files"):
        fill_docx(listed_only(1500, stated=1), {})


@pytest.mark.parametrize(
    "ending",
    [
        b"PK\x05\x06\x00\x00",  # An end record cut short.
        b"PK\x06\x07"
        + bytes(16)
        + struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, 0, 0, 0),
    ],
)
def test_an_archive_without_a_whole_end_record_is_no_document(ending: bytes) -> None:
    """No end record, or a ZIP64 locator pointing nowhere: ``zipfile`` refuses it."""
    with pytest.raises(DocumentError, match="is not a Word document"):
        fill_docx(b"%PDF-1.7 " + ending, {})


def zip_of(files: Mapping[str, bytes]) -> bytes:
    """A ZIP archive of ``files``."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def replaced(document: bytes, name: str, data: bytes) -> bytes:
    """``document`` with the part ``name`` replaced by ``data``."""
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    return zip_of({**files, name: data})


# Listing the placeholders -------------------------------------------------------------


def test_placeholders_are_listed_as_fill_docx_reads_them() -> None:
    """Each name once, in the order written, across runs; the body first.

    The braces that make none are listed as ``PlaceholderError`` quotes
    them. The content types name the header before the body, as Word may.
    """
    template = docx(
        runs("{{ b }} {{cli", "ent.name}} {{ b }}")
        + runs("{{ client name }}")
        + "<w:tbl><w:tr><w:tc>"
        + runs("{{cell}}")
        + "</w:tc></w:tr></w:tbl>",
        header=runs("{{ref}} {{b}}"),
        footnotes="<w:footnote>" + runs("{{note}} }}") + "</w:footnote>",
    )
    types = read(template, "[Content_Types].xml").decode()
    body = f'<Override PartName="/word/document.xml" ContentType="{MAIN}"/>'
    header = types[types.index('<Override PartName="/word/header1.xml"') :]
    header = header[: header.index("/>") + 2]
    reordered = types.replace(body, "").replace(header, header + body)
    template = replaced(template, "[Content_Types].xml", reordered.encode())

    listed = placeholders(template)

    assert listed == Placeholders(
        names=("b", "client.name", "cell", "ref", "note"),
        malformed=("{{ client name }}", "{{note}} }}"),
    )
    with pytest.raises(PlaceholderError) as refused:
        fill_docx(template, {})
    assert refused.value.malformed == listed.malformed
    assert set(refused.value.missing) == set(listed.names)
    assert placeholders(docx(runs("No placeholder."))) == Placeholders(
        names=(), malformed=()
    )


def test_parts_are_read_in_a_stable_order_whatever_the_content_types_say() -> None:
    """The body, headers, footers, footnotes, endnotes; each kind by name.

    Word lists the notes before the headers; another writer lists anything
    anyhow. ``header2`` comes before ``header10``. The errors of
    ``fill_docx`` follow the same order.
    """
    parts = {
        "word/endnotes.xml": (
            "endnotes",
            "<w:endnote>" + runs("{{e}}") + "</w:endnote>",
        ),
        "word/footnotes.xml": (
            "footnotes",
            "<w:footnote>" + runs("{{f}} }}") + "</w:footnote>",
        ),
        "word/footer1.xml": ("ftr", runs("{{ft}}")),
        "word/header10.xml": ("hdr", runs("{{h10}}")),
        "word/header2.xml": ("hdr", runs("{{h2}} {{ x y }}")),
        "word/document.xml": (
            "document",
            "<w:body>" + runs("{{body}} {{ b c }}") + "</w:body>",
        ),
    }
    kinds = {"ftr": "footer", "hdr": "header", "document": "document.main"}
    overrides = "".join(
        f'<Override PartName="/{name}" '
        f'ContentType="{WORDML}.{kinds.get(root, root)}+xml"/>'
        for name, (root, _) in parts.items()
    )
    types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f"{overrides}</Types>"
    )
    template = zip_of(
        {
            "[Content_Types].xml": types.encode(),
            **{name: part(root, text).encode() for name, (root, text) in parts.items()},
        }
    )

    listed = placeholders(template)
    with pytest.raises(PlaceholderError) as refused:
        fill_docx(template, {})

    assert listed.names == ("body", "h2", "h10", "ft", "f", "e")
    assert listed.malformed == ("{{ b c }}", "{{ x y }}", "{{f}} }}")
    assert refused.value.malformed == listed.malformed


def test_placeholders_are_read_within_the_archives_limits() -> None:
    """What ``fill_docx`` refuses to read, ``placeholders`` refuses too."""
    template = docx(runs("{{a}}"), extra={"word/media/image1.png": PNG})

    with pytest.raises(DocumentError, match="more than 3 files"):
        placeholders(template, limits=ArchiveLimits(max_parts=3))
    with pytest.raises(DocumentError, match="not a Word document"):
        placeholders(b"not a zip")
    with pytest.raises(DocumentError, match="declares a DTD"):
        placeholders(
            replaced(
                template, "word/document.xml", b'<!DOCTYPE w [<!ENTITY a "b">]><w/>'
            )
        )

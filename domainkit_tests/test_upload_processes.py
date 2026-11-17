"""Each upload's check runs in a process of its own, under the system's limits (U1).

pypdf's own work cannot be bounded from inside the process that runs it:
its constructor decodes a chain of tables before any check starts, and one
of its calls can run a minute without returning (a second review). A
check's process gets ``UPLOAD_CHECKS.MEMORY`` of address space and
``SECONDS`` of CPU, is killed past ``SECONDS`` of wall clock, and holds its
slot until it has exited, a request cancelled meanwhile included (ADR 0167).
"""

import asyncio
import io
import json
import logging
import os
import pickle
import struct
import sys
import threading
import time
import tracemalloc
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from domainkit import inspection
from domainkit.conf import override_settings
from domainkit.conf.global_settings import MIB, UploadChecksConfig
from domainkit.exceptions import ImproperlyConfigured, ServiceBusy
from domainkit.http import uploads
from domainkit.http.errors import install_exception_handlers
from domainkit.http.locale import LocaleMiddleware
from domainkit.http.uploads import (
    CHECKED_PDF,
    IMAGES,
    FileType,
    UploadRefused,
    receive_upload,
    save_upload,
)
from domainkit.i18n.catalogs import compile_po
from domainkit.i18n.lazy import LazyString
from domainkit.i18n.translation import reset_catalogs
from domainkit.inspection import (
    COMPLEX,
    DAMAGED,
    PDF_STREAM_LIMIT,
    TOO_LONG,
    TOO_MUCH,
    FileRefused,
    NotOfType,
    OfficeCheck,
)
from domainkit.storage import MemoryStorage
from domainkit_tests.support import hooks
from domainkit_tests.support.documents import office, one_page

pytestmark = pytest.mark.anyio

PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
ANY: Final = lambda head: True  # noqa: E731 - matches every file; never pickled.


def typed(check: Any) -> FileType:  # noqa: ANN401 - a check.
    """A type of any file, checked by ``check``."""
    return FileType("text/plain", ".txt", ANY, check)


@contextmanager
def limited(seconds: float) -> Iterator[None]:
    """One check at a time, of ``seconds`` and the least memory."""
    with override_settings(
        UPLOAD_CHECKS={"PROCESSES": 1, "SECONDS": seconds, "MEMORY": 128 * MIB}
    ):
        yield


@pytest.fixture
def limits() -> Iterator[None]:
    """One check at a time, of one second and the least memory."""
    with limited(1.0):
        yield


@pytest.fixture
def slots() -> Iterator[None]:
    """One check at a time, with time to spare: a loaded runner is slow to start one."""
    with limited(60.0):
        yield


async def refusal(data: bytes, kind: FileType) -> tuple[int, str, str, float]:
    """``save_upload``'s refusal of ``data``: status, code, detail, seconds taken."""
    started = time.monotonic()
    with pytest.raises(UploadRefused) as refused:
        await save_upload(data, types=[kind], max_size=10**8, storage=MemoryStorage())
    error = refused.value
    return error.status_code, error.code, error.detail, time.monotonic() - started


# What pypdf does before, or between, any bound in its process --------------------


def xref_chain(streams: int) -> bytes:
    """A PDF whose /Prev chain has ``streams`` tables, each inflating to 16 MiB.

    pypdf decodes and keeps every one in its constructor: 2 MB of upload
    took 2 GiB.
    """
    out = b"%PDF-1.7\n"
    one = len(out)
    out += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    two = len(out)
    out += b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    previous = None
    for index in range(streams):
        number = 3 + index
        offset = len(out)
        rows = []
        listed = f"[{number} 1]"
        if index == streams - 1:
            rows = [struct.pack(">BIB", 0, 0, 255), struct.pack(">BIB", 1, one, 0)]
            rows.append(struct.pack(">BIB", 1, two, 0))
            listed = f"[0 3 {number} 1]"
        rows.append(struct.pack(">BIB", 1, offset, 0))
        raw = b"".join(rows)
        data = zlib.compress(raw + bytes(PDF_STREAM_LIMIT - 64 - len(raw)))
        chain = f" /Prev {previous}" if previous is not None else ""
        out += (
            (
                f"{number} 0 obj\n<< /Type /XRef /Size {3 + streams} /Index {listed} "
                f"/W [1 4 1] /Root 1 0 R{chain} /Filter /FlateDecode "
                f"/Length {len(data)} >>\nstream\n"
            ).encode()
            + data
            + b"\nendstream\nendobj\n"
        )
        previous = offset
    return out + f"startxref\n{previous}\n%%EOF\n".encode()


def reparsed_index(entries: int, items: int) -> bytes:
    """A PDF whose one object stream's index names one object ``entries`` times.

    pypdf parses the array of ``items`` numbers again for each entry, in
    one call no clock in its process looks at: 529 bytes ran past 60 s.
    """
    header = b"0 0 " * entries
    stream = zlib.compress(header + b"[" + b"0 " * items + b"]")
    out = b"%PDF-1.7\n"
    one = len(out)
    out += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    two = len(out)
    out += b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    three = len(out)
    out += (
        (
            f"3 0 obj\n<< /Type /ObjStm /N {entries} /First {len(header)} "
            f"/Filter /FlateDecode /Length {len(stream)} >>\nstream\n"
        ).encode()
        + stream
        + b"\nendstream\nendobj\n"
    )
    five = len(out)
    rows = [
        struct.pack(">BIH", 0, 0, 65535),
        struct.pack(">BIH", 1, one, 0),
        struct.pack(">BIH", 1, two, 0),
        struct.pack(">BIH", 1, three, 0),
        struct.pack(">BIH", 2, 3, 0),
        struct.pack(">BIH", 1, five, 0),
    ]
    table = zlib.compress(b"".join(rows))
    out += (
        (
            f"5 0 obj\n<< /Type /XRef /Size 6 /W [1 4 2] /Root 1 0 R "
            f"/Filter /FlateDecode /Length {len(table)} >>\nstream\n"
        ).encode()
        + table
        + b"\nendstream\nendobj\n"
    )
    return out + f"startxref\n{five}\n%%EOF\n".encode()


def unended_strings(headers: int, filler: int) -> bytes:
    """A PDF whose content stream writes ``9 0 obj(`` many times, never closed.

    Each is parsed as an object, reading to the end of the file.
    """
    raw = b"9 0 obj(" * headers + b"A" * filler
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(raw) + raw + b"\nendstream",
    ]
    out = b"%PDF-1.7\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n"
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % start


@pytest.mark.parametrize(
    ("data", "seconds", "expected"),
    [
        (xref_chain(12), 30.0, ("unsafe_file", COMPLEX)),
        (reparsed_index(40_000, 5_000), 1.0, ("unreadable_file", TOO_LONG)),
        (unended_strings(2_000, 400_000), 1.0, ("unreadable_file", TOO_LONG)),
    ],
    ids=["xref-chain", "reparsed-index", "unended-strings"],
)
async def test_what_pypdf_does_unbounded_is_stopped_by_the_system(
    data: bytes, seconds: float, expected: tuple[str, str]
) -> None:
    """Memory or time: 422 within a few seconds, never stored.

    A chain of cross-reference streams, which pypdf decodes and keeps as
    it opens a file, took the system's memory limit to stop; since 2.7
    the decoding policy refuses it first: each stream decodes to 8 bytes
    an entry at most (ADR 0176).
    """
    with limited(seconds):
        status, code, said, taken = await refusal(data, CHECKED_PDF)

    assert (status, code, said) == (422, *expected)
    assert taken < 10


@pytest.mark.parametrize(
    ("check", "seconds", "detail"),
    [
        (hooks.spin, 1.0, TOO_LONG),
        (hooks.hog, 30.0, TOO_MUCH),
        (hooks.over_cpu, 30.0, TOO_LONG),
    ],
)
async def test_a_check_past_its_limits_is_a_refusal(
    check: Any,  # noqa: ANN401 - a check.
    seconds: float,
    detail: str,
) -> None:
    """The clock, the memory, the CPU: each ends the process, and refuses the file."""
    with limited(seconds):
        status, code, said, taken = await refusal(b"anything", typed(check))

    assert (status, code, said) == (422, "unreadable_file", detail)
    assert taken < 10


async def test_a_blank_page_fits_the_least_memory_and_less_is_refused() -> None:
    """64 MiB could not hold Python and pypdf: a blank one-page PDF was refused."""
    with limited(30.0):
        received = await receive_upload(one_page(), types=[CHECKED_PDF], max_size=10**4)
    received.close()

    assert UploadChecksConfig(MEMORY=128 * MIB).MEMORY == 128 * MIB
    with pytest.raises(ValueError, match="greater than or equal to 134217728"):
        UploadChecksConfig(MEMORY=64 * MIB)


async def test_a_checks_process_gets_no_secret_and_a_session_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not the parent's environment, nor its process group; first for the OOM killer.

    A check in the process saw every variable of the application's,
    secrets included (third review).
    """
    monkeypatch.setenv("MY_SECRET_KEY", "hunter2")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")

    status, code, said, _ = await refusal(b"x", typed(hooks.surroundings))
    seen = json.loads(said)

    assert (status, code) == (422, "seen")
    assert {"PATH", "LC_ALL"} <= set(seen["environment"])
    assert "MY_SECRET_KEY" not in seen["environment"]
    assert not [name for name in seen["environment"] if name.startswith("DOMAINKIT_")]
    assert seen["session"] != os.getsid(0)
    assert seen["oom"] == ("1000" if sys.platform == "linux" else "none")


async def test_a_path_on_sys_path_is_handed_as_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``Path`` on ``sys.path`` made every checked upload a 500."""
    monkeypatch.setattr(sys, "path", [*sys.path, tmp_path])

    received = await receive_upload(b"x", types=[typed(hooks.chatty)], max_size=10)
    received.close()

    assert received.kind.inspect is hooks.chatty


def test_what_is_no_path_on_sys_path_is_left_out(tmp_path: Path) -> None:
    """A check's process gets ``sys.path`` as text, without what is no path.

    The entries are handed to the helper, never put on ``sys.path``:
    coverage reads ``sys.path`` each time it meets a file it has not seen,
    and fails on an entry that is no path, as a checked upload with one
    there once did.
    """
    path = inspection._child_path(["/first", tmp_path, 7])

    assert path == ["/first", str(tmp_path)]


class Watched(io.BytesIO):
    """A file that notes the threads it is read in."""

    def __init__(self, data: bytes) -> None:
        """Hold ``data``; no reads yet."""
        super().__init__(data)
        self.threads: set[int] = set()

    def read(self, size: int | None = -1) -> bytes:
        """Note the thread, then read."""
        self.threads.add(threading.get_ident())
        return super().read(size)


async def test_the_whole_file_is_handed_over_and_read_in_a_thread() -> None:
    """``run_checks`` took a size, and checked only that much of the file it sent."""
    file = Watched(b"x" * 300_000)

    with pytest.raises(FileRefused) as refused:
        await inspection.run_checks(
            [hooks.measured], file, processes=1, memory=256 * MIB, seconds=30
        )

    assert refused.value.message == "300000"
    assert file.threads
    assert threading.get_ident() not in file.threads


async def test_a_checks_process_answers_what_its_check_raised() -> None:
    """A library missing, a bug, a print to standard output, a type not taken."""
    with pytest.raises(
        ImproperlyConfigured, match="in its process: This check needs a library"
    ):
        await receive_upload(b"x", types=[typed(hooks.unconfigured)], max_size=10)
    with pytest.raises(RuntimeError, match="failed in its process: KeyError"):
        await receive_upload(b"x", types=[typed(hooks.broken)], max_size=10)
    with pytest.raises(UploadRefused) as unsupported:
        await receive_upload(office("xlsx"), types=[uploads.DOCX], max_size=10**6)
    received = await receive_upload(b"x", types=[typed(hooks.chatty)], max_size=10)
    received.close()

    assert unsupported.value.status_code == 415
    assert received.kind.inspect is hooks.chatty


# A lazy refusal, made again where it is shown ----------------------------------------

SECRETS: Final = "Files with secrets are not accepted."
ARABIC_CHECKS: Final = (
    'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    '"Plural-Forms: nplurals=6; plural=n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : '
    'n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5;\\n"\n\n'
    f'msgid "{SECRETS}"\nmsgstr "لا تُقبل الملفات التي فيها أسرار."\n\n'
    'msgid "The file holds a secret."\nmsgid_plural "The file holds secrets."\n'
    'msgstr[0] "لا يحمل الملف أسرارًا."\nmsgstr[1] "يحمل الملف سرًّا."\n'
    'msgstr[2] "يحمل الملف سرّين."\nmsgstr[3] "يحمل الملف أسرارًا."\n'
    'msgstr[4] "يحمل الملف أسرارًا."\nmsgstr[5] "يحمل الملف أسرارًا."\n\n'
    'msgctxt "upload"\nmsgid "Refused."\nmsgstr "مرفوض."\n'
)
"""A project's catalog of its checks' refusals, in Arabic."""


@pytest.fixture
def arabic_checks(tmp_path: Path) -> Iterator[None]:
    """``LOCALE_PATHS`` holds ``ARABIC_CHECKS``, compiled."""
    po = tmp_path / "ar" / "LC_MESSAGES" / "messages.po"
    po.parent.mkdir(parents=True)
    po.write_text(ARABIC_CHECKS, encoding="utf-8")
    po.with_suffix(".mo").write_bytes(compile_po(ARABIC_CHECKS))
    reset_catalogs()
    with override_settings(LOCALE_PATHS=[tmp_path]):
        yield
    reset_catalogs()


@pytest.mark.parametrize(
    ("check", "data", "source"),
    [
        (hooks.lazy, b"x", ("gettext", (SECRETS,))),
        (
            hooks.lazy_count,
            b"SECRET, SECRET",
            ("ngettext", ("The file holds a secret.", "The file holds secrets.", 2)),
        ),
        (hooks.lazy_in_context, b"x", ("pgettext", ("upload", "Refused."))),
    ],
)
async def test_a_lazy_refusal_is_made_again_by_the_parent(
    check: Any,  # noqa: ANN401 - a check.
    data: bytes,
    source: tuple[str, tuple[object, ...]],
) -> None:
    """The process names its lookup; the parent makes the lazy string again.

    The process has no settings to make one text: every file such a check
    refused was a 500 (follow-up of 2.5.0).
    """
    with pytest.raises(FileRefused) as refused:
        await inspection.run_checks(
            [check], io.BytesIO(data), processes=1, memory=256 * MIB, seconds=30
        )

    message: object = refused.value.message  # Declared text; may be lazy.
    assert isinstance(message, LazyString)
    assert message.source() == source
    assert refused.value.code == "unsafe_file"


def checked_by(checks: dict[str, Any]) -> TestClient:
    """A client of an application that checks ``POST /files/<name>`` by ``checks``."""
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/files/{check}")
    async def upload(check: str, request: Request) -> None:
        kind = typed(checks[check])
        (await receive_upload(await request.body(), types=[kind], max_size=99)).close()

    return TestClient(LocaleMiddleware(app))


def refusals_logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What ``save_upload`` logged of its checks' refusals."""
    return [
        each.getMessage()
        for each in caplog.records
        if each.name == "domainkit.http.uploads"
    ]


def test_a_lazy_refusal_is_answered_in_the_callers_language(
    arabic_checks: None, caplog: pytest.LogCaptureFixture
) -> None:
    """In Arabic for a caller who asks for it, in English otherwise: 422, not 500.

    As a domain error's lazy message is since 2.5.0; the plural form is
    the caller's language's, and a context is kept. The log says it in
    English, whatever the language: as a plain refusal is logged.
    """
    caplog.set_level(logging.INFO, logger="domainkit.http.uploads")
    checks = {
        "plain": hooks.lazy,
        "count": hooks.lazy_count,
        "context": hooks.lazy_in_context,
    }
    client = checked_by(checks)
    answers = {
        (check, language): client.post(
            f"/files/{check}",
            content=b"SECRET, SECRET",
            headers={"Accept-Language": language} if language else {},
        )
        for check in checks
        for language in ("ar", None)
    }

    assert {
        key: (
            response.status_code,
            response.json()["code"],
            response.headers["Content-Language"],
            response.json()["detail"],
        )
        for key, response in answers.items()
    } == {
        ("plain", "ar"): (422, "unsafe_file", "ar", "لا تُقبل الملفات التي فيها أسرار."),
        ("plain", None): (422, "unsafe_file", "en", SECRETS),
        ("count", "ar"): (422, "unsafe_file", "ar", "يحمل الملف سرّين."),
        ("count", None): (422, "unsafe_file", "en", "The file holds secrets."),
        ("context", "ar"): (422, "unsafe_file", "ar", "مرفوض."),
        ("context", None): (422, "unsafe_file", "en", "Refused."),
    }
    assert refusals_logged(caplog) == [
        f"An upload was refused by its check: {text}"
        for text in (SECRETS, "The file holds secrets.", "Refused.")
        for _ in ("ar", None)
    ]


def test_a_refusal_in_the_files_own_words_is_utf_8_on_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bytes that are not UTF-8 make no known answer; a line break is a space.

    A check that refused with the file's own text, decoded with
    ``surrogateescape``, made a 500: no body encodes a lone surrogate. And
    a line break in it wrote a line of its own to a plain-text log.
    """
    caplog.set_level(logging.INFO, logger="domainkit.http.uploads")
    client = checked_by({"quoted": hooks.quoted})

    undecodable = client.post("/files/quoted", content=b"Not \xff accepted.")
    forged = client.post(
        "/files/quoted", content="No.\nINFO: forged\u202e.\x00".encode()
    )

    assert (undecodable.status_code, undecodable.json()["code"]) == (
        422,
        "unreadable_file",
    )
    assert undecodable.json()["detail"] == DAMAGED
    assert (forged.status_code, forged.json()["detail"]) == (422, "No. INFO: forged.")
    assert refusals_logged(caplog) == [
        f"An upload was refused by its check: {DAMAGED}",
        "An upload was refused by its check: No. INFO: forged.",
    ]


async def test_a_check_that_reads_the_settings_is_told_as_its_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Worded as the process's, and logged with where the check raised it.

    A refusal filled in (``gettext_lazy(...).format(...)``) reads the
    settings, which the process has not: the application raised "Set the
    DOMAINKIT_SETTINGS_MODULE environment variable.", as if it had none,
    and nothing was logged.
    """
    with pytest.raises(
        ImproperlyConfigured,
        match=r"^A check of an upload, in its process: Set the \w+_SETTINGS_MODULE ",
    ):
        await receive_upload(b"x", types=[typed(hooks.formatted)], max_size=10)

    (record,) = [each for each in caplog.records if each.name == "domainkit.inspection"]
    assert record.levelname == "ERROR"
    assert "ImproperlyConfigured" in record.getMessage()
    assert ", in formatted" in record.getMessage()


@pytest.mark.parametrize(
    ("said", "source"),
    [
        ({"lookup": "gettext", "arguments": ["No."]}, ("gettext", ("No.",))),
        (
            {"lookup": "ngettext", "arguments": ["A page.", "Pages.", 0]},
            ("ngettext", ("A page.", "Pages.", 0)),
        ),
        (
            {"lookup": "ngettext", "arguments": ["A page.", "Pages.", 2**64 - 1]},
            ("ngettext", ("A page.", "Pages.", 2**64 - 1)),
        ),
        (
            {"lookup": "pgettext", "arguments": ["c" * 501, "m" * 501]},
            ("pgettext", ("c" * 500, "m" * 500)),
        ),
        ({"lookup": "__import__", "arguments": ["os"]}, None),
        ({"lookup": "gettext_lazy", "arguments": ["No."]}, None),
        ({"lookup": ["gettext"], "arguments": ["No."]}, None),
        ({"lookup": "gettext", "arguments": "No."}, None),
        ({"lookup": "gettext", "arguments": []}, None),
        ({"lookup": "gettext", "arguments": ["No.", "No."]}, None),
        ({"lookup": "gettext", "arguments": [7]}, None),
        ({"lookup": "pgettext", "arguments": ["upload", None]}, None),
        ({"lookup": "ngettext", "arguments": ["A page.", "Pages.", True]}, None),
        ({"lookup": "ngettext", "arguments": ["A page.", "Pages.", 2.0]}, None),
        ({"lookup": "ngettext", "arguments": ["A page.", "Pages.", "2"]}, None),
        ({"lookup": "ngettext", "arguments": ["A page.", "Pages.", -1]}, None),
        ({"lookup": "ngettext", "arguments": ["A page.", "Pages.", 2**64]}, None),
        ({"lookup": "gettext", "arguments": ["No."], "function": "eval"}, None),
        (
            {"lookup": "pgettext", "arguments": ["up\nload", "No.\u2028Yes."]},
            ("pgettext", ("up load", "No. Yes.")),
        ),
        ({"lookup": "gettext", "arguments": ["No.\udcff"]}, None),
        ({"lookup": "ngettext", "arguments": ["A page.", "\ud800", 2]}, None),
        ({"arguments": ["No."]}, None),
        (["gettext", "No."], None),
        (None, None),
    ],
)
def test_a_lazy_refusal_is_read_as_a_lookup_of_known_shape(
    said: object, source: tuple[str, tuple[object, ...]] | None
) -> None:
    """One of three lookups, with its arguments' number and types; nothing else.

    The process read an untrusted file: it may name texts to look up, never
    code to run. A text is cut as a plain message is; a count is C's
    ``unsigned long``. Any other answer is of no known shape.
    """
    answer = json.dumps({"refused": said, "code": "unsafe_file"}).encode()

    with pytest.raises(FileRefused) as refused:
        inspection._outcome(answer, 0, b"", 1)

    message: object = refused.value.message  # Declared text; may be lazy.
    if source is None:
        assert (message, refused.value.code) == (DAMAGED, "unreadable_file")
    else:
        assert isinstance(message, LazyString)
        assert (message.source(), refused.value.code) == (source, "unsafe_file")


# A slot per process, held until it exits --------------------------------------------


@pytest.fixture
def gate(tmp_path: Path) -> Path:
    """Where ``hooks.held`` waits: it goes on once the file is there."""
    return tmp_path / "release"


async def started(gate: Path) -> int:
    """Wait until ``hooks.held`` runs; its process's id."""
    note = gate.with_suffix(".started")
    for _ in range(1000):
        if note.exists() and note.read_text():
            return int(note.read_text())
        await asyncio.sleep(0.01)
    raise AssertionError("The check never started.")


def gone(pid: int) -> bool:
    """Whether no process has the id ``pid`` (or it is a zombie, reaped soon)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    status = Path(f"/proc/{pid}/status")
    return status.exists() and "zombie" in status.read_text()


async def test_past_the_processes_one_more_check_is_503_and_others_go_on(
    slots: None, gate: Path
) -> None:
    """A type with no check takes no slot: an image passes while a PDF waits."""
    storage = MemoryStorage()
    held = typed(hooks.held)
    first = asyncio.ensure_future(
        save_upload(str(gate).encode(), types=[held], max_size=10**4, storage=storage)
    )
    pid = await started(gate)

    with pytest.raises(ServiceBusy, match="cannot be checked now"):
        await save_upload(b"two", types=[held], max_size=10, storage=storage)
    image = await receive_upload(PNG_BYTES, types=IMAGES, max_size=10**4)
    image.close()
    await asyncio.to_thread(gate.write_text, "go")
    stored = await first

    assert pid != os.getpid()
    assert list(storage.files) == [stored.name]
    assert inspection._running == 0


async def test_a_request_cancelled_kills_its_check_and_then_frees_the_slot(
    slots: None, gate: Path
) -> None:
    """Never a slot freed while its process runs.

    A deadline cut the request short, the thread of its check ran on, and
    more checks ran than the limit (second review).
    """
    held = typed(hooks.held)
    request = asyncio.ensure_future(
        save_upload(
            str(gate).encode(), types=[held], max_size=10**4, storage=MemoryStorage()
        )
    )
    pid = await started(gate)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert gone(pid)
    assert inspection._running == 0
    received = await receive_upload(b"next", types=[typed(hooks.chatty)], max_size=10)
    received.close()


async def test_a_second_cancellation_still_waits_for_the_process(
    slots: None, gate: Path
) -> None:
    """Cancelled while it kills its process, a check waits for it all the same."""
    held = typed(hooks.held)
    request = asyncio.ensure_future(
        save_upload(
            str(gate).encode(), types=[held], max_size=10**4, storage=MemoryStorage()
        )
    )
    pid = await started(gate)

    request.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert gone(pid)
    assert inspection._running == 0


@pytest.mark.parametrize(
    ("bootstrap", "status", "said"),
    [
        ("import sys; sys.exit(0)", 0, ""),
        ("import no_such_module", 1, "ModuleNotFoundError"),
    ],
)
async def test_a_process_that_ends_without_an_answer_is_an_error_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bootstrap: str,
    status: int,
    said: str,
) -> None:
    """It ended before the file was written to it: no answer, no hang, no refusal.

    Any process without an answer was a file that "needed too much memory",
    unlogged: an import that failed too (third review).
    """
    monkeypatch.setattr(inspection, "BOOTSTRAP", bootstrap)

    with pytest.raises(
        RuntimeError, match=f"without an answer \\(exit status {status}\\)"
    ):
        await inspection.run_checks(
            [hooks.chatty],
            io.BytesIO(bytes(4 * MIB)),
            processes=1,
            memory=128 * MIB,
            seconds=30,
        )

    (record,) = [each for each in caplog.records if each.name == "domainkit.inspection"]
    assert record.levelname == "ERROR"
    assert said in record.getMessage()


@pytest.mark.parametrize(
    ("check", "failed", "said"),
    [
        (hooks.exits, "SystemExit", "SystemExit: 3"),
        (hooks.lazy_otherwise, "ImproperlyConfigured", "ImproperlyConfigured"),
    ],
)
async def test_a_check_that_raises_anything_is_told_and_logged(
    caplog: pytest.LogCaptureFixture,
    check: Any,  # noqa: ANN401 - a check.
    failed: str,
    said: str,
) -> None:
    """``SystemExit``; a lazy string made by hand from a lookup, which needs settings.

    The process has none to make it text, and it names no lookup the parent
    makes again (``LazyString.source``).
    """
    with pytest.raises(RuntimeError, match=f"failed in its process: {failed}"):
        await receive_upload(b"x", types=[typed(check)], max_size=10)

    (record,) = [each for each in caplog.records if each.name == "domainkit.inspection"]
    assert record.levelname == "ERROR"
    assert said in record.getMessage()


async def test_what_a_process_says_is_kept_to_its_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """100 KB to standard error neither blocks the process nor fills the log."""
    with pytest.raises(RuntimeError, match="exit status 4"):
        await receive_upload(b"x", types=[typed(hooks.shouts)], max_size=10)

    (record,) = [each for each in caplog.records if each.name == "domainkit.inspection"]
    assert record.getMessage().endswith("noise the end")
    assert len(record.getMessage()) < inspection.MAX_SAID + 200


@pytest.mark.parametrize(
    ("check", "name", "said"),
    [
        (hooks.crashes, "SIGSEGV", "Fatal Python error"),
        (hooks.killed, "SIGKILL", ""),
    ],
)
async def test_a_process_a_signal_ended_refuses_the_file_and_is_logged(
    caplog: pytest.LogCaptureFixture,
    check: Any,  # noqa: ANN401 - a check.
    name: str,
    said: str,
) -> None:
    """A crash, or a kill no one can tell from the OOM killer's: could not be read."""
    status, code, detail, _ = await refusal(b"x", typed(check))

    assert (status, code, detail) == (422, "unreadable_file", DAMAGED)
    (record,) = [each for each in caplog.records if each.name == "domainkit.inspection"]
    assert record.levelname == "WARNING"
    assert f"ended by {name}" in record.getMessage()
    assert said in record.getMessage()


# The answer is data; the process is the check's ------------------------------------


@pytest.mark.parametrize(
    ("answer", "returncode", "raised", "match"),
    [
        (b"", 0, RuntimeError, "exit status 0"),
        (b"[1]", 0, RuntimeError, "exit status 0"),
        (b"[" * 100_000, 0, RuntimeError, "exit status 0"),
        (b"", -24, FileRefused, TOO_LONG),
        (b"", -200, FileRefused, DAMAGED),
        (b'{"took": 7}', 0, FileRefused, DAMAGED),
        (b'{"refused": "No.", "code": "Bad Code"}', -9, FileRefused, "No."),
        (b'{"none": true}', 0, NotOfType, ""),
        (b'{"improperly": "Install it."}', 0, ImproperlyConfigured, "Install it."),
        (b'{"improperly": "Install\\nit."}', 0, ImproperlyConfigured, ": Install it."),
        (b'{"failed": "ValueError"}', 0, RuntimeError, "ValueError"),
        (b'{"failed": "Value\\nError"}', 0, RuntimeError, ": Value Error."),
        (b'{"what": 1}', 0, FileRefused, DAMAGED),
    ],
)
def test_an_answer_is_read_as_data_of_known_shapes(
    answer: bytes, returncode: int, raised: type[Exception], match: str
) -> None:
    """A process that read an untrusted file may answer anything, or end anyhow."""
    with pytest.raises(raised) as error:
        inspection._outcome(answer, returncode, b"", 1)

    if isinstance(error.value, FileRefused):
        assert error.value.message == match
        assert error.value.code in {"unreadable_file", "unsafe_file"}
    else:
        assert match in str(error.value)
    assert inspection._outcome(b'{"took": 0}', -9, b"", 1) == 0


@pytest.mark.parametrize(
    ("answer", "shown"),
    [
        ({"refused": "No.\ud800"}, None),
        ({"refused": "\udcff"}, None),
        ({"refused": "x" * 499 + "\ud800"}, None),
        ({"refused": "x" * 500 + "\ud800"}, "x" * 500),
        ({"refused": "No.\nINFO: forged\u202e.\x00"}, "No. INFO: forged."),
        ({"refused": "Tab\there,\u2028there."}, "Tab here, there."),
        ({"improperly": "\ud800"}, None),
        ({"failed": "\ud800"}, None),
    ],
)
def test_a_text_from_the_process_is_utf_8_on_one_line(
    answer: dict[str, str], shown: str | None
) -> None:
    """A lone surrogate, which JSON lets through, is of no known shape.

    No body or log encodes one: a refusal that held one was a 500. White
    space becomes a space and a control or format character goes, so that
    a refusal is one line in a log, shown as it reads. Only what is kept
    of the text counts.
    """
    with pytest.raises(FileRefused) as refused:
        inspection._outcome(json.dumps(answer).encode(), 0, b"", 1)

    expected = (DAMAGED, "unreadable_file") if shown is None else (shown, "unsafe_file")
    assert (refused.value.message, refused.value.code) == expected


def test_a_checks_process_serves_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limits first; then the pickled request and the file; then JSON, always."""
    limited: list[tuple[int, float]] = []
    monkeypatch.setattr(inspection, "_limit", lambda *bounds: limited.append(bounds))

    def served(checks: list[Any], data: bytes, request: bytes | None = None) -> Any:  # noqa: ANN401 - JSON.
        body = request or pickle.dumps(checks)
        source = io.BytesIO(struct.pack(">Q", len(body)) + body + data)
        sink = io.BytesIO()
        inspection._serve({"memory": 1, "seconds": 2.0}, source, sink)
        return json.loads(sink.getvalue())

    assert served([None], b"x") == {"took": 0}
    assert served([hooks.chatty], b"x") == {"took": 0}
    assert served([hooks.unconfigured], b"x") == {
        "improperly": "This check needs a library."
    }
    assert served([OfficeCheck("docx"), None], office("xlsx")) == {"took": 1}
    assert served([OfficeCheck("docx")], office("xlsx")) == {"none": True}
    assert served([hooks.no_secrets], b"SECRET") == {
        "refused": "Files with secrets are not accepted.",
        "code": "secret",
    }
    assert served([hooks.out_of_memory], b"x") == {
        "refused": TOO_MUCH,
        "code": "unreadable_file",
    }
    assert served([], b"", request=b"not a pickle") == {"failed": "UnpicklingError"}
    assert served([hooks.exits], b"x") == {"failed": "SystemExit"}
    assert served([hooks.lazy], b"x") == {
        "refused": {
            "lookup": "gettext",
            "arguments": ["Files with secrets are not accepted."],
        },
        "code": "unsafe_file",
    }
    assert served([hooks.lazy_count], b"SECRET") == {
        "refused": {
            "lookup": "ngettext",
            "arguments": ["The file holds a secret.", "The file holds secrets.", 1],
        },
        "code": "unsafe_file",
    }
    assert served([hooks.lazy_of_its_own], b"x") == {
        "refused": "FILES WITH SECRETS ARE NOT ACCEPTED.",
        "code": "unsafe_file",
    }
    assert served([hooks.measured], b"12345") == {"refused": "5", "code": "measured"}
    assert limited == [(1, 2.0)] * 13


def test_what_pypdf_skipped_in_this_thread_is_a_refusal() -> None:
    """Skipped for want of memory, or damaged; another thread's words, never."""
    logger = logging.getLogger("pypdf")
    with inspection._Skipped() as skipped:
        thread = threading.Thread(
            target=logger.warning, args=("Previous trailer cannot be read",)
        )
        thread.start()
        thread.join()
        logger.warning("Xref table not zero-indexed.")
        skipped.check()
        logger.warning("Previous trailer cannot be read: Unable to allocate")
        with pytest.raises(FileRefused, match=TOO_MUCH):
            skipped.check()
    with inspection._Skipped() as skipped:
        logger.warning("Ignoring wrong pointing object 3 0 (offset 0)")
        with pytest.raises(FileRefused, match=DAMAGED):
            skipped.check()
    assert skipped not in logger.handlers


def test_a_process_limits_itself_where_the_system_can(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Address space and CPU; first for the OOM killer; Windows has none of them."""
    import resource

    score = tmp_path / "oom_score_adj"
    monkeypatch.setattr(inspection, "OOM_SCORE", str(score))
    asked: list[tuple[int, tuple[int, int]]] = []

    def setrlimit(kind: int, limits: tuple[int, int]) -> None:
        asked.append((kind, limits))
        if kind == resource.RLIMIT_AS:
            raise ValueError("not lower than the system's own")

    monkeypatch.setattr(resource, "setrlimit", setrlimit)
    inspection._limit(64, 1.5)
    assert asked == [(resource.RLIMIT_AS, (64, 64)), (resource.RLIMIT_CPU, (2, 3))]
    assert score.read_text() == "1000"
    monkeypatch.setitem(sys.modules, "resource", None)
    monkeypatch.setattr(inspection, "OOM_SCORE", str(tmp_path / "none" / "score"))
    inspection._limit(64, 1.5)
    assert len(asked) == 2


async def test_a_check_costs_a_process_start(slots: None) -> None:
    """What isolation costs an upload, here: a fraction of a second."""
    started_at = time.monotonic()
    received = await receive_upload(b"x", types=[typed(hooks.chatty)], max_size=10)
    received.close()

    assert time.monotonic() - started_at < 5


def test_an_office_part_is_scanned_in_its_bytes() -> None:
    """A part decoded as text took four bytes a character: 1 GiB for 5 MB."""
    wide = "<x>" + "\U0001f600" + "a" * 2_000_000 + "</x>"
    data = office(parts={"customXml/item1.xml": wide})
    tracemalloc.start()
    try:
        OfficeCheck("docx", max_expansion=10_000, max_ratio=10_000)(io.BytesIO(data))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 3 * len(wide)

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
from pathlib import Path
from typing import Any, Final

import pytest

from domainkit import inspection
from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured, ServiceBusy
from domainkit.http import uploads
from domainkit.http.uploads import (
    CHECKED_PDF,
    IMAGES,
    FileType,
    UploadRefused,
    receive_upload,
    save_upload,
)
from domainkit.inspection import (
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
from domainkit_tests.support.documents import office

pytestmark = pytest.mark.anyio

PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
ANY: Final = lambda head: True  # noqa: E731 - matches every file; never pickled.


def typed(check: Any) -> FileType:  # noqa: ANN401 - a check.
    """A type of any file, checked by ``check``."""
    return FileType("text/plain", ".txt", ANY, check)


@pytest.fixture
def limits() -> Iterator[None]:
    """One check at a time, of one second and the least memory."""
    with override_settings(
        UPLOAD_CHECKS={"PROCESSES": 1, "SECONDS": 1.0, "MEMORY": 128 * 1024 * 1024}
    ):
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
    ("data", "detail"),
    [
        (xref_chain(12), TOO_MUCH),
        (reparsed_index(40_000, 5_000), TOO_LONG),
        (unended_strings(2_000, 400_000), TOO_LONG),
    ],
    ids=["xref-chain", "reparsed-index", "unended-strings"],
)
async def test_what_pypdf_does_unbounded_is_stopped_by_the_system(
    limits: None, data: bytes, detail: str
) -> None:
    """Memory or time: 422 ``unreadable_file`` within a few seconds, never stored."""
    status, code, said, seconds = await refusal(data, CHECKED_PDF)

    assert (status, code, said) == (422, "unreadable_file", detail)
    assert seconds < 10


@pytest.mark.parametrize(
    ("check", "detail"),
    [
        (hooks.spin, TOO_LONG),
        (hooks.hog, TOO_MUCH),
        (hooks.over_cpu, TOO_LONG),
    ],
)
async def test_a_check_past_its_limits_is_a_refusal(
    limits: None,
    check: Any,  # noqa: ANN401 - a check.
    detail: str,
) -> None:
    """The clock, the memory, the CPU: each ends the process, and refuses the file."""
    status, code, said, seconds = await refusal(b"anything", typed(check))

    assert (status, code, said) == (422, "unreadable_file", detail)
    assert seconds < 10


async def test_a_checks_process_answers_what_its_check_raised() -> None:
    """A library missing, a bug, a print to standard output, a type not taken."""
    with pytest.raises(ImproperlyConfigured, match="needs a library"):
        await receive_upload(b"x", types=[typed(hooks.unconfigured)], max_size=10)
    with pytest.raises(RuntimeError, match="failed in its process: KeyError"):
        await receive_upload(b"x", types=[typed(hooks.broken)], max_size=10)
    with pytest.raises(UploadRefused) as unsupported:
        await receive_upload(office("xlsx"), types=[uploads.DOCX], max_size=10**6)
    received = await receive_upload(b"x", types=[typed(hooks.chatty)], max_size=10)
    received.close()

    assert unsupported.value.status_code == 415
    assert received.kind.inspect is hooks.chatty


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
    limits: None, gate: Path
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
    limits: None, gate: Path
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
    limits: None, gate: Path
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


async def test_a_process_that_reads_nothing_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It ended before the file was written to it: no answer, no hang."""
    monkeypatch.setattr(inspection, "BOOTSTRAP", "import sys; sys.exit(0)")

    with pytest.raises(FileRefused) as refused:
        await inspection.run_checks(
            [hooks.chatty],
            io.BytesIO(bytes(4 * 1024 * 1024)),
            4 * 1024 * 1024,
            processes=1,
            memory=128 * 1024 * 1024,
            seconds=5,
        )

    assert refused.value.message == TOO_MUCH


# The answer is data; the process is the check's ------------------------------------


@pytest.mark.parametrize(
    ("answer", "raised", "match"),
    [
        (b"", FileRefused, TOO_MUCH),
        (b"[1]", FileRefused, DAMAGED),
        (b'{"took": 7}', FileRefused, DAMAGED),
        (b'{"refused": "No.", "code": "Bad Code"}', FileRefused, "No."),
        (b'{"none": true}', NotOfType, ""),
        (b'{"improperly": "Install it."}', ImproperlyConfigured, "Install it."),
        (b'{"failed": "ValueError"}', RuntimeError, "ValueError"),
        (b'{"what": 1}', FileRefused, DAMAGED),
    ],
)
def test_an_answer_is_read_as_data_of_known_shapes(
    answer: bytes, raised: type[Exception], match: str
) -> None:
    """A process that read an untrusted file may answer anything."""
    with pytest.raises(raised) as error:
        inspection._verdict(answer, 1)

    if isinstance(error.value, FileRefused):
        assert error.value.message == match
        assert error.value.code in {"unreadable_file", "unsafe_file"}
    assert inspection._verdict(b'{"took": 0}', 1) == 0


def test_a_checks_process_serves_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limits first; then the pickled request and the file; then JSON, always."""
    limited: list[tuple[int, float]] = []
    monkeypatch.setattr(inspection, "_limit", lambda *bounds: limited.append(bounds))

    def served(checks: list[Any], data: bytes, request: bytes | None = None) -> Any:  # noqa: ANN401 - JSON.
        body = request or pickle.dumps({"checks": checks, "size": len(data)})
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
    assert limited == [(1, 2.0)] * 8


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Address space and CPU; a limit refused is let be; Windows has none."""
    import resource

    asked: list[tuple[int, tuple[int, int]]] = []

    def setrlimit(kind: int, limits: tuple[int, int]) -> None:
        asked.append((kind, limits))
        if kind == resource.RLIMIT_AS:
            raise ValueError("not lower than the system's own")

    monkeypatch.setattr(resource, "setrlimit", setrlimit)
    inspection._limit(64, 1.5)
    assert asked == [(resource.RLIMIT_AS, (64, 64)), (resource.RLIMIT_CPU, (2, 3))]
    monkeypatch.setitem(sys.modules, "resource", None)
    inspection._limit(64, 1.5)
    assert len(asked) == 2


async def test_a_check_costs_a_process_start(limits: None) -> None:
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

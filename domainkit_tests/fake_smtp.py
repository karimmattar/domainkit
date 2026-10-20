"""A small SMTP server on a thread, with STARTTLS and implicit TLS, for tests.

It speaks enough of RFC 5321 for ``smtplib``: EHLO, STARTTLS, AUTH PLAIN,
MAIL, RCPT, DATA, RSET, NOOP, and QUIT. Recipients in ``refuse`` get 550.
"""

import base64
import datetime as dt
import ipaddress
import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class Received:
    """One message the server took."""

    sender: str
    recipients: list[str]
    data: bytes


@dataclass
class SmtpServer:
    """What the server allows, and what it received."""

    tls: ssl.SSLContext | None = None
    """Offer STARTTLS with this context (or speak TLS at once, with ``implicit``)."""
    implicit: bool = False
    user: str | None = None
    password: str | None = None
    refuse: set[str] = field(default_factory=set)
    received: list[Received] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    port: int = 0


def certificate(directory: Path, name: str = "localhost") -> tuple[Path, Path]:
    """A self-signed certificate for ``name`` and 127.0.0.1, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(name),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{name}.pem", directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    """A TLS server context presenting ``cert``."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


class _Handler(socketserver.StreamRequestHandler):
    """One SMTP session."""

    server: "_Server"

    def handle(self) -> None:
        """Answer commands until QUIT or the client goes away."""
        smtp = self.server.smtp
        if smtp.implicit and smtp.tls is not None:
            self._secure(smtp.tls)
        secured = smtp.implicit
        self._reply("220 localhost ESMTP test")
        sender, recipients = "", list[str]()
        while line := self.rfile.readline():
            command = line.decode().rstrip("\r\n")
            verb = command.split(" ", 1)[0].upper()
            smtp.commands.append(verb)
            if verb in {"EHLO", "HELO"}:
                options = ["localhost"]
                if smtp.tls is not None and not secured:
                    options.append("STARTTLS")
                if smtp.user is not None:
                    options.append("AUTH PLAIN")
                lines = [f"250-{option}" for option in options[:-1]]
                self._reply("\r\n".join([*lines, f"250 {options[-1]}"]))
            elif verb == "STARTTLS" and smtp.tls is not None:
                self._reply("220 Go ahead")
                self._secure(smtp.tls)
                secured = True
            elif verb == "AUTH":
                _, _, encoded = command.split(" ", 2)
                _, user, password = base64.b64decode(encoded).decode().split("\0")
                if (user, password) == (smtp.user, smtp.password):
                    self._reply("235 Authenticated")
                else:
                    self._reply("535 Authentication failed")
            elif verb == "MAIL":
                sender, recipients = _address(command), []
                self._reply("250 OK")
            elif verb == "RCPT":
                recipient = _address(command)
                if recipient in smtp.refuse:
                    self._reply("550 No such user")
                else:
                    recipients.append(recipient)
                    self._reply("250 OK")
            elif verb == "DATA":
                self._reply("354 End with a dot")
                data = b""
                while (chunk := self.rfile.readline()) not in {b".\r\n", b""}:
                    data += chunk
                smtp.received.append(Received(sender, recipients, data))
                self._reply("250 Queued")
            elif verb == "QUIT":
                self._reply("221 Bye")
                return
            else:
                self._reply("250 OK" if verb in {"RSET", "NOOP"} else "502 No")

    def finish(self) -> None:
        """Close the files, and the TLS socket if the session switched to one."""
        super().finish()
        if self.connection is not self.request:
            self.connection.close()

    def _reply(self, text: str) -> None:
        """Send one reply."""
        self.wfile.write(f"{text}\r\n".encode())
        self.wfile.flush()

    def _secure(self, context: ssl.SSLContext) -> None:
        """Switch the connection to TLS."""
        self.connection = context.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")


def _address(command: str) -> str:
    """The address in ``MAIL FROM:<a>`` or ``RCPT TO:<a>``."""
    return command.partition("<")[2].partition(">")[0]


class _Server(socketserver.ThreadingTCPServer):
    """A threaded TCP server that knows its ``SmtpServer``."""

    daemon_threads = True
    allow_reuse_address = True
    smtp: SmtpServer


@contextmanager
def smtp_server(smtp: SmtpServer) -> Iterator[SmtpServer]:
    """Run ``smtp`` on a free port of 127.0.0.1 until the block ends."""
    server = _Server(("127.0.0.1", 0), _Handler)
    server.smtp = smtp
    smtp.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield smtp
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

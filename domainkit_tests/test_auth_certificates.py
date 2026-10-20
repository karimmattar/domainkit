"""Client certificates (mutual TLS): from the server, or from trusted proxies only."""

import datetime as dt
import io
import sys
from ipaddress import ip_network
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.certificates import (
    common_name,
    fingerprint,
    trusted_peer,
)
from domainkit.contrib.auth.tokens import AuthToken, TokenKind, utc_now
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.fake_smtp import certificate
from domainkit_tests.test_auth_http import create_user, stored_tokens

AUTHENTICATION = "domainkit.contrib.auth"
CLASSES = [
    f"{AUTHENTICATION}.authentication.TokenAuthentication",
    f"{AUTHENTICATION}.cookies.CookieAuthentication",
    f"{AUTHENTICATION}.certificates.ClientCertificateAuthentication",
]
PROXY = "10.0.0.5"
HEADERS = {
    "HEADER": "X-Client-Cert",
    "VERIFY_HEADER": "X-Client-Verify",
    "PROXIES": [PROXY],
}
ANONYMOUS = {"user": "", "scheme": None}


def pem_of(path: Path) -> str:
    """A certificate file's PEM text."""
    return path.read_text(encoding="ascii")


def with_tls(app: ASGIApp, chain: list[str], error: str | None = None) -> ASGIApp:
    """``app`` behind a server that terminated TLS and offers the TLS extension."""

    async def serve(scope: Scope, receive: Receive, send: Send) -> None:
        tls = {"client_cert_chain": chain, "client_cert_error": error}
        extensions = {**scope.get("extensions", {}), "tls": tls}
        await app({**scope, "extensions": extensions}, receive, send)

    return serve


def register(username: str, path: Path, **options: str) -> str:
    """Run ``addcertificate``; return what it printed."""
    out = io.StringIO()
    args = [f"--{name}={value}" for name, value in options.items()]
    call_command("addcertificate", username, str(path), *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def alice_cert(tmp_path: Path) -> Path:
    """A self-signed certificate for ``alice``."""
    cert, _ = certificate(tmp_path, "alice")
    return cert


def test_a_certificate_the_server_checked_logs_its_user_in(
    identity: Api, alice_cert: Path
) -> None:
    """``addcertificate`` registers its SHA-256; the TLS extension carries it."""
    alice = create_user("alice")
    client = identity(AUTHENTICATION_CLASSES=CLASSES)

    printed = register("alice", alice_cert, name="billing-service")

    digest = fingerprint(pem_of(alice_cert))
    assert printed == f"SHA-256 {digest} logs in as 'alice'.\n"
    (row,) = stored_tokens(alice)
    assert (row["id"], row["kind"], row["name"]) == (
        digest,
        "certificate",
        "billing-service",
    )
    tls = TestClient(with_tls(client.app, [pem_of(alice_cert), "-----CA-----"]))
    assert tls.get("/whoami").json() == {"user": "alice", "scheme": "certificate"}
    assert TestClient(with_tls(client.app, [])).get("/whoami").json() == ANONYMOUS
    (listed,) = tls.get("/auth/tokens").json()["items"]
    assert (listed["id"], listed["kind"], listed["current"]) == (
        digest,
        "certificate",
        True,
    )


def test_certificates_that_do_not_hold_are_refused(
    identity: Api, alice_cert: Path, tmp_path: Path
) -> None:
    """Unverified, unknown, malformed, or for an inactive user: 401."""
    create_user("alice")
    create_user("off", is_active=False)
    client = identity(AUTHENTICATION_CLASSES=CLASSES)
    register("alice", alice_cert)
    off_cert, _ = certificate(tmp_path, "off")
    register("off", off_cert)
    stranger, _ = certificate(tmp_path, "stranger")
    cases = {
        "The client certificate did not verify.": with_tls(
            client.app, [pem_of(alice_cert)], "expired"
        ),
        "Unknown client certificate.": with_tls(client.app, [pem_of(stranger)]),
        "Malformed client certificate.": with_tls(client.app, ["not a pem"]),
        "User inactive or deleted.": with_tls(client.app, [pem_of(off_cert)]),
    }

    for message, app in cases.items():
        response = TestClient(app).get("/whoami")
        assert response.status_code == 401, message
        assert response.json()["detail"] == message


def test_other_tokens_are_not_certificates(identity: Api, alice_cert: Path) -> None:
    """A stored token of another kind never matches a certificate."""
    alice = create_user("alice")
    client = identity(AUTHENTICATION_CLASSES=CLASSES)

    async def store() -> None:
        async with AuthUnitOfWork() as uow:
            uow.tokens.session.add(
                AuthToken(
                    id=fingerprint(pem_of(alice_cert)),
                    user_id=alice,
                    kind=TokenKind.ACCESS,
                    created_at=utc_now(),
                )
            )
            await uow.commit()

    run_async(store())

    response = TestClient(with_tls(client.app, [pem_of(alice_cert)])).get("/whoami")
    assert response.status_code == 401


def proxied(client: TestClient, address: str = PROXY) -> TestClient:
    """A client of the same application, connecting from ``address``."""
    return TestClient(client.app, client=(address, 1))


def test_certificate_proxies_may_pass_a_certificate_in_a_header(
    identity: Api, alice_cert: Path
) -> None:
    """URL-encoded PEM, with the proxy's ``SUCCESS``, from its own address."""
    create_user("alice")
    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"]):
        client = identity(AUTHENTICATION_CLASSES=CLASSES, CLIENT_CERT=HEADERS)
        register("alice", alice_cert)
        sent = {
            "X-Client-Cert": quote(pem_of(alice_cert)),
            "X-Client-Verify": "SUCCESS",
        }

        believed = proxied(client).get("/whoami", headers=sent)
        unchecked = proxied(client).get(
            "/whoami", headers={**sent, "X-Client-Verify": "FAILED:expired"}
        )
        unsaid = proxied(client).get(
            "/whoami", headers={"X-Client-Cert": sent["X-Client-Cert"]}
        )
        forged = proxied(client, "203.0.113.9").get("/whoami", headers=sent)
        empty = proxied(client).get(
            "/whoami", headers={"X-Client-Cert": "", "X-Client-Verify": "NONE"}
        )

    assert believed.json() == {"user": "alice", "scheme": "certificate"}
    assert unchecked.status_code == unsaid.status_code == 401
    for refused in (unchecked, unsaid):
        assert refused.json()["detail"] == (
            "The proxy did not verify the client certificate."
        )
    assert (forged.status_code, forged.json()["detail"]) == (
        401,
        "Client certificates in headers are taken from trusted proxies only.",
    )
    assert empty.json() == ANONYMOUS


def test_other_trusted_proxies_cannot_pass_certificates(
    identity: Api, tmp_path: Path
) -> None:
    """Hosts of ``TRUSTED_PROXIES`` are not ``CLIENT_CERT.PROXIES``.

    ``TRUSTED_PROXIES`` often holds a whole pod network: any pod there
    could otherwise send its own header and its own ``SUCCESS``, with a
    self-signed certificate naming the superuser.
    """
    create_user("root", is_superuser=True, is_staff=True)
    forged, _ = certificate(tmp_path, "root")
    sent = {"X-Client-Cert": quote(pem_of(forged)), "X-Client-Verify": "SUCCESS"}
    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"]):
        client = identity(
            AUTHENTICATION_CLASSES=CLASSES,
            CLIENT_CERT={**HEADERS, "MATCH": "subject"},
        )

        other_pod = proxied(client, "10.42.7.13").get("/scoped/root", headers=sent)
        proxy = proxied(client).get("/scoped/root", headers=sent)

    assert (other_pod.status_code, other_pod.json()["detail"]) == (
        401,
        "Client certificates in headers are taken from trusted proxies only.",
    )
    assert proxy.json() == {"ok": True}  # The proxy checked it, and says so.


def test_the_peer_is_the_proxy_even_after_forwarded_headers(
    identity: Api, alice_cert: Path
) -> None:
    """``ProxyHeadersMiddleware`` names the client, and keeps the proxy as peer."""
    from domainkit_tests.conftest import MIDDLEWARE

    create_user("alice")
    middleware = ["domainkit.http.middleware.ProxyHeadersMiddleware", *MIDDLEWARE]
    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"], MIDDLEWARE=middleware):
        client = identity(
            AUTHENTICATION_CLASSES=CLASSES,
            CLIENT_CERT={"HEADER": "X-Client-Cert", "PROXIES": [PROXY]},
        )
        register("alice", alice_cert)
        sent = {
            "X-Client-Cert": quote(pem_of(alice_cert)),
            "X-Forwarded-For": "198.51.100.4",
        }

        through_proxy = proxied(client).get("/whoami", headers=sent)
        direct = proxied(client, "198.51.100.4").get("/whoami", headers=sent)

    assert through_proxy.json() == {"user": "alice", "scheme": "certificate"}
    assert direct.status_code == 401


def test_subjects_may_name_users(identity: Api, tmp_path: Path) -> None:
    """``MATCH = "subject"``: the common name is the username."""
    create_user("alice")
    create_user("off", is_active=False)
    client = identity(AUTHENTICATION_CLASSES=CLASSES, CLIENT_CERT={"MATCH": "subject"})
    alice, _ = certificate(tmp_path, "alice")
    off, _ = certificate(tmp_path, "off")
    nobody, _ = certificate(tmp_path, "nobody")
    nameless = tmp_path / "nameless.pem"
    nameless.write_bytes(_certificate_without_common_name())
    not_der = (
        "-----BEGIN CERTIFICATE-----\n"
        "bm90IGEgY2VydGlmaWNhdGU=\n"
        "-----END CERTIFICATE-----"
    )

    def who(pem: str) -> Any:  # noqa: ANN401 - a response.
        return TestClient(with_tls(client.app, [pem])).get("/whoami")

    assert who(pem_of(alice)).json() == {"user": "alice", "scheme": "certificate"}
    for pem in (pem_of(off), pem_of(nobody), pem_of(nameless)):
        assert who(pem).json()["detail"] == "Unknown client certificate."
    assert who(not_der).json()["detail"] == "Malformed client certificate."


def _certificate_without_common_name() -> bytes:
    """A certificate whose subject names only an organization."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_subjects_need_cryptography(
    monkeypatch: pytest.MonkeyPatch, alice_cert: Path
) -> None:
    """Reading a subject needs the ``mfa`` extra's ``cryptography``."""
    from domainkit.contrib.auth.certificates import certificate_der

    der = certificate_der(pem_of(alice_cert))
    monkeypatch.setitem(sys.modules, "cryptography", None)

    with pytest.raises(
        ImproperlyConfigured,
        match=r'^MATCH = "subject" needs cryptography: '
        r'pip install "domainkit\[mfa\]"\.$',
    ):
        common_name(der)


def test_headers_need_certificate_proxies(use_auth: UseAuth) -> None:
    """A header no proxy is trusted to set could only come from clients.

    ``TRUSTED_PROXIES`` is not enough: certificates need proxies of their own.
    """
    header = {"HEADER": "X-Client-Cert"}
    with pytest.raises(ImproperlyConfigured) as refused:
        use_auth(settings={"TRUSTED_PROXIES": ["10.0.0.0/8"]}, CLIENT_CERT=header)
    assert str(refused.value) == (
        "AUTH.CLIENT_CERT.HEADER is believed only from AUTH.CLIENT_CERT.PROXIES; "
        "list the address of the proxy that sets it."
    )
    use_auth(CLIENT_CERT={**header, "PROXIES": [PROXY]})
    use_auth(CLIENT_CERT={"PROXIES": [PROXY]})  # Unused without a header.


def test_subjects_through_headers_need_the_proxys_check(use_auth: UseAuth) -> None:
    """Without it, a self-signed certificate could name any user."""
    subject = {"HEADER": "X-Client-Cert", "MATCH": "subject", "PROXIES": [PROXY]}

    with pytest.raises(ImproperlyConfigured) as refused:
        use_auth(CLIENT_CERT=subject)
    assert str(refused.value) == (
        'AUTH.CLIENT_CERT: MATCH = "subject" through HEADER needs VERIFY_HEADER, '
        "or anyone could sign a certificate naming any user."
    )
    use_auth(CLIENT_CERT={**subject, "VERIFY_HEADER": "X-Verify"})
    use_auth(CLIENT_CERT={"MATCH": "subject"})  # The server's own check suffices.


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_take_single_certificate_proxies(
    use_auth: UseAuth, environment: str
) -> None:
    """Staging and production refuse networks: one host each, the proxy's."""
    deployed = {"ENVIRONMENT": environment, "ALLOWED_HOSTS": ["api.example.com"]}
    hashers = ["domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"]
    header = {"HEADER": "X-Client-Cert", "VERIFY_HEADER": "X-Client-Verify"}

    with pytest.raises(ImproperlyConfigured) as refused:
        use_auth(
            settings=deployed,
            PASSWORD_HASHERS=hashers,
            CLIENT_CERT={
                **header,
                "PROXIES": ["10.0.0.5", "10.0.0.0/8", "fd00::/64", "10.0.1.0/31"],
            },
        )
    use_auth(
        settings=deployed,
        PASSWORD_HASHERS=hashers,
        CLIENT_CERT={**header, "PROXIES": ["10.0.0.5", "10.0.0.6/32", "fd00::1"]},
    )
    use_auth(  # Tests may.
        settings={"ENVIRONMENT": "test"},
        CLIENT_CERT={**header, "PROXIES": ["10.0.0.0/8"]},
    )

    assert str(refused.value) == (
        f"AUTH.CLIENT_CERT.PROXIES must list single addresses (/32 or /128) in "
        f"{environment}, not networks (10.0.0.0/8, fd00::/64, 10.0.1.0/31): any "
        "host there could name any user."
    )


@pytest.mark.parametrize(
    ("scope", "trusted"),
    [
        ({"client": ("10.1.2.3", 1)}, True),
        ({"client": ("::ffff:10.1.2.3", 1)}, True),
        ({"client": ("2001:db8::1", 1)}, True),
        ({"client": ("192.0.2.1", 1)}, False),
        ({"client": ("testclient", 1)}, False),
        ({"client": None}, False),
        ({}, False),
        ({"client": ("192.0.2.1", 1), "domainkit.peer": ("10.0.0.1", 1)}, True),
    ],
)
def test_trusted_peers(scope: dict[str, Any], trusted: bool) -> None:
    """The socket peer must be one of the proxies."""
    proxies = [ip_network("10.0.0.0/8"), ip_network("2001:db8::/32")]
    connection = HTTPConnection({"type": "http", "headers": [], **scope})

    assert trusted_peer(connection, proxies) is trusted


def test_addcertificate_checks_what_it_is_given(
    identity: Api, alice_cert: Path, tmp_path: Path
) -> None:
    """A user, a PEM file, a short name, and a certificate not yet registered."""
    alice = create_user("alice")
    identity()
    register("alice", alice_cert)
    text = tmp_path / "text.pem"
    text.write_text("hello", encoding="ascii")
    binary = tmp_path / "binary.pem"
    binary.write_bytes(b"\xff\xfe")

    for args, message in (
        (["alice", str(alice_cert)], "registered already"),
        (["nobody", str(alice_cert)], "does not exist"),
        (["alice", str(tmp_path / "missing.pem")], "not a PEM certificate"),
        (["alice", str(text)], "not a PEM certificate"),
        (["alice", str(binary)], "not a PEM certificate"),
        (["alice", str(alice_cert), "--name", "n" * 101], "at most 100"),
    ):
        with pytest.raises(CommandError, match=message):
            call_command("addcertificate", *args, stdout=io.StringIO())
    call_command("cleartokens", "--user", "alice", stdout=io.StringIO())
    assert stored_tokens(alice) == []

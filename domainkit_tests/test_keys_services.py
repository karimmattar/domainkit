"""Key services as providers: Cloud KMS, Key Vault, Huawei KMS, PKCS#11, a project's."""

import base64
import hashlib
import json
import secrets
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Self

import httpx2
import pytest

from domainkit.cloud import TokenSource
from domainkit.contrib.keys.operations import provider_for
from domainkit.contrib.keys.settings import KeysSettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.client import stub
from domainkit.keys import DataKey, KeyProvider, KeyProviderError, UnknownKey
from domainkit.keys.azure import AzureKeyVaultKeyProvider
from domainkit.keys.gcp import AAD, GcpKmsKeyProvider
from domainkit.keys.huawei import HuaweiKmsKeyProvider, sign
from domainkit.keys.pkcs11 import Pkcs11KeyProvider

pytestmark = pytest.mark.anyio

KEY_NAME = "projects/acme/locations/eu/keyRings/app/cryptoKeys/fields"


class Fixed(TokenSource):
    """A token source that always answers ``token``, or refuses."""

    def __init__(self, token: str = "tok", *, refuse: bool = False) -> None:  # noqa: S107
        """Answer ``token``; with ``refuse``, raise as a refused login would."""
        super().__init__()
        self.answer = token
        self.refuse = refuse

    async def _fetch(self, scope: str) -> tuple[str, float]:
        """The token, or a refusal."""
        if self.refuse:
            from domainkit.cloud import CloudAuthError

            raise CloudAuthError("no")
        return self.answer, 3600.0


class FakeGcp:
    """Cloud KMS's encrypt and decrypt for one key, version by version."""

    def __init__(self) -> None:
        """Start at version 1."""
        self.version = 1
        self.sealed: dict[str, tuple[int, bytes]] = {}
        self.status: int | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one call."""
        if self.status is not None:
            return httpx2.Response(self.status, json={})
        assert request.headers["authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["additionalAuthenticatedData"] == AAD
        path = request.url.path
        if path == f"/v1/{KEY_NAME}:encrypt":
            ciphertext = base64.b64encode(secrets.token_bytes(24)).decode()
            self.sealed[ciphertext] = (
                self.version,
                base64.b64decode(body["plaintext"]),
            )
            return httpx2.Response(200, json={"ciphertext": ciphertext, "name": "v"})
        assert path == f"/v1/{KEY_NAME}:decrypt"
        held = self.sealed.get(body["ciphertext"])
        if held is None:
            return httpx2.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}})
        return httpx2.Response(
            200, json={"plaintext": base64.b64encode(held[1]).decode()}
        )


@pytest.fixture
def gcp() -> Iterator[FakeGcp]:
    """Cloud KMS, answering every call of the test."""
    fake = FakeGcp()
    with stub(fake):
        yield fake


async def roundtrip(provider: KeyProvider) -> DataKey:
    """Make a data key; it unwraps to itself, and rewraps to a key that does too."""
    made = await provider.generate_data_key()
    assert await provider.unwrap(made.wrapped) == made.plaintext
    moved = await provider.rewrap(made.wrapped)
    assert moved != made.wrapped
    assert await provider.unwrap(moved) == made.plaintext
    assert provider.owns(made.wrapped)
    shown = repr(provider)
    assert shown.startswith(type(provider).__name__)
    assert "SK" not in shown  # No secret.
    assert "1234" not in shown  # No PIN.
    return made


async def test_cloud_kms_wraps_here_made_keys(gcp: FakeGcp) -> None:
    """Encrypt and decrypt with the additional data; others' keys refused."""
    provider = GcpKmsKeyProvider(KEY_NAME, account=Fixed())

    made = await roundtrip(provider)

    assert made.wrapped.startswith("gcp-kms:")
    with pytest.raises(UnknownKey):
        await provider.unwrap("gcp-kms:" + base64.b64encode(b"other").decode())
    with pytest.raises(UnknownKey):
        await provider.unwrap("vault:v1:x")


async def test_cloud_kms_failures_are_named(gcp: FakeGcp) -> None:
    """A refusal, a refused token, a missing field, a bad name."""
    provider = GcpKmsKeyProvider(KEY_NAME, account=Fixed())
    gcp.status = 403
    with pytest.raises(KeyProviderError, match="refused encrypt"):
        await provider.generate_data_key()
    with pytest.raises(KeyProviderError, match="token was refused"):
        await GcpKmsKeyProvider(
            KEY_NAME, account=Fixed(refuse=True)
        ).generate_data_key()
    with pytest.raises(ValueError, match="cryptoKeys"):
        GcpKmsKeyProvider("projects/acme/keys/x")
    assert type(GcpKmsKeyProvider(KEY_NAME).account).__name__ == "MetadataAccount"


async def test_cloud_kms_answers_that_do_not_hold() -> None:
    """No plaintext, plaintext not base64, no JSON, no service."""
    provider = GcpKmsKeyProvider(KEY_NAME, account=Fixed())
    for answer in (
        httpx2.Response(200, json={}),
        httpx2.Response(200, json={"plaintext": "!!"}),
        httpx2.Response(200, text="<html>"),
    ):
        with (
            stub(lambda request, answer=answer: answer),
            pytest.raises(KeyProviderError),
        ):
            await provider.unwrap("gcp-kms:abc")

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no")

    with stub(down), pytest.raises(KeyProviderError, match="could not be reached"):
        await provider.unwrap("gcp-kms:abc")


class FakeVault:
    """Key Vault: one key, its versions, wrapkey and unwrapkey."""

    def __init__(self) -> None:
        """Version ``v1`` is current."""
        self.current = "v1"
        self.reads = 0
        self.wrapped: dict[tuple[str, str], bytes] = {}
        self.kid: str | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one call."""
        assert request.url.params["api-version"] == "7.4"
        parts = request.url.path.strip("/").split("/")
        if request.method == "GET":
            self.reads += 1
            kid = self.kid or f"https://acme.vault.azure.net/keys/fields/{self.current}"
            return httpx2.Response(200, json={"key": {"kid": kid}})
        _, _, version, action = parts
        body = json.loads(request.content)
        assert body["alg"] == "RSA-OAEP-256"
        if action == "wrapkey":
            value = (
                base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
            )
            raw = body["value"]
            self.wrapped[(version, value)] = base64.urlsafe_b64decode(
                raw + "=" * (-len(raw) % 4)
            )
            return httpx2.Response(200, json={"kid": version, "value": value})
        held = self.wrapped.get((version, body["value"]))
        if held is None:
            return httpx2.Response(400, json={"error": {"code": "BadParameter"}})
        return httpx2.Response(
            200, json={"value": base64.urlsafe_b64encode(held).decode().rstrip("=")}
        )


async def test_key_vault_wraps_under_the_current_version() -> None:
    """The version is read once and kept; a rotation reaches new keys later."""
    fake = FakeVault()
    provider = AzureKeyVaultKeyProvider(
        "https://acme.vault.azure.net/", "fields", credential=Fixed()
    )
    with stub(fake):
        made = await roundtrip(provider)
        fake.current = "v2"
        provider._current = None  # Five minutes pass.
        moved = await provider.rewrap(made.wrapped)

        assert made.wrapped.startswith("azure-kv:v1:")
        assert moved.startswith("azure-kv:v2:")
        assert await provider.unwrap(moved) == made.plaintext
        assert fake.reads == 2
        for refused in ("azure-kv:v1:nothing", "azure-kv:../x:y", "azure-kv:v1:", "x"):
            with pytest.raises(UnknownKey):
                await provider.unwrap(refused)


async def test_key_vault_failures_are_named() -> None:
    """A kid without a version, a refused read, no JSON, a refused token."""
    fake = FakeVault()
    fake.kid = "https://acme.vault.azure.net/keys/fields/../"
    provider = AzureKeyVaultKeyProvider(
        "https://kv.example", "fields", credential=Fixed()
    )
    with stub(fake), pytest.raises(KeyProviderError, match="without a version"):
        await provider.generate_data_key()
    for answer in (httpx2.Response(403), httpx2.Response(200, text="x")):
        with (
            stub(lambda request, answer=answer: answer),
            pytest.raises(KeyProviderError),
        ):
            await provider.generate_data_key()
    with (
        stub(lambda request: httpx2.Response(200, json={"value": "!!"})),
        pytest.raises(KeyProviderError),
    ):
        await provider.unwrap("azure-kv:v1:abc")

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no")

    with stub(down), pytest.raises(KeyProviderError, match="could not be reached"):
        await provider.generate_data_key()
    refusing = AzureKeyVaultKeyProvider(
        "https://kv.example", "fields", credential=Fixed(refuse=True)
    )
    with pytest.raises(KeyProviderError, match="token was refused"):
        await refusing.generate_data_key()
    with pytest.raises(ValueError, match="A256KW"):
        AzureKeyVaultKeyProvider("https://kv.example", "fields", algorithm="RSA1_5")


class FakeHuawei:
    """Huawei KMS: create, decrypt, and encrypt data keys of one master key."""

    def __init__(self) -> None:
        """Nothing wrapped yet."""
        self.sealed: dict[str, bytes] = {}
        self.lie = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one call, its signature present."""
        assert request.headers["authorization"].startswith(
            "SDK-HMAC-SHA256 Access=AK, SignedHeaders=content-type;host;x-project-id;"
        )
        body = json.loads(request.content)
        action = request.url.path.rpartition("/")[2]
        assert body["key_id"] == "mk-1"
        if action == "create-datakey":
            key = secrets.token_bytes(32)
            cipher = self._seal(key)
            return httpx2.Response(
                200,
                json={"key_id": "mk-1", "plain_text": key.hex(), "cipher_text": cipher},
            )
        if action == "encrypt-datakey":
            plain = bytes.fromhex(body["plain_text"])
            key, digest = plain[:32], plain[32:]
            assert digest == hashlib.sha256(key).digest()
            return httpx2.Response(200, json={"cipher_text": self._seal(key)})
        assert body["datakey_cipher_length"] == "32"
        held = self.sealed.get(body["cipher_text"])
        if held is None:
            return httpx2.Response(400, json={"error": {"error_code": "KMS.0205"}})
        dgst = hashlib.sha256(b"lie" if self.lie else held).hexdigest()
        return httpx2.Response(
            200,
            json={
                "data_key": held.hex(),
                "datakey_length": "32",
                "datakey_dgst": dgst,
            },
        )

    def _seal(self, key: bytes) -> str:
        """A new ciphertext of ``key``."""
        cipher = secrets.token_hex(48)
        self.sealed[cipher] = key
        return cipher


def huawei() -> HuaweiKmsKeyProvider:
    """The provider, for the fake."""
    return HuaweiKmsKeyProvider(
        "mk-1",
        region="ap-southeast-3",
        project_id="0abc",
        access_key="AK",
        secret_key="SK",
    )


async def test_huawei_kms_makes_unwraps_and_rewraps() -> None:
    """create, decrypt with its digest checked, encrypt with the key's digest."""
    fake = FakeHuawei()
    provider = huawei()
    with stub(fake):
        made = await roundtrip(provider)
        assert made.wrapped.startswith("huawei-kms:")
        for refused in ("huawei-kms:zz", "huawei-kms:" + "ab" * 48, "aws-kms:x"):
            with pytest.raises(UnknownKey):
                await provider.unwrap(refused)
        fake.lie = True
        with pytest.raises(KeyProviderError, match="digest"):
            await provider.unwrap(made.wrapped)


async def test_huawei_kms_answers_that_do_not_hold() -> None:
    """Keys not hex or of another size; a ciphertext not hex."""
    provider = huawei()
    for answer in (
        {"plain_text": "zz", "cipher_text": "ab"},
        {"plain_text": "ab", "cipher_text": "ab"},
        {"plain_text": "ab" * 32, "cipher_text": "not hex"},
    ):
        with (
            stub(lambda request, answer=answer: httpx2.Response(200, json=answer)),
            pytest.raises(KeyProviderError),
        ):
            await provider.generate_data_key()
    with pytest.raises(ValueError, match="project"):
        HuaweiKmsKeyProvider(
            "k", region="r", project_id="", access_key="a", secret_key="s"
        )


def test_huawei_signs_as_its_gateway_does() -> None:
    """The canonical request ends its path with /; the query sorted; host signed."""
    from domainkit.clock import freeze_time

    with freeze_time("2027-01-04T10:00:00Z"):
        headers = sign(
            "GET",
            "https://kms.example.com/v1.0/p/kms/keys?b=2&a=1",
            {"Content-Type": "application/json"},
            b"",
            access_key="AK",
            secret_key="SK",
        )
    canonical = "\n".join(
        [
            "GET",
            "/v1.0/p/kms/keys/",
            "a=1&b=2",
            (
                "content-type:application/json\nhost:kms.example.com\n"
                "x-sdk-date:20270104T100000Z\n"
            ),
            "content-type;host;x-sdk-date",
            hashlib.sha256(b"").hexdigest(),
        ]
    )
    import hmac

    to_sign = (
        "SDK-HMAC-SHA256\n20270104T100000Z\n"
        + hashlib.sha256(canonical.encode()).hexdigest()
    )
    expected = hmac.new(b"SK", to_sign.encode(), hashlib.sha256).hexdigest()
    assert headers["Authorization"].endswith(f"Signature={expected}")
    assert headers["X-Sdk-Date"] == "20270104T100000Z"


SOFTHSM = Path("/usr/lib/softhsm/libsofthsm2.so")
UTIL = shutil.which("softhsm2-util")


@pytest.fixture
def hsm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A SoftHSM token ``dk`` (PIN 1234) with AES keys ``kek-1`` and ``kek-2``."""
    if not SOFTHSM.exists() or UTIL is None:
        pytest.skip("SoftHSM is not installed")
    import pkcs11
    from pkcs11 import KeyType

    (tmp_path / "tokens").mkdir()
    config = tmp_path / "softhsm2.conf"
    config.write_text(f"directories.tokendir = {tmp_path / 'tokens'}\n")
    monkeypatch.setenv("SOFTHSM2_CONF", str(config))
    subprocess.run(  # noqa: S603 - our own arguments.
        [
            UTIL,
            "--init-token",
            "--free",
            "--label",
            "dk",
            "--pin",
            "1234",
            "--so-pin",
            "5678",
        ],
        check=True,
        capture_output=True,
    )
    token = pkcs11.lib(str(SOFTHSM)).get_token(token_label="dk")
    with token.open(user_pin="1234", rw=True) as session:
        for label in ("kek-1", "kek-2"):
            session.generate_key(KeyType.AES, 256, label=label, store=True)
    return str(SOFTHSM)


async def test_an_hsm_wraps_and_rotates_to_a_new_key(hsm: str) -> None:
    """kek-1, then kek-2 with kek-1 retired; a changed key or label refused."""
    old = Pkcs11KeyProvider(hsm, token_label="dk", pin="1234", key_label="kek-1")
    made = await roundtrip(old)
    new = Pkcs11KeyProvider(
        hsm, token_label="dk", pin="1234", key_label="kek-2", retired_labels=("kek-1",)
    )

    moved = await new.rewrap(made.wrapped)

    assert made.wrapped.startswith("pkcs11:kek-1:")
    assert moved.startswith("pkcs11:kek-2:")
    assert await new.unwrap(moved) == made.plaintext
    changed = (
        made.wrapped[:-2] + ("A" if made.wrapped[-2] != "A" else "B") + made.wrapped[-1]
    )
    for refused in (changed, "pkcs11:kek-9:abc", "pkcs11:kek-1:!!", "local:x"):
        with pytest.raises(UnknownKey):
            await old.unwrap(refused)
    missing = Pkcs11KeyProvider(hsm, token_label="dk", pin="1234", key_label="kek-9")
    with pytest.raises(KeyProviderError, match="could not be found"):
        await missing.generate_data_key()
    assert "1234" not in repr(old)


async def test_an_hsm_that_refuses_to_encrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A library error while encrypting is the provider's, not the key's."""
    import pkcs11

    class Key:
        def encrypt(self, *args: Any, **kwargs: Any) -> bytes:  # noqa: ANN401
            raise pkcs11.exceptions.DeviceError

    class Session:
        def get_key(self, **kwargs: Any) -> Key:  # noqa: ANN401
            return Key()

    provider = Pkcs11KeyProvider("lib.so", token_label="t", pin="p", key_label="k")
    monkeypatch.setattr(provider, "_open", lambda: Session())

    with pytest.raises(KeyProviderError, match="failed to encrypt"):
        await provider.generate_data_key()
    with pytest.raises(ValueError, match="distinct"):
        Pkcs11KeyProvider(
            "l", token_label="t", pin="p", key_label="k", retired_labels=("k",)
        )


class Rot13Provider(KeyProvider):
    """A project's provider: not a real one, but its own prefix."""

    name = "rot"

    def __init__(self, shift: int = 1) -> None:
        """Remember ``shift``."""
        self.shift = shift

    @classmethod
    def from_options(cls, options: Mapping[str, Any], *, timeout: float) -> Self:
        """``SHIFT``, read."""
        return cls(int(options.get("SHIFT", 1)))

    async def generate_data_key(self) -> DataKey:
        """Nothing secret."""
        return DataKey(bytes(32), "rot:x")

    async def unwrap(self, wrapped: str) -> bytes:
        """Nothing secret."""
        return bytes(32)

    async def rewrap(self, wrapped: str) -> str:
        """The same."""
        return wrapped


class NotAProvider:
    """Wraps nothing."""


HERE = "domainkit_tests.test_keys_services"


@pytest.mark.parametrize(
    ("settings", "kind"),
    [
        ({"PROVIDER": "gcp-kms", "GCP_KMS": {"KEY_NAME": KEY_NAME}}, GcpKmsKeyProvider),
        (
            {
                "PROVIDER": "azure-key-vault",
                "AZURE_KEY_VAULT": {
                    "VAULT_URL": "https://acme.vault.azure.net",
                    "KEY_NAME": "fields",
                    "TENANT_ID": "t",
                    "CLIENT_ID": "c",
                    "CLIENT_SECRET": "s",
                },
            },
            AzureKeyVaultKeyProvider,
        ),
        (
            {
                "PROVIDER": "azure-key-vault",
                "AZURE_KEY_VAULT": {
                    "VAULT_URL": "https://acme.vault.azure.net",
                    "KEY_NAME": "fields",
                },
            },
            AzureKeyVaultKeyProvider,
        ),
        (
            {
                "PROVIDER": "huawei-kms",
                "HUAWEI_KMS": {
                    "KEY_ID": "mk-1",
                    "REGION": "ap-southeast-3",
                    "PROJECT_ID": "0abc",
                    "ACCESS_KEY": "AK",
                    "SECRET_KEY": "SK",
                },
            },
            HuaweiKmsKeyProvider,
        ),
        (
            {
                "PROVIDER": "pkcs11",
                "PKCS11": {
                    "LIBRARY": "/x.so",
                    "TOKEN_LABEL": "t",
                    "PIN": "p",
                    "KEY_LABEL": "k",
                },
            },
            Pkcs11KeyProvider,
        ),
        (
            {"PROVIDER": "custom", "CUSTOM": {"CLASS": f"{HERE}.Rot13Provider"}},
            Rot13Provider,
        ),
    ],
)
def test_keys_settings_name_each_provider(settings: dict[str, Any], kind: type) -> None:
    """Each PROVIDER, from its settings; data keys of the service needed."""
    wrapped = {
        "gcp-kms": "gcp-kms:x",
        "azure-key-vault": "azure-kv:v1:x",
        "huawei-kms": "huawei-kms:ab",
        "pkcs11": "pkcs11:k:x",
        "custom": "rot:x",
    }[settings["PROVIDER"]]

    provider = provider_for(KeysSettings(**settings, DATA_KEYS={"k1": wrapped}))

    assert isinstance(provider, kind)
    with pytest.raises(ValueError, match="needs"):
        KeysSettings(PROVIDER=settings["PROVIDER"], DATA_KEYS={"k1": wrapped})


def test_a_custom_provider_must_be_one_and_own_its_keys() -> None:
    """Not importable, not a KeyProvider, keys of another: refused."""
    for path, keys, message in (
        (f"{HERE}.Nope", {"k": "rot:x"}, "CUSTOM.CLASS"),
        (f"{HERE}.NotAProvider", {"k": "rot:x"}, "not a KeyProvider"),
        (
            f"{HERE}.Rot13Provider",
            {"k": "aws-kms:x"},
            "were not wrapped by Rot13Provider",
        ),
    ):
        config = KeysSettings(PROVIDER="custom", CUSTOM={"CLASS": path}, DATA_KEYS=keys)
        with pytest.raises(ImproperlyConfigured, match=message):
            provider_for(config)
    from domainkit.keys.vault import VaultTransitKeyProvider

    vault = VaultTransitKeyProvider.from_options(
        {"url": "https://vault", "key": "k", "token": "t"}, timeout=5.0
    )
    assert vault.key == "k"  # The default: the class, called with the options.


def test_key_vault_applications_are_whole() -> None:
    """A tenant without a secret, or without a client, is refused."""
    base = {"VAULT_URL": "https://acme.vault.azure.net", "KEY_NAME": "k"}
    for extra in ({"TENANT_ID": "t"}, {"TENANT_ID": "t", "CLIENT_SECRET": "s"}):
        with pytest.raises(ValueError, match="CLIENT"):
            KeysSettings(
                PROVIDER="azure-key-vault",
                AZURE_KEY_VAULT={**base, **extra},
                DATA_KEYS={"k": "azure-kv:v:x"},
            )

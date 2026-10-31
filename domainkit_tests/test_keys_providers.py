"""Key providers: a local KEK, AWS KMS and Vault transit, against protocol fakes."""

import base64
import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.client import stub
from domainkit.keys import (
    DataKey,
    KeyProvider,
    KeyProviderError,
    LocalKeyProvider,
    UnknownKey,
)
from domainkit.keys.aws import (
    AwsCredentials,
    AwsKmsKeyProvider,
    container_credentials,
    default_credentials,
    environment_credentials,
    sign,
    signing_key,
    web_identity_credentials,
)
from domainkit.keys.vault import VaultTransitKeyProvider
from domainkit_tests.fake_kms import SECRETS, FakeKms, FakeVault

ACCESS_KEY: Final = "AKIDEXAMPLE"
CREDENTIALS: Final = AwsCredentials(ACCESS_KEY, SECRETS[ACCESS_KEY])
KEY_ARN: Final = "arn:aws:kms:eu-west-1:111122223333:key/k-1"
NOW: Final = datetime(2026, 10, 31, 12, tzinfo=UTC)
AWS_VARIABLES: Final = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
)


@pytest.fixture
def no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """No AWS credentials in the environment."""
    for name in AWS_VARIABLES:
        monkeypatch.delenv(name, raising=False)


async def static() -> AwsCredentials:
    """The fake KMS's access key."""
    return CREDENTIALS


@pytest.fixture
def kms() -> Iterator[FakeKms]:
    """A fake KMS with one key, ``k-1``, aliased ``alias/payments``, answering calls."""
    fake = FakeKms()
    fake.create("k-1", alias="alias/payments")
    fake.create("k-2")
    with stub(fake):
        yield fake


def aws(key_id: str = KEY_ARN) -> AwsKmsKeyProvider:
    """A provider of the fake KMS's key."""
    return AwsKmsKeyProvider(key_id, region="eu-west-1", credentials=static)


@pytest.fixture
def vault() -> Iterator[FakeVault]:
    """A fake Vault with the transit key ``payments``, answering calls."""
    fake = FakeVault()
    fake.create("payments")
    with stub(fake):
        yield fake


def transit(**options: Any) -> VaultTransitKeyProvider:  # noqa: ANN401
    """A provider of the fake Vault's ``payments`` key."""
    options.setdefault("token", "s.test-token")
    return VaultTransitKeyProvider("https://vault.test:8200", "payments", **options)


# The local provider -----------------------------------------------------------------


@pytest.mark.anyio
async def test_the_local_provider_wraps_under_secret_key_and_unwraps() -> None:
    """32 random bytes, wrapped as ``local:<kek id>:...``, unwrapped back."""
    provider = LocalKeyProvider.from_secret_key()
    made = await provider.generate_data_key()
    assert len(made.plaintext) == 32
    assert made.wrapped.startswith("local:")
    assert provider.owns(made.wrapped)
    assert await provider.unwrap(made.wrapped) == made.plaintext
    again = await provider.rewrap(made.wrapped)
    assert again != made.wrapped
    assert provider.unwrap_now(again) == made.plaintext
    assert repr(provider) == "LocalKeyProvider()"


@pytest.mark.anyio
async def test_rotating_secret_key_keeps_old_data_keys_unwrapping() -> None:
    """A KEK in ``SECRET_KEY_FALLBACKS`` unwraps; rewrap moves to the new one."""
    old = LocalKeyProvider.from_secret_key()
    made = await old.generate_data_key()
    new_secret = "rotated-secret-" + "x8Kq2Lm9Pz4Rt7Vw1Yb5Nc3Hd6Jf0Gs"
    with override_settings(
        SECRET_KEY=new_secret,
        SECRET_KEY_FALLBACKS=["tests-only-6Zq3b1Jw8RkV0yXc5MnHf2LpT9sDg4AeU7oKiWx3N"],
    ):
        rotated = LocalKeyProvider.from_secret_key()
        assert await rotated.unwrap(made.wrapped) == made.plaintext
        moved = await rotated.rewrap(made.wrapped)
    assert moved.split(":")[1] != made.wrapped.split(":")[1]
    with override_settings(SECRET_KEY=new_secret, SECRET_KEY_FALLBACKS=[]):
        assert await LocalKeyProvider.from_secret_key().unwrap(moved) == made.plaintext
        with pytest.raises(UnknownKey, match="SECRET_KEY_FALLBACKS"):
            await LocalKeyProvider.from_secret_key().unwrap(made.wrapped)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        lambda wrapped: wrapped[:-4] + ("AAAA" if wrapped[-4:] != "AAAA" else "BBBB"),
        lambda wrapped: wrapped[:-30],
        lambda wrapped: wrapped + "!",
    ],
    ids=["changed", "cut", "not-base64"],
)
async def test_a_changed_local_key_does_not_unwrap(change: Any) -> None:  # noqa: ANN401
    """Tampering, truncation, and garbage are refused alike."""
    provider = LocalKeyProvider.from_secret_key()
    made = await provider.generate_data_key()
    with pytest.raises(UnknownKey, match="changed, or cut short"):
        provider.unwrap_now(change(made.wrapped))


@pytest.mark.anyio
async def test_the_local_provider_refuses_others_keys() -> None:
    """A key of another provider, or of no KEK it has, is unknown."""
    provider = LocalKeyProvider.from_secret_key()
    for wrapped in ("vault:v1:abc", "local:00000000:abc", "local"):
        with pytest.raises(UnknownKey, match="did not wrap that key"):
            await provider.unwrap(wrapped)


def test_the_local_provider_needs_keks_of_32_bytes() -> None:
    """No KEK, or a short one, is refused."""
    for keks in ([], [b"short"]):
        with pytest.raises(ValueError, match="32 bytes"):
            LocalKeyProvider(keks)


def test_a_data_key_never_shows_itself() -> None:
    """``repr`` hides both forms of the key."""
    shown = repr(DataKey(b"\x01" * 32, "local:abc:def"))
    assert shown == "DataKey(plaintext=<hidden>, wrapped=<hidden>)"


def test_a_provider_owns_the_keys_with_its_prefix() -> None:
    """``owns`` reads the prefix and the colon."""

    class Named(KeyProvider):
        name = "x"

        async def generate_data_key(self) -> DataKey:
            raise NotImplementedError

        async def unwrap(self, wrapped: str) -> bytes:
            raise NotImplementedError

        async def rewrap(self, wrapped: str) -> str:
            raise NotImplementedError

    assert Named().owns("x:1")
    assert not Named().owns("xy:1")
    assert repr(Named()) == "Named()"


def test_missing_cryptography_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``cryptography``, the local provider says which extra brings it."""
    monkeypatch.setitem(
        sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None
    )
    with pytest.raises(ImproperlyConfigured, match=r"domainkit\[mfa\]"):
        LocalKeyProvider([b"k" * 32])


# SigV4 ------------------------------------------------------------------------------


def test_sigv4_signs_as_aws_documents() -> None:
    """AWS's own example request (IAM ListUsers) gets AWS's own signature."""
    signed = sign(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        b"",
        CREDENTIALS,
        region="us-east-1",
        service="iam",
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
    )
    assert signed["X-Amz-Date"] == "20150830T123600Z"
    assert signed["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )
    key = signing_key(CREDENTIALS.secret_access_key, "20150830", "us-east-1", "iam")
    assert key.hex() == (
        "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"
    )


def test_sigv4_signs_the_session_token_and_a_port() -> None:
    """Temporary credentials sign their token; a port not the scheme's is kept."""
    signed = sign(
        "POST",
        "https://kms.test:8443/",
        {"X-Amz-Target": "TrentService.Decrypt"},
        b"{}",
        AwsCredentials(ACCESS_KEY, "secret", "token"),
        region="eu-west-1",
        service="kms",
        now=NOW,
    )
    assert signed["X-Amz-Security-Token"] == "token"
    assert (
        "SignedHeaders=host;x-amz-date;x-amz-security-token;x-amz-target,"
        in (signed["Authorization"])
    )
    plain = sign(
        "GET",
        "http://a.test:80",
        {},
        b"",
        CREDENTIALS,
        region="r",
        service="s",
        now=NOW,
    )
    assert "SignedHeaders=host;x-amz-date," in plain["Authorization"]


def test_credentials_name_their_key_only() -> None:
    """``repr`` shows the access key id; long-lived ones are always fresh."""
    assert repr(CREDENTIALS) == "AwsCredentials(access_key_id='AKIDEXAMPLE')"
    assert CREDENTIALS.fresh()
    with freeze_time(NOW):
        assert not AwsCredentials("a", "b", "c", NOW + timedelta(minutes=5)).fresh()
        assert AwsCredentials("a", "b", "c", NOW + timedelta(minutes=6)).fresh()


# AWS KMS ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_kms_makes_unwraps_and_rewraps_data_keys(kms: FakeKms) -> None:
    """Each call is signed; the wrapped key names its provider; rewrap moves on."""
    provider = aws("alias/payments")
    made = await provider.generate_data_key()
    assert made.wrapped.startswith("aws-kms:")
    assert await provider.unwrap(made.wrapped) == made.plaintext
    kms.rotate("k-1")
    moved = await provider.rewrap(made.wrapped)
    assert kms.version_of(moved.removeprefix("aws-kms:")) == ("k-1", 1)
    assert await provider.unwrap(moved) == made.plaintext
    assert kms.calls == ["GenerateDataKey", "Decrypt", "ReEncrypt", "Decrypt"]
    sent = json.loads(kms.bodies[0])
    assert sent == {
        "KeyId": "alias/payments",
        "KeySpec": "AES_256",
        "EncryptionContext": {"domainkit": "data-key"},
    }
    assert repr(provider) == (
        "AwsKmsKeyProvider(key_id='alias/payments', region='eu-west-1')"
    )


@pytest.mark.anyio
async def test_kms_refuses_a_key_of_another_kms_key(kms: FakeKms) -> None:
    """A data key under ``k-2`` does not unwrap through ``k-1``'s provider."""
    made = await aws("k-2").generate_data_key()
    with pytest.raises(UnknownKey, match="IncorrectKeyException"):
        await aws().unwrap(made.wrapped)
    with pytest.raises(UnknownKey, match="InvalidCiphertextException"):
        await aws().rewrap(made.wrapped)
    damaged = made.wrapped[:-8] + "AAAAAAA="
    with pytest.raises(UnknownKey, match="InvalidCiphertextException"):
        await aws("k-2").unwrap(damaged)


@pytest.mark.anyio
async def test_kms_refuses_what_it_did_not_wrap_before_calling(kms: FakeKms) -> None:
    """Another provider's key, or one that is not base64, never reaches KMS."""
    for wrapped in ("vault:v1:abc", "aws-kms:not base64!"):
        with pytest.raises(UnknownKey):
            await aws().unwrap(wrapped)
    assert kms.calls == []


@pytest.mark.anyio
async def test_kms_errors_name_the_exception_only(kms: FakeKms) -> None:
    """A refusal names KMS's exception and the status, never its message."""
    with pytest.raises(KeyProviderError, match=r"NotFoundException \(400\)"):
        await aws("alias/none").generate_data_key()
    wrong = AwsKmsKeyProvider(
        KEY_ARN,
        region="eu-west-1",
        credentials=lambda: _credentials(AwsCredentials(ACCESS_KEY, "wrong")),
    )
    with pytest.raises(KeyProviderError, match="InvalidSignatureException"):
        await wrong.generate_data_key()


async def _credentials(found: AwsCredentials) -> AwsCredentials:
    """``found``, awaited."""
    return found


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx2.Response(500, text="<html>"), "500 and no JSON"),
        (httpx2.Response(200, json=[1]), "200 and no JSON"),
        (httpx2.Response(200, json={"CiphertextBlob": "x"}), "without Plaintext"),
        (httpx2.Response(200, json={"Plaintext": 3}), "without Plaintext"),
        (
            httpx2.Response(400, json={"__type": "Weird Type<script>"}),
            r"refused GenerateDataKey: an error \(400\)",
        ),
    ],
)
async def test_kms_answers_that_make_no_sense_are_refused(
    answer: httpx2.Response, message: str
) -> None:
    """A broken answer is a provider error, never a key."""
    with stub(lambda request: answer), pytest.raises(KeyProviderError, match=message):
        await aws().generate_data_key()


@pytest.mark.anyio
async def test_kms_that_cannot_be_reached_is_a_provider_error() -> None:
    """A network failure names the operation, chained to the cause."""

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    provider = AwsKmsKeyProvider(
        KEY_ARN, region="eu-west-1", credentials=static, endpoint="https://kms.test"
    )
    with (
        override_settings(HTTP_CLIENT={"RETRIES": 0}),
        stub(down),
        pytest.raises(KeyProviderError, match=r"could not be reached \(Decrypt\)"),
    ):
        await provider.unwrap("aws-kms:AAAA")


@pytest.mark.anyio
async def test_kms_calls_log_no_key(
    kms: FakeKms, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing logged during the calls holds a key, wrapped or not."""
    caplog.set_level(logging.DEBUG)
    provider = aws()
    made = await provider.generate_data_key()
    await provider.unwrap(made.wrapped)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert base64.b64encode(made.plaintext).decode() not in logged
    assert made.wrapped.removeprefix("aws-kms:") not in logged


# AWS credentials --------------------------------------------------------------------


@pytest.mark.anyio
async def test_environment_credentials_are_read_at_each_call(
    no_aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both parts are needed; the token is optional; a new secret is picked up."""
    source = environment_credentials()
    with pytest.raises(KeyProviderError, match="AWS_ACCESS_KEY_ID"):
        await source()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKID")
    with pytest.raises(KeyProviderError, match="AWS_SECRET_ACCESS_KEY"):
        await source()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "one")
    first = await source()
    assert (first.access_key_id, first.secret_access_key, first.session_token) == (
        "AKID",
        "one",
        None,
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "two")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "t")
    second = await source()
    assert (second.secret_access_key, second.session_token) == ("two", "t")


STS_ANSWER: Final = """<AssumeRoleWithWebIdentityResponse>
<AssumeRoleWithWebIdentityResult><Credentials>
<AccessKeyId>ASIA1</AccessKeyId><SecretAccessKey>s1</SecretAccessKey>
<SessionToken>t1</SessionToken><Expiration>2026-10-31T13:00:00Z</Expiration>
</Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>"""


@pytest.mark.anyio
async def test_web_identity_credentials_assume_the_role_and_are_kept(
    tmp_path: Path,
) -> None:
    """STS is asked once with the token file's token, again near the expiry."""
    token = tmp_path / "token"
    token.write_text("jwt-1\n")
    asked: list[httpx2.Request] = []

    def sts(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(200, text=STS_ANSWER)

    source = web_identity_credentials(
        "arn:aws:iam::1:role/app", token, region="eu-west-1"
    )
    with stub(sts), freeze_time(NOW) as clock:
        first = await source()
        assert await source() is first
        clock.move_to(NOW + timedelta(minutes=56))
        await source()
    assert (first.access_key_id, first.secret_access_key, first.session_token) == (
        "ASIA1",
        "s1",
        "t1",
    )
    assert first.expires_at == NOW + timedelta(hours=1)
    assert len(asked) == 2
    assert asked[0].url == "https://sts.eu-west-1.amazonaws.com/"
    form = dict(item.split("=", 1) for item in asked[0].content.decode().split("&"))
    assert form["Action"] == "AssumeRoleWithWebIdentity"
    assert form["WebIdentityToken"] == "jwt-1"
    assert form["RoleSessionName"] == "domainkit"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            httpx2.Response(403, text="<Error/>"),
            r"refused AssumeRoleWithWebIdentity \(403\)",
        ),
        (httpx2.Response(200, text="<Nothing/>"), "without credentials"),
    ],
)
async def test_web_identity_refusals_are_provider_errors(
    tmp_path: Path, answer: httpx2.Response, message: str
) -> None:
    """A refusal or an answer without credentials says so."""
    token = tmp_path / "token"
    token.write_text("jwt")
    source = web_identity_credentials("arn:role", token, region="eu-west-1")
    with stub(lambda request: answer), pytest.raises(KeyProviderError, match=message):
        await source()


@pytest.mark.anyio
async def test_unreachable_credential_endpoints_are_provider_errors(
    tmp_path: Path,
) -> None:
    """STS or the container's endpoint down: a provider error."""

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    token = tmp_path / "token"
    token.write_text("jwt")
    with override_settings(HTTP_CLIENT={"RETRIES": 0}), stub(down):
        with pytest.raises(KeyProviderError, match="STS could not be reached"):
            await web_identity_credentials("arn:role", token, region="eu-west-1")()
        with pytest.raises(KeyProviderError, match="endpoint could not be reached"):
            await container_credentials("http://169.254.170.2/creds")()


@pytest.mark.anyio
async def test_container_credentials_send_the_authorization(tmp_path: Path) -> None:
    """The endpoint's JSON gives the credentials; the token goes in Authorization."""
    asked: list[httpx2.Request] = []

    def endpoint(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(
            200,
            json={
                "AccessKeyId": "ASIA2",
                "SecretAccessKey": "s2",
                "Token": "t2",
                "Expiration": "2026-10-31T13:00:00",
            },
        )

    with stub(endpoint), freeze_time(NOW):
        found = await container_credentials(
            "http://169.254.170.23/v1/credentials", authorization="pod-token"
        )()
    assert (found.access_key_id, found.session_token) == ("ASIA2", "t2")
    assert found.expires_at == NOW + timedelta(hours=1)
    assert asked[0].headers["authorization"] == "pod-token"
    with (
        stub(lambda request: httpx2.Response(500, text="no")),
        pytest.raises(KeyProviderError, match="answered 500 without credentials"),
    ):
        await container_credentials("http://169.254.170.2/x")()


@pytest.mark.anyio
async def test_default_credentials_take_the_first_source_set_up(
    no_aws: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Variables, then a web identity, then the container; else, say what is missing."""
    with pytest.raises(KeyProviderError, match="unset"):
        await default_credentials("eu-west-1")()
    asked: list[httpx2.Request] = []

    def anywhere(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        if request.url.host.startswith("sts."):
            return httpx2.Response(200, text=STS_ANSWER)
        return httpx2.Response(
            200,
            json={
                "AccessKeyId": "ASIA3",
                "SecretAccessKey": "s3",
                "Expiration": "2026-10-31T13:00:00Z",
            },
        )

    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/1")
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", "plain")
    with stub(anywhere), freeze_time(NOW):
        assert (await default_credentials("eu-west-1")()).access_key_id == "ASIA3"
        assert str(asked[-1].url) == "http://169.254.170.2/v2/credentials/1"
        assert asked[-1].headers["authorization"] == "plain"
        monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://127.0.0.1/c")
        token_file = tmp_path / "pod"
        token_file.write_text("from-file\n")
        monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", str(token_file))
        await default_credentials("eu-west-1")()
        assert str(asked[-1].url) == "http://127.0.0.1/c"
        assert asked[-1].headers["authorization"] == "from-file"
        identity = tmp_path / "identity"
        identity.write_text("jwt")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:role")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(identity))
        assert (await default_credentials("eu-west-1")()).access_key_id == "ASIA1"
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
        assert (await default_credentials("eu-west-1")()).access_key_id == "AKID"


@pytest.mark.anyio
async def test_kms_takes_the_default_credentials(
    no_aws: None, monkeypatch: pytest.MonkeyPatch, kms: FakeKms
) -> None:
    """Without a source, the environment's access key signs."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRETS[ACCESS_KEY])
    provider = AwsKmsKeyProvider(KEY_ARN, region="eu-west-1")
    assert provider.endpoint == "https://kms.eu-west-1.amazonaws.com"
    made = await provider.generate_data_key()
    assert await provider.unwrap(made.wrapped) == made.plaintext


# Vault transit ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_vault_makes_unwraps_and_rewraps_data_keys(vault: FakeVault) -> None:
    """Vault's own ciphertext is the wrapped key; rewrap moves it to the newest."""
    provider = transit(namespace="team-a")
    made = await provider.generate_data_key()
    assert made.wrapped.startswith("vault:v1:")
    assert len(made.plaintext) == 32
    assert await provider.unwrap(made.wrapped) == made.plaintext
    vault.rotate("payments")
    moved = await provider.rewrap(made.wrapped)
    assert moved.startswith("vault:v2:")
    vault.min_decryption["payments"] = 2
    assert await provider.unwrap(moved) == made.plaintext
    with pytest.raises(UnknownKey, match="cannot unwrap"):
        await provider.unwrap(made.wrapped)
    first = vault.seen[0]
    assert first.url.path == "/v1/transit/datakey/plaintext/payments"
    assert json.loads(first.content) == {"bits": 256}
    assert first.headers["x-vault-namespace"] == "team-a"
    assert "x-request-id" not in first.headers
    assert repr(provider) == (
        "VaultTransitKeyProvider(url='https://vault.test:8200', key='payments')"
    )


@pytest.mark.anyio
async def test_vault_takes_a_token_that_renews(vault: FakeVault) -> None:
    """A callable token is asked before each call; a refused one is a provider error."""
    tokens = iter(["s.test-token", "s.expired"])

    async def token() -> str:
        return next(tokens)

    provider = transit(token=token, mount="/transit/")
    made = await provider.generate_data_key()
    with pytest.raises(KeyProviderError, match=r"refused decrypt \(403\)"):
        await provider.unwrap(made.wrapped)
    assert "x-vault-namespace" not in vault.seen[0].headers


@pytest.mark.anyio
async def test_vault_refuses_what_it_did_not_wrap_before_calling(
    vault: FakeVault,
) -> None:
    """Another provider's key never reaches Vault."""
    with pytest.raises(UnknownKey, match="Vault did not wrap"):
        await transit().rewrap("local:abc:def")
    assert vault.seen == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            httpx2.Response(
                200, json={"data": {"plaintext": "!!", "ciphertext": "vault:v1:x"}}
            ),
            "without the key",
        ),
        (
            httpx2.Response(200, json={"data": {"ciphertext": "vault:v1:x"}}),
            "without the key",
        ),
        (
            httpx2.Response(200, json={"data": {"plaintext": "AAAA"}}),
            "without the wrapped key",
        ),
        (httpx2.Response(200, json={"data": [1]}), "without data"),
        (httpx2.Response(200, text="not json"), "without data"),
        (
            httpx2.Response(400, json={"errors": ["no such key"]}),
            r"refused datakey/plaintext \(400\)",
        ),
    ],
)
async def test_vault_answers_that_make_no_sense_are_refused(
    answer: httpx2.Response, message: str
) -> None:
    """A broken answer is a provider error, never a key."""
    with stub(lambda request: answer), pytest.raises(KeyProviderError, match=message):
        await transit().generate_data_key()


@pytest.mark.anyio
async def test_vault_that_cannot_be_reached_is_a_provider_error() -> None:
    """A network failure names the action."""

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with (
        override_settings(HTTP_CLIENT={"RETRIES": 0}),
        stub(down),
        pytest.raises(KeyProviderError, match=r"could not be reached \(decrypt\)"),
    ):
        await transit().unwrap("vault:v1:abc")


def test_vault_needs_a_key_and_a_mount() -> None:
    """Empty names are refused."""
    with pytest.raises(ValueError, match="key name and a mount"):
        VaultTransitKeyProvider("https://v", "", token="t")
    with pytest.raises(ValueError, match="key name and a mount"):
        VaultTransitKeyProvider("https://v", "k", token="t", mount="")

"""The test plugin's tools for money-moving projects, and its per-test resets.

TEST-21: authenticator codes, step-up, SCA, approvals, the audit trail as
records, and fake key services. TEST-14: hooks a test adds are dropped.
TEST-19: the ``settings`` fixture reads settings read-only. TEST-13: the
application's own role on the test databases.
"""

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from domainkit.apps import registry
from domainkit.apps.hooks import hooks_kept, on_deployment_warnings
from domainkit.clock import FrozenClock, freeze_time
from domainkit.conf import DatabaseConfig
from domainkit.contrib.audit.testing import RecordedEntry, audit_entries
from domainkit.contrib.auth import totp
from domainkit.exceptions import ImproperlyConfigured
from domainkit.keys.aws import sign
from domainkit.management.base import run_async
from domainkit.testing import (
    Authenticator,
    SettingsOverrides,
    UnexpectedResponse,
    approve_request,
    confirm_sca,
    enrol_totp,
    step_up,
    totp_code,
)
from domainkit.testing import _read_only as read_only
from domainkit.testing.databases import application_role, grant_application_roles
from domainkit.testing.kms import SECRETS, FakeKms, FakeVault
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user, login

START = "2027-01-04T10:00:00Z"
RFC_SECRET = b"12345678901234567890"
"""RFC 6238's SHA-1 test key."""


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """A clock for codes."""
    with freeze_time(START) as clock:
        yield clock


# Codes --------------------------------------------------------------------------


def test_codes_are_rfc_6238s() -> None:
    """RFC 6238's vectors, in six digits; the base-32 secret gives the same."""
    encoded = base64.b32encode(RFC_SECRET).decode().rstrip("=")

    assert totp_code(RFC_SECRET, datetime.fromtimestamp(59, UTC)) == "287082"
    assert totp_code(encoded, datetime.fromtimestamp(1111111109, UTC)) == "081804"
    assert totp_code(encoded.lower(), "2033-05-18T03:33:20Z") == "279037"


def test_codes_are_the_services(frozen: FrozenClock) -> None:
    """Now's code is one ``domainkit.contrib.auth`` accepts now."""
    code = totp_code(RFC_SECRET)

    assert totp.match(RFC_SECRET, code, frozen.now().timestamp(), window=0)


def test_an_app_never_gives_a_step_twice(frozen: FrozenClock) -> None:
    """The second code of a step moves the frozen clock to the next step's start."""
    app = Authenticator(RFC_SECRET)

    first = app.code()
    second = app.code()

    assert first == totp_code(RFC_SECRET, START)
    assert frozen.now() == datetime(2027, 1, 4, 10, 0, 30, tzinfo=UTC)
    assert second == totp_code(RFC_SECRET)
    assert app.code() != second


def test_a_second_code_of_a_step_needs_a_frozen_clock() -> None:
    """Without one, the next code would wait for the next step."""
    app = Authenticator(RFC_SECRET)
    app.code()

    with pytest.raises(ImproperlyConfigured, match="freeze the clock"):
        app.code()


# Through the routes -------------------------------------------------------------


@pytest.fixture
def alice(identity: Api, frozen: FrozenClock) -> TestClient:
    """Alice, logged in, at the identity routes (MFA, step-up, SCA)."""
    create_user("alice")
    client = identity()
    client.headers.update(bearer(login(client, "alice")["access_token"]))
    return client


def test_a_person_enrols_steps_up_and_confirms_a_transfer(alice: TestClient) -> None:
    """Each helper does its step through the application's own routes."""
    app = enrol_totp(alice, PASSWORD)
    step_up(alice, app)
    asked = alice.post(
        "/transfers/authorize",
        json={"amount": {"amount": "5.00", "currency": "EUR"}, "payee": "DE89"},
    )
    authorization = confirm_sca(alice, asked.json()["challenge"], app)

    made = alice.post(
        "/transfers",
        json={"amount": {"amount": "5.00", "currency": "EUR"}, "payee": "DE89"},
        headers={"X-Authorization": authorization},
    )

    assert made.status_code == 200, made.json()
    assert alice.get("/auth/mfa").json()["enabled"] is True


def test_the_trail_is_read_as_records(alice: TestClient) -> None:
    """Every filter narrows; none reads the whole trail, oldest first."""
    enrol_totp(alice, PASSWORD)
    everything = run_async(audit_entries())
    [enrolled] = run_async(audit_entries(action="auth.mfa_enabled"))

    assert isinstance(enrolled, RecordedEntry)
    assert enrolled.outcome == "success"
    assert enrolled in everything
    assert [e.occurred_at for e in everything] == sorted(
        e.occurred_at for e in everything
    )
    assert run_async(audit_entries(actor=enrolled.actor, database="default"))
    assert run_async(audit_entries(target="nothing-has-this-target")) == []


# Refusals -------------------------------------------------------------------------


def answering(**statuses: int) -> TestClient:
    """A client of an application answering each route with its status.

    ``enrol``, ``confirm``, ``step_up``, ``sca``, ``read``, and ``approve``;
    200 unless given. The enrolment answers ``RFC_SECRET``.
    """
    app = FastAPI()

    def answer(name: str, body: dict[str, Any]) -> dict[str, Any]:
        if (status := statuses.get(name, 200)) != 200:
            raise HTTPException(status, detail=f"{name} refused")
        return body

    secret = base64.b32encode(RFC_SECRET).decode()
    app.post("/auth/mfa/totp")(lambda: answer("enrol", {"secret": secret}))
    app.post("/auth/mfa/totp/confirm")(lambda: answer("confirm", {}))
    app.post("/auth/step-up")(lambda: answer("step_up", {}))
    app.post("/auth/sca/{challenge}/confirm")(
        lambda challenge: answer("sca", {"authorization": f"{challenge}.ok"})
    )
    app.get("/requests/1")(lambda: answer("read", {"fingerprint": "f-1"}))

    @app.post("/requests/1/approve")
    def approve(body: dict[str, str]) -> dict[str, Any]:
        return answer("approve", {"status": "approved", **body})

    return TestClient(app)


def test_an_approval_reads_the_fingerprint_first(frozen: FrozenClock) -> None:
    """It approves the version it read."""
    approved = approve_request(answering(), "/requests/1")

    assert approved == {"status": "approved", "fingerprint": "f-1"}


@pytest.mark.parametrize(
    ("refused", "step"),
    [
        ("enrol", "Starting TOTP enrolment"),
        ("confirm", "Confirming TOTP enrolment"),
        ("step_up", "Stepping up"),
        ("sca", "Confirming the SCA challenge"),
        ("read", "Reading the approval request"),
        ("approve", "Approving the request"),
    ],
)
def test_a_refused_step_names_itself_and_the_answer(
    frozen: FrozenClock, refused: str, step: str
) -> None:
    """``UnexpectedResponse``: an assertion, with the status and the body."""
    client = answering(**{refused: 403})
    app = Authenticator(RFC_SECRET)

    steps: dict[str, Callable[[], object]] = {
        "enrol": lambda: enrol_totp(client, PASSWORD),
        "confirm": lambda: enrol_totp(client, PASSWORD),
        "step_up": lambda: step_up(client, app),
        "sca": lambda: confirm_sca(client, "c-1", app),
        "read": lambda: approve_request(client, "/requests/1"),
        "approve": lambda: approve_request(client, "/requests/1"),
    }

    with pytest.raises(UnexpectedResponse) as caught:
        steps[refused]()

    assert str(caught.value) == f'{step}: 403 {{"detail":"{refused} refused"}}'
    assert caught.value.response.status_code == 403
    assert isinstance(caught.value, AssertionError)


def test_the_routes_may_be_elsewhere(frozen: FrozenClock) -> None:
    """``prefix`` names where the project mounts them."""
    app = FastAPI()
    seen: list[str] = []

    @app.post("/id/step-up")
    def stepped() -> dict[str, str]:
        seen.append("step-up")
        return {}

    app.post("/id/sca/c/confirm")(lambda: {"authorization": "c.ok"})

    client = TestClient(app)
    step_up(client, Authenticator(RFC_SECRET), prefix="/id")

    assert confirm_sca(client, "c", Authenticator(RFC_SECRET), prefix="/id") == "c.ok"
    assert seen == ["step-up"]


# Hooks a test adds (TEST-14) -------------------------------------------------------


def test_hooks_added_in_the_block_are_dropped(use_auth: UseAuth) -> None:
    """What was registered before stays; what the block added goes."""
    use_auth()
    before = list(on_deployment_warnings.of("alpha"))

    with hooks_kept():
        on_deployment_warnings(lambda: ["added"], domain="alpha")
        assert len(on_deployment_warnings.of("alpha")) == len(before) + 1

    assert list(on_deployment_warnings.of("alpha")) == before


def test_a_point_first_used_in_the_block_is_emptied(use_auth: UseAuth) -> None:
    """A domain with no hook of the point before has none after."""
    use_auth()
    assert on_deployment_warnings.of("contenttypes") == ()

    with hooks_kept():
        on_deployment_warnings(lambda: ["added"], domain="contenttypes")

    assert on_deployment_warnings.of("contenttypes") == ()


def test_domains_loaded_again_in_the_block_keep_their_hooks(
    use_auth: UseAuth,
) -> None:
    """A later generation's registrations are theirs: nothing is put back."""
    use_auth()
    with hooks_kept():
        use_auth()  # The registry is cleared and populated again.
        on_deployment_warnings(lambda: ["later"], domain="alpha")
        later = list(on_deployment_warnings.of("alpha"))

    assert list(on_deployment_warnings.of("alpha")) == later


def test_points_no_registry_used_are_left_alone() -> None:
    """With no domains loaded, there is nothing to keep or put back."""
    registry.clear()
    with hooks_kept():
        pass

    assert on_deployment_warnings.of("alpha") == ()


# Settings read through the fixture (TEST-19) -------------------------------------


def test_settings_read_through_the_fixture_are_read_only() -> None:
    """Mappings refuse changes, all the way down; assigning the whole works."""
    with ExitStack() as stack:
        settings = SettingsOverrides(stack)
        caches = settings.CACHES

        with pytest.raises(TypeError):
            caches["reports"] = {"URL": "memory://"}
        with pytest.raises(TypeError):
            caches["default"]["URL"] = "memory://other"
        settings.CACHES = {**caches, "reports": {"URL": "memory://"}}

        assert "reports" in settings.CACHES
        assert isinstance(settings.CACHES, MappingProxyType)


def test_lists_and_sets_are_read_only_too() -> None:
    """Lists become tuples and sets frozensets; other values stay."""
    assert read_only({"a": [1, {2}], "b": "text"}) == {
        "a": (1, frozenset({2})),
        "b": "text",
    }
    assert read_only(3) == 3


# The application's own role (TEST-13) ----------------------------------------------


def config(url: str, migration: str | None) -> DatabaseConfig:
    """A database entry."""
    return DatabaseConfig(
        URL=SecretStr(url),
        MIGRATION_URL=None if migration is None else SecretStr(migration),
    )


@pytest.mark.parametrize(
    ("url", "migration", "expected"),
    [
        ("postgresql+asyncpg://app@db/x", None, None),
        ("sqlite+aiosqlite:///x.db", "sqlite+aiosqlite:///x.db", None),
        ("postgresql+asyncpg://db/x", "postgresql+asyncpg://owner@db/x", None),
        ("postgresql+asyncpg://o@db/x", "postgresql+asyncpg://o:pw@db/x", None),
        (
            "postgresql+asyncpg://app:pw@db/x",
            "postgresql+asyncpg://owner@db/x",
            ("app", "postgresql+asyncpg://owner@db/x"),
        ),
    ],
)
def test_only_another_role_than_the_owners_is_granted(
    url: str, migration: str | None, expected: tuple[str, str] | None
) -> None:
    """PostgreSQL, a ``MIGRATION_URL``, and two roles; nothing else."""
    assert application_role(config(url, migration)) == expected


def test_no_entry_to_grant_needs_no_server() -> None:
    """Entries of one role are passed over without connecting."""
    run_async(grant_application_roles({"default": config("sqlite+aiosqlite://", None)}))


@pytest.mark.usefixtures("database_access")
def test_a_role_that_does_not_exist_is_named(database_url: str) -> None:
    """The run stops at once, saying which role to create."""
    if not database_url.startswith("postgresql"):
        pytest.skip("set DOMAINKIT_TEST_POSTGRES_URL to a PostgreSQL server")
    from sqlalchemy import make_url

    url = make_url(database_url).set(username="dk_nobody_at_all", password="x")
    entry = config(url.render_as_string(hide_password=False), database_url)

    with pytest.raises(pytest.UsageError, match="'dk_nobody_at_all', which does not"):
        run_async(grant_application_roles({"reports": entry}))


# Fake key services ---------------------------------------------------------------


def kms_call(kms: FakeKms, operation: str, body: dict[str, Any], **signed: str) -> Any:  # noqa: ANN401
    """Call ``kms`` as AWS would be called, signed with the example key."""
    from domainkit.keys.aws import AwsCredentials

    content = json.dumps(body).encode()
    url = "https://kms.eu-west-1.amazonaws.com/"
    headers = sign(
        "POST",
        url,
        {"X-Amz-Target": f"TrentService.{operation}", **signed},
        content,
        AwsCredentials("AKIDEXAMPLE", SECRETS["AKIDEXAMPLE"]),
        region="eu-west-1",
        service="kms",
        now=datetime.now(UTC),
    )
    return kms(httpx2.Request("POST", url, headers=headers, content=content))


def error_of(response: Any) -> str:  # noqa: ANN401 - a response.
    """The KMS exception an answer names."""
    kind: str = response.json()["__type"]
    return kind.rpartition("#")[2]


def test_the_fake_kms_refuses_what_kms_refuses() -> None:
    """Unknown operations and keys, other key specs; the fake names each."""
    kms = FakeKms()
    kms.create("k-1", alias="alias/bank")
    made = kms_call(
        kms, "GenerateDataKey", {"KeyId": "alias/bank", "KeySpec": "AES_256"}
    )
    blob = made.json()["CiphertextBlob"]

    assert error_of(kms_call(kms, "Encrypt", {})) == "UnknownOperationException"
    assert (
        error_of(
            kms_call(kms, "GenerateDataKey", {"KeyId": "k-1", "KeySpec": "AES_128"})
        )
        == "ValidationException"
    )
    moved = {"CiphertextBlob": blob, "SourceKeyId": "k-1", "DestinationKeyId": "k-9"}
    assert error_of(kms_call(kms, "ReEncrypt", moved)) == "NotFoundException"
    assert kms.calls == ["GenerateDataKey", "Encrypt", "GenerateDataKey", "ReEncrypt"]


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20270104/eu-west-1/kms",
        (
            "AWS4-HMAC-SHA256 Credential=NOBODY/20270104/eu-west-1/kms/aws4_request, "
            "SignedHeaders=host;x-amz-date, Signature=00"
        ),
        (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20270104/eu-west-1/kms/"
            "aws4_request, SignedHeaders=x-amz-target, Signature=00"
        ),
    ],
)
def test_the_fake_kms_refuses_unsigned_requests(authorization: str) -> None:
    """Malformed, unknown access keys, and host or date unsigned: refused."""
    request = httpx2.Request(
        "POST", "https://kms.test/", headers={"Authorization": authorization}
    )

    assert error_of(FakeKms()(request)) == "InvalidSignatureException"


def test_the_fake_kms_takes_other_access_keys() -> None:
    """``secrets`` names the keys it accepts."""
    assert FakeKms(secrets={}).secrets == {}
    assert FakeKms().secrets == SECRETS


def test_the_fake_vault_refuses_unknown_keys_and_paths() -> None:
    """An unknown key is a 400, an unknown path of a known key a 404."""
    vault = FakeVault()
    vault.create("bank")
    token = {"X-Vault-Token": vault.token}
    made = vault(
        httpx2.Request(
            "POST",
            "https://vault.test/v1/transit/datakey/plaintext/bank",
            headers=token,
            json={"bits": 256},
        )
    )
    ciphertext = made.json()["data"]["ciphertext"]

    missing = vault(
        httpx2.Request(
            "POST", "https://vault.test/v1/transit/decrypt/nope", headers=token
        )
    )
    other = vault(
        httpx2.Request(
            "POST",
            "https://vault.test/v1/transit/sign/bank",
            headers=token,
            json={"ciphertext": ciphertext},
        )
    )

    assert (missing.status_code, other.status_code) == (400, 404)

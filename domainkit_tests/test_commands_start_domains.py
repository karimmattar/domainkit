"""Commands that run the application's work start the domains (user report W1).

A command ran without ``registry.lifespan``: with a key service that is
not ``local``, the data keys were never unwrapped. The ring unwraps them
itself where SQLAlchemy's events run (a row loaded, a row written), so
most commands read and write encrypted fields as they are; but a blind
index is computed before the query runs, outside that greenlet, and
raised ``KeysNotLoaded``. ``seed`` and ``privacy export``/``erase`` run
other domains' hooks, written for the started application: they now start
the domains, as ``runworker`` does (``needs_domains``). The other commands
need nothing the domains' startup gives, and must run without it: ``keys
generate`` before any data key exists, ``audit verify`` on a trail whose
startup check refuses.
"""

import io
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import (
    on_erase_subject,
    on_export_subject,
    on_seed,
    on_shutdown,
    on_startup,
)
from domainkit.conf import override_settings
from domainkit.contrib.keys.operations import SettingsKeyring
from domainkit.ddd import F
from domainkit.http.client import stub
from domainkit.keys.ring import KeysNotLoaded, keyring, use_keyring
from domainkit.keys.vault import VaultTransitKeyProvider
from domainkit.management import ActionCommand, action, call_command
from domainkit.management.base import run_async
from domainkit_tests.fake_kms import FakeVault
from domainkit_tests.sample_domains.cards.models import Cardholder, CardNumber, Payment
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork
from domainkit_tests.test_keys_app import ANN, AUDIT, BOB, PAN, PRIVACY, project, save

VAULT = {"URL": "https://vault.test", "KEY": "payments", "TOKEN": "s.test-token"}
type Run = Callable[..., str]


@pytest.fixture
def vault_project(database_url: str, tmp_path: Path) -> Iterator[Run]:
    """Cards whose data keys Vault holds, as a new process finds them.

    Ann has a card and a payment. The ring is made anew, its keys not
    unwrapped, as in a process that has just started. ``run(*argv)``
    runs a command; what it printed.
    """
    fake = FakeVault()
    fake.create("payments")
    provider = VaultTransitKeyProvider(
        "https://vault.test", "payments", token="s.test-token"
    )
    with stub(fake):
        wrapped = run_async(provider.generate_data_key()).wrapped
        with project(
            database_url,
            tmp_path,
            AUDIT,
            PRIVACY,
            PROVIDER="vault",
            VAULT=VAULT,
            DATA_KEYS={"v": wrapped},
        ):
            run_async(
                save(
                    Cardholder(id=ANN, name="Ann", card_number=PAN, passport="P1"),
                    Payment(id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN)),
                )
            )
            use_keyring(SettingsKeyring())
            assert not keyring().loaded

            def run(*argv: str) -> str:
                out = io.StringIO()
                call_command(*argv, stdout=out, stderr=io.StringIO())
                return out.getvalue()

            yield run
    use_keyring(None)


async def payments_of_the_card() -> int:
    """The payments made with Ann's card, found by its blind index."""
    async with CardsUnitOfWork() as uow:
        return len(await uow.payments.filter(F("pan") == CardNumber(PAN)))


def test_seed_runs_with_the_domains_started(vault_project: Run) -> None:
    """A seed that looks up what exists by a blind index works.

    It raised ``KeysNotLoaded``: nothing had unwrapped the data keys, and
    a blind index is computed before the query reaches SQLAlchemy.
    """
    found: list[int] = []

    async def seed_payments() -> None:
        found.append(await payments_of_the_card())

    on_seed(seed_payments, domain="alpha")

    assert vault_project("seed") == "Seeded alpha.\n"
    assert found == [1]


@pytest.mark.parametrize(
    "argv",
    [
        ("privacy", "export", "--subject", str(BOB)),
        ("privacy", "erase", "--subject", str(BOB), "--noinput"),
    ],
)
def test_privacy_runs_each_domains_hooks_with_the_domains_started(
    vault_project: Run, argv: tuple[str, ...]
) -> None:
    """A domain's export and erase hooks may look up by a blind index.

    Bob has no card: privacy loads no sealed value of his before the
    hooks, so nothing had unwrapped the keys, and the hook's lookup raised
    ``KeysNotLoaded`` (as a newsletter's would, finding him by the blind
    index of his address).
    """
    found: list[int] = []

    async def export(subject: str) -> Mapping[str, object]:
        found.append(await payments_of_the_card())
        return {}

    async def erase(subject: str, reason: str) -> Mapping[str, int]:
        found.append(await payments_of_the_card())
        return {}

    on_export_subject(export, domain="alpha")
    on_erase_subject(erase, domain="alpha")

    vault_project(*argv)

    assert found == [1]


def test_commands_that_need_no_startup_read_encrypted_fields_as_they_are(
    vault_project: Run,
) -> None:
    """The ring unwraps the keys where SQLAlchemy's events run.

    ``dumpdata --decrypt`` opens values in ``run_sync``, ``keys status``
    loads the ring itself, and ``privacy purge`` erases through the ORM:
    none of them starts the domains, and each reads what Vault's keys
    sealed. A blind index outside those events still needs the keys.
    """
    assert PAN in vault_project("dumpdata", "--decrypt")
    assert "Current key: v; retired: none." in vault_project("keys", "status")
    vault_project("privacy", "purge")

    use_keyring(SettingsKeyring())
    with pytest.raises(KeysNotLoaded):
        run_async(payments_of_the_card())


@pytest.fixture
def alpha() -> Iterator[None]:
    """The alpha sample domain alone."""
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=["domainkit_tests.sample_domains.alpha"]):
        setup()
        yield
    registry.clear()


@pytest.mark.usefixtures("alpha")
def test_seed_starts_the_domains_and_stops_them() -> None:
    """Startup hooks first, shutdown hooks after, as serving does."""
    happened: list[str] = []

    async def started() -> None:
        happened.append("started")

    async def stopped() -> None:
        happened.append("stopped")

    async def seeded() -> None:
        happened.append("seeded")

    on_startup(started, domain="alpha")
    on_shutdown(stopped, domain="alpha")
    on_seed(seeded, domain="alpha")

    call_command("seed", stdout=io.StringIO())

    assert happened == ["started", "seeded", "stopped"]


class Tool(ActionCommand):
    """One action that needs the domains, one that does not, one that cannot."""

    requires_setup = False

    def __init__(self) -> None:
        """Remember what ran."""
        super().__init__(stdout=io.StringIO())
        self.ran: list[str] = []

    @action(help="work as the application does", needs_domains=True)
    async def work(self) -> None:
        """Remember it."""
        self.ran.append("work")

    @action(help="look around")
    async def look(self) -> None:
        """Remember it."""
        self.ran.append("look")

    @action(help="a sync action", needs_domains=True)
    def sync(self) -> None:
        """Never runs."""
        self.ran.append("sync")


def test_an_action_marked_so_runs_with_the_domains_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``action(needs_domains=True)``: that action only; a sync one is refused."""
    from contextlib import asynccontextmanager

    entered: list[str] = []

    @asynccontextmanager
    async def lifespan(**options: Any) -> Any:  # noqa: ANN401
        entered.append("started")
        yield
        entered.append("stopped")

    monkeypatch.setattr(registry, "lifespan", lifespan)
    tool = Tool()

    tool.run_from_argv(["manage.py", "tool", "work"])
    tool.run_from_argv(["manage.py", "tool", "look"])
    with pytest.raises(TypeError, match=r"needs an async handle \(sync\)"):
        tool.run_from_argv(["manage.py", "tool", "sync"])

    assert tool.ran == ["work", "look"]
    assert entered == ["started", "stopped"]  # Once: for work, not for look.

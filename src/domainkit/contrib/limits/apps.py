"""Install ``domainkit.contrib.limits`` to bound what money may move."""

from collections.abc import Mapping
from typing import Final

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import (
    on_deployment_warnings,
    on_erase_subject,
    on_export_subject,
)
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.routing import router
from domainkit.ddd import DomainError
from domainkit.money import Money, MoneyError

from .settings import LimitDefault, LimitsSettings

MESSAGING: Final = "domainkit.contrib.messaging"
ACCOUNTING: Final = "domainkit.contrib.accounting"


class LimitsConfig(DomainConfig):
    """Limits on money movements: per transaction, per window, per subject."""

    label = "limits"
    verbose_name = "Limits"
    settings_class = LimitsSettings
    requires = ()

    def ready(self) -> None:
        """Check the routing and the defaults; map the table; register the hooks.

        With the ledger installed, its postings are counted against limits
        (``on_posting``): the limits live in the ledger's database, or
        ``setup()`` fails (``limits.E001``). A default of ``LIMITS.DEFAULTS``
        that is not a limit fails it too (``limits.E002``). With messaging,
        old usage is purged on a schedule (``tasks``).
        """
        from .tables import start_mappers
        from .uow import limits_alias

        alias = limits_alias()
        config = registry.settings(LimitsSettings)
        problems = default_problems(config.DEFAULTS)
        if registry.is_installed(ACCOUNTING) and (
            router.target_for("accounting") != alias
        ):
            problems.append(
                CheckMessage(
                    "limits.E001",
                    f"The ledger's postings are counted in their own transaction, "
                    f"so the limits must live in the ledger's database "
                    f"({router.target_for('accounting')!r}), not in {alias!r}.",
                    hint="Route 'limits' where 'accounting' is, in DATABASE_ROUTING.",
                )
            )
        if problems:
            raise CheckFailed("LIMITS cannot be enforced as configured", problems)
        start_mappers()
        on_erase_subject(self.erase_person)
        on_export_subject(self.export_person)
        on_deployment_warnings(self.weak_settings)
        if registry.is_installed(ACCOUNTING):
            from domainkit.contrib.accounting import on_posting

            from .postings import enforce

            on_posting(enforce)
        if registry.is_installed(MESSAGING):
            from .tasks import register

            register(config)

    def weak_settings(self) -> list[str]:
        """``limits.W001``: usage that nothing purges on a schedule.

        Without messaging no worker deletes old usage, and the table grows
        with every movement. A project that runs ``manage.py limits purge``
        from its own scheduler silences it.
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS or registry.is_installed(
            MESSAGING
        ):
            return []
        return [
            CheckMessage(
                "limits.W001",
                f"Old usage is not purged on a schedule: {MESSAGING} is not "
                "installed, so no worker deletes it, and the usage table grows "
                "with every movement.",
                hint=f"Install {MESSAGING}, or run 'manage.py limits purge' from a "
                "scheduler and list limits.W001 in SILENCED_CHECKS.",
            )
        ]

    async def erase_person(self, subject: str, reason: str) -> Mapping[str, int]:
        """Remove the limits set for ``subject``, and its counted movements."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_person(self, subject: str) -> Mapping[str, object]:
        """The limits set for ``subject``, and its counted movements."""
        from .personal import export_subject

        return await export_subject(subject)


def default_problems(defaults: tuple[LimitDefault, ...]) -> list[CheckMessage]:
    """``limits.E002``: defaults that are not limits, or that share a key."""
    from .models import Direction, Limit, Window
    from .usage import default_rule

    problems: list[CheckMessage] = []
    seen: set[tuple[object, ...]] = set()
    for number, default in enumerate(defaults):
        where = f"LIMITS['DEFAULTS'][{number}] ({default.code!r})"
        try:
            rule = default_rule(default)
            Limit.define(
                rule.code,
                rule.kind,
                rule.currency,
                direction=Direction(default.direction),
                subject=rule.subject,
                per_transaction=_money(default.per_transaction, rule.currency),
                window=None if default.window is None else Window(default.window),
                max_amount=_money(default.max_amount, rule.currency),
                max_count=rule.max_count,
                time_zone=rule.time_zone,
                note=default.note,
            )
        except (DomainError, MoneyError, ValueError) as exc:
            problems.append(CheckMessage("limits.E002", f"{where}: {exc}"))
            continue
        key = (rule.code, rule.kind, rule.direction, rule.currency, rule.subject)
        if key in seen:
            problems.append(
                CheckMessage(
                    "limits.E002", f"{where}: another default has the same key."
                )
            )
        seen.add(key)
    return problems


def _money(text: str | None, code: str) -> Money | None:
    """A default's amount as ``Money``; None stays None."""
    return None if text is None else Money.of(text, code)

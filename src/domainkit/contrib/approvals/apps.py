"""Install ``domainkit.contrib.approvals`` for maker-checker on guarded commands."""

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Final

from domainkit.apps import DomainConfig, Requirement, registry
from domainkit.apps.hooks import (
    on_check_application,
    on_erase_subject,
    on_export_subject,
)
from domainkit.checks import CheckMessage
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import AllOf, AnyOf, BasePermission, Not

from .settings import ApprovalsSettings

if TYPE_CHECKING:
    from fastapi import FastAPI

ACCOUNTING: Final = "domainkit.contrib.accounting"
AUTH: Final = "domainkit.contrib.auth"


class ApprovalsConfig(DomainConfig):
    """Pending commands that run once people other than their maker approve them."""

    label = "approvals"
    verbose_name = "Approvals"
    settings_class = ApprovalsSettings
    requires = (
        Requirement(
            "domainkit.contrib.messaging", "approved commands run in the worker"
        ),
        Requirement("domainkit.contrib.audit", "every decision is audited"),
    )

    def ready(self) -> None:
        """Check the routing and the approve rule; map the tables; register.

        The requests live in one database (``DATABASE_ROUTING['approvals']``).
        ``APPROVE_RULE`` is imported now, so a wrong path fails ``setup()``.
        With ``APPROVE_ACTION`` on, the action ``approvals.approve`` is
        registered with auth, which must be installed. The guards of large
        postings and of making superusers are registered when their apps
        are installed and their settings ask.

        Raises:
            ImproperlyConfigured: ``APPROVE_ACTION`` is on without
                ``domainkit.contrib.auth``.
        """
        from .builtin import (
            guard_entries,
            guard_superusers,
            refuse_ungoverned_superusers,
        )
        from .rules import configured_rule
        from .tables import start_mappers
        from .tasks import register
        from .uow import approvals_alias

        config = registry.settings(ApprovalsSettings)
        approvals_alias()
        configured_rule()
        start_mappers()
        register(config)
        on_erase_subject(self.erase_person)
        on_export_subject(self.export_person)
        on_check_application(self.application_problems)
        if config.APPROVE_ACTION:
            if not registry.is_installed(AUTH):
                raise ImproperlyConfigured(
                    "APPROVALS.APPROVE_ACTION asks approvers for authorizations of "
                    f"{AUTH}: install it."
                )
            from .authorizing import register as register_approve

            register_approve()
        if config.ENTRY_THRESHOLDS and registry.is_installed(ACCOUNTING):
            guard_entries(config)
        if registry.is_installed(AUTH):
            from domainkit.contrib.auth.gates import on_make_superuser

            on_make_superuser(refuse_ungoverned_superusers, domain=self.label)
            if config.SUPERUSER_APPROVALS:
                guard_superusers(config)

    def application_problems(self, app: "FastAPI") -> list[str]:
        """``approvals.E001``: ``APPROVE_ACTION`` beside a step-up window.

        The action takes the place of the approver's step-up: an
        ``APPROVE_RULE``, or a policy's ``approve_rule``, that holds a
        ``StepUp`` (inside ``&``, ``|``, or ``~`` too) would ask the
        approver twice, for a window and for the act.
        """
        if not registry.settings(ApprovalsSettings).APPROVE_ACTION:
            return []
        from domainkit.contrib.auth import StepUp

        from .guards import guards
        from .rules import configured_rule, rule_instance

        problems: list[str] = []
        configured = configured_rule()
        if configured is not None and any(
            isinstance(each, StepUp) for each in _nested(configured)
        ):
            problems.append(
                CheckMessage(
                    "approvals.E001",
                    "APPROVALS.APPROVE_ACTION asks each approval for an "
                    "authorization of its own, in place of a step-up, but "
                    "APPROVE_RULE asks StepUp too.",
                    hint="Take StepUp out of APPROVALS.APPROVE_RULE, or turn "
                    "APPROVE_ACTION off.",
                )
            )
        for action, guard in guards().items():
            for policy in guard.policies:
                rule = rule_instance(policy.approve_rule)
                if rule is not None and any(
                    isinstance(each, StepUp) for each in _nested(rule)
                ):
                    problems.append(
                        CheckMessage(
                            "approvals.E001",
                            f"APPROVALS.APPROVE_ACTION asks each approval for an "
                            f"authorization of its own, in place of a step-up, but "
                            f"the policy {policy.name!r} of {action!r} asks StepUp "
                            "in its approve_rule.",
                            hint="Take StepUp out of the policy's approve_rule, or "
                            "turn APPROVE_ACTION off.",
                        )
                    )
        return problems

    async def erase_person(self, subject: str, reason: str) -> Mapping[str, int]:
        """Unlink ``subject`` from the requests they made and decided."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_person(self, subject: str) -> Mapping[str, object]:
        """The requests ``subject`` made, and their decisions."""
        from .personal import export_subject

        return await export_subject(subject)


def _nested(rule: BasePermission) -> Iterator[BasePermission]:
    """``rule``, and the rules it combines (``&``, ``|``, ``~``), at any depth."""
    yield rule
    if isinstance(rule, AllOf | AnyOf):
        for each in rule.rules:
            yield from _nested(each)
    elif isinstance(rule, Not):
        yield from _nested(rule.rule)

"""Install ``domainkit.contrib.approvals`` for maker-checker on guarded commands."""

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Final

from domainkit.apps import DomainConfig, Requirement, registry
from domainkit.apps.hooks import (
    on_check_application,
    on_deployment_warnings,
    on_erase_subject,
    on_export_subject,
)
from domainkit.checks import CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import AllOf, AnyOf, BasePermission, Not

from .settings import ApprovalsSettings

if TYPE_CHECKING:
    from fastapi import FastAPI

ACCOUNTING: Final = "domainkit.contrib.accounting"
AUTH: Final = "domainkit.contrib.auth"
ACTS: Final = ("SUBMIT_ACTION", "APPROVE_ACTION", "REJECT_ACTION", "CANCEL_ACTION")
"""The settings that make a route of the requests ask for an authorization."""


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
        registered with auth, which must be installed; so are
        ``approvals.submit``, ``approvals.reject``, and ``approvals.cancel``
        with ``SUBMIT_ACTION``, ``REJECT_ACTION``, and ``CANCEL_ACTION``
        (ADR 0163). The guards of large
        postings, of making superusers, and of the admin changes
        ``ADMIN_APPROVALS`` names are registered when their apps are
        installed and their settings ask; the checks of admin changes run
        after the project's (``HOLD_ORDER``, ADR 0172).

        Raises:
            ImproperlyConfigured: One of the ``*_ACTION`` settings, or
                ``ADMIN_APPROVALS``, is set without ``domainkit.contrib.auth``.
        """
        from .builtin import (
            HOLD_ORDER,
            check_admin_change,
            check_admin_screen,
            check_group_change,
            check_group_screen,
            guard_admin_changes,
            guard_entries,
            guard_superusers,
            refuse_staff_creation,
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
        on_deployment_warnings(self.weak_settings)
        if asked := [name for name in ACTS if getattr(config, name)]:
            if not registry.is_installed(AUTH):
                raise ImproperlyConfigured(
                    f"APPROVALS.{asked[0]} asks for authorizations of {AUTH}: "
                    "install it."
                )
            from .authorizing import register as register_actions

            register_actions(config)
        if config.ENTRY_THRESHOLDS and registry.is_installed(ACCOUNTING):
            guard_entries(config)
        if config.ADMIN_APPROVALS and not registry.is_installed(AUTH):
            raise ImproperlyConfigured(
                f"APPROVALS.ADMIN_APPROVALS guards the admin API of {AUTH}: install it."
            )
        if registry.is_installed(AUTH):
            from domainkit.contrib.auth.actions import on_held_act
            from domainkit.contrib.auth.gates import (
                on_admin_change,
                on_admin_group_change,
                on_admin_group_screen,
                on_admin_screen,
                on_create_staff,
                on_make_superuser,
            )

            from .services import held_request_waits

            on_make_superuser(refuse_ungoverned_superusers, domain=self.label)
            on_held_act(held_request_waits, domain=self.label)
            if config.SUPERUSER_APPROVALS:
                guard_superusers(config)
            if config.ADMIN_APPROVALS:
                guard_admin_changes(config)
                on_admin_change(check_admin_change, domain=self.label, order=HOLD_ORDER)
                on_admin_group_change(
                    check_group_change, domain=self.label, order=HOLD_ORDER
                )
                on_create_staff(refuse_staff_creation, domain=self.label)
                if config.APPROVE_ACTION:  # The approver's screen, at the twin.
                    on_admin_screen(check_admin_screen, domain=self.label)
                    on_admin_group_screen(check_group_screen, domain=self.label)

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

    def weak_settings(self) -> list[str]:
        """``approvals.W001``: a group's permissions held, a user's groups not.

        ``auth.set_group_permissions`` alone holds granting a permission to
        a group that has members, but a new group made with that permission
        and joined by them grants it as well, unheld (in staging and
        production).
        """
        held = registry.settings(ApprovalsSettings).ADMIN_APPROVALS
        if (
            settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS
            or "auth.set_group_permissions" not in held
            or "auth.set_groups" in held
        ):
            return []
        return [
            CheckMessage(
                "approvals.W001",
                "APPROVALS.ADMIN_APPROVALS holds auth.set_group_permissions "
                "but not auth.set_groups: a group made with the permissions, "
                "and joined, grants them with no approval.",
                hint='Hold "auth.set_groups" too.',
            )
        ]

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

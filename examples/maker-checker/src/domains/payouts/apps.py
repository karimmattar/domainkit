"""Installation hooks for the payouts domain (Django's ``AppConfig``)."""

from domainkit.apps import DomainConfig
from domainkit.contrib.approvals import Policy, guard
from domainkit.contrib.auth.actions import register_action

from domains.payouts import controllers
from domains.payouts.models import SqlAlchemyPayoutsUnitOfWork, start_mappers


class PayoutsConfig(DomainConfig):
    """Map the table; guard large payouts; register the act of sending one."""

    requires = ("domainkit.contrib.approvals", "domainkit.contrib.audit")

    def ready(self) -> None:
        """Connect the layers, and register the guard and the act.

        Over the threshold, a payout needs one approval by a holder of
        ``payouts.approve_payout``; its maker never approves it. Makers
        submit it over HTTP with ``payouts.add_payout``, the permission
        of the payout route. Approvers read what ``show`` says of it.
        """
        start_mappers()
        guard(
            controllers.SEND_PAYOUT,
            controllers.send_payout,
            unit_of_work=SqlAlchemyPayoutsUnitOfWork,
            policies=[
                Policy(
                    "payouts.large_payout",
                    approvals=1,
                    permission="payouts.approve_payout",
                    when=controllers.large,
                ),
            ],
            permission="payouts.add_payout",
            show=controllers.shown,
        )
        register_action(controllers.SEND)

"""Hourly purge of expired tokens and signups (needs ``contrib.messaging``)."""

from datetime import timedelta

from domainkit.contrib.messaging import periodic

from .settings import MAX_STEP_UP_AGE
from .tokens import utc_now
from .uow import AuthUnitOfWork


@periodic(every=timedelta(hours=1), name="domainkit.auth.clear_expired_tokens")
async def clear_expired_tokens() -> None:
    """Delete tokens whose lifetime is over (``cleartokens`` does it by hand).

    Pending signups whose link no longer works go too, with their hashes;
    so do step-ups older than any ``StepUp`` takes, and transactions whose
    authorization no longer works.
    """
    async with AuthUnitOfWork() as uow:
        await uow.tokens.clear_expired()
        await uow.signups.clear_expired()
        await uow.step_ups.clear_older(utc_now() - MAX_STEP_UP_AGE)
        await uow.sca.clear_expired()
        await uow.commit()

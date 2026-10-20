"""Hourly purge of expired tokens and signups (needs ``contrib.messaging``)."""

from datetime import timedelta

from domainkit.contrib.messaging import periodic

from .uow import AuthUnitOfWork


@periodic(every=timedelta(hours=1), name="domainkit.auth.clear_expired_tokens")
async def clear_expired_tokens() -> None:
    """Delete tokens whose lifetime is over (``cleartokens`` does it by hand).

    Pending signups whose link no longer works go too, with their hashes.
    """
    async with AuthUnitOfWork() as uow:
        await uow.tokens.clear_expired()
        await uow.signups.clear_expired()
        await uow.commit()

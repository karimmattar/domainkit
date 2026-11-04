"""Logging in and linking through identity providers.

A login comes back to one of three ends:

- the identity is linked: its user logs in (with their second factor's
  challenge, if they have one);
- it is not, ``IDENTITY.SIGNUP`` is on, and the provider verified its
  email, which no account has: an account is made, its password
  unusable, and the identity linked to it;
- otherwise it is refused: ``identity_email_taken`` (409) when an account
  has the email, since logging in to it would hand it to whoever controls
  the provider's account; the owner logs in and links the identity
  instead. ``identity_not_linked`` (422) else.
"""

import logging
import re
from typing import Final
from uuid import UUID

from domainkit.apps import registry
from domainkit.contrib.auth import AbstractBaseUser, AbstractUser
from domainkit.contrib.auth.integration import UserSignedUp
from domainkit.contrib.auth.models import UserId

from .events import IdentityLinked, IdentityUnlinked
from .exceptions import EmailTaken, IdentityTaken, LastWayIn, NoAccount, UnknownProvider
from .providers import Claims, Provider, make_provider
from .settings import IdentitySettings
from .stores import Flow, IdentityUnitOfWork

__all__ = ["begin", "complete", "provider", "unlink"]

logger = logging.getLogger(__name__)
LOGIN: Final = "login"
LINK: Final = "link"
_USERNAME: Final = re.compile(r"[^\w.@+-]")


def identity_settings() -> IdentitySettings:
    """``IDENTITY``, validated."""
    return registry.settings(IdentitySettings)


def provider(name: str) -> Provider:
    """The provider ``name``.

    Raises:
        UnknownProvider: ``IDENTITY.PROVIDERS`` has none of that name.
    """
    config = identity_settings().PROVIDERS.get(name)
    if config is None:
        raise UnknownProvider("No identity provider has that name.")
    return make_provider(name, config)


async def begin(name: str, purpose: str, user_id: UUID | None = None) -> str:
    """Start a login (a link, for ``user_id``) at ``name``: the browser goes there."""
    chosen = provider(name)
    async with IdentityUnitOfWork() as uow:
        await uow.flows.clear_expired()  # Flows never finished go with each new one.
        flow = await uow.flows.start(
            name, purpose, user_id, identity_settings().FLOW_TTL
        )
        await uow.commit()
    return await chosen.authorization_url(flow.id, flow.nonce, flow.verifier)


async def complete(
    name: str, code: str, state: str
) -> tuple[Flow, AbstractBaseUser | None]:
    """Finish what ``state`` started: the flow, and the user to log in (None: a link).

    Raises:
        UnknownProvider: No provider has the name.
        NoAccount: The state is unknown, expired, or another provider's, or
            the identity is linked to no account and none may be made.
        ProviderError: The provider refused, or answered what does not hold.
        EmailTaken: An account has the identity's email.
        IdentityTaken: The identity is linked to another user (a link).
    """
    chosen = provider(name)
    async with IdentityUnitOfWork() as uow:
        flow = await uow.flows.take(state)
        await uow.commit()
    if flow is None or flow.provider != name:
        raise NoAccount("That login expired or was used; start again.")
    claims = await chosen.exchange(code, flow.verifier, flow.nonce)
    if flow.purpose == LINK and flow.user_id is not None:
        await link(flow.user_id, name, claims)
        return flow, None
    return flow, await account_for(name, claims)


async def account_for(name: str, claims: Claims) -> AbstractBaseUser:
    """The user ``claims`` log in as: linked, or made (see the module).

    Raises:
        NoAccount: None may be made, or the account is inactive.
        EmailTaken: An account has the email.
    """
    async with IdentityUnitOfWork() as uow:
        found = await uow.identities.find(name, claims.subject)
        if found is not None:
            user = await uow.users.find(UserId(found.user_id))
            if user is not None:
                if not user.is_active:
                    raise NoAccount("That account cannot log in.")
                return user
            await uow.identities.forget_user(found.user_id)  # Its user is gone.
        if not (identity_settings().SIGNUP and claims.email and claims.email_verified):
            raise NoAccount("No account is linked to that identity.")
        if await uow.users.find_by_email(claims.email):
            raise EmailTaken(
                "An account has this email: log in to it, then link this identity."
            )
        model = uow.users.model
        if model.signup_fields():
            raise NoAccount("This account needs fields a provider does not give.")
        user = await uow.users.create_user(
            await _free_username(uow, claims), None, email=claims.email
        )
        if isinstance(user, AbstractUser):
            user.verify_email()
        user.record(UserSignedUp(user_id=user.id))
        await uow.identities.link(user.id, name, claims.subject, claims.email)
        await uow.commit()
    # A configured provider's name; line breaks dropped all the same.
    shown = name.replace("\r", "").replace("\n", "")
    logger.info("An account was made for a new %s identity.", shown)
    return user


async def link(user_id: UUID, name: str, claims: Claims) -> None:
    """Link the identity to the user.

    Raises:
        IdentityTaken: Another user has it.
    """
    async with IdentityUnitOfWork() as uow:
        found = await uow.identities.find(name, claims.subject)
        if found is not None and found.user_id != user_id:
            raise IdentityTaken("Another account is linked to that identity.")
        if found is None:
            await uow.identities.link(user_id, name, claims.subject, claims.email)
            user = await uow.users.find(UserId(user_id))
            if user is not None:  # Audited: a new way into the account.
                user.record(IdentityLinked(user_id=user_id, provider=name))
            await uow.commit()


async def unlink(user: AbstractBaseUser, name: str) -> None:
    """Unlink the user's identity at ``name``.

    Raises:
        LastWayIn: It is the user's only way in (no usable password, no
            other identity).
    """
    async with IdentityUnitOfWork() as uow:
        linked = await uow.identities.of_user(user.id)
        others = [each for each in linked if each.provider != name]
        if not others and not user.has_usable_password():
            raise LastWayIn("Set a password or link another identity first.")
        if await uow.identities.unlink(user.id, name):
            stored = await uow.users.find(UserId(user.id))
            if stored is not None:
                stored.record(IdentityUnlinked(user_id=user.id, provider=name))
        await uow.commit()


async def _free_username(uow: IdentityUnitOfWork, claims: Claims) -> str:
    """The email as a username, or the provider's subject if it is taken."""
    email = claims.email or ""
    candidate = _USERNAME.sub("", email)[:150]
    if candidate and not await uow.users.username_taken(candidate):
        return candidate
    return _USERNAME.sub("", f"id-{claims.subject}")[:150]

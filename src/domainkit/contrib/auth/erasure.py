"""Privacy erasure of users: their credentials go with their personal data.

With ``domainkit.contrib.privacy`` installed, ``privacy erase`` erases a
user through ``AbstractUser.erase_personal_data``, which also deactivates
the account and makes its password unusable, in the same commit. Then
``forget_credentials`` deletes the user's tokens (sessions, personal
tokens, refresh tokens, client certificates) and second factor (its
secret and recovery codes): secrets of an account nobody may use.
Deleting a user (``UserRepository.remove``) deletes them the same way.
"""

from typing import TYPE_CHECKING, Final
from uuid import UUID

from domainkit.apps import registry
from domainkit.db.models import model_key

from .uow import AuthUnitOfWork
from .users import get_user_model

if TYPE_CHECKING:
    from domainkit.contrib.privacy import PersonalDataErased

AUTH: Final = "domainkit.contrib.auth"


async def forget_credentials(event: "PersonalDataErased") -> None:
    """Delete the tokens and second factor of a user whose data was erased.

    Subscribed to ``PersonalDataErased`` when privacy is installed; events
    about other models are ignored.
    """
    if not registry.is_installed(AUTH):
        return
    try:
        domain, model = model_key(get_user_model())
    except LookupError:
        return  # A user table this project's domains do not hold.
    if event.model != f"{domain}.{model}":
        return
    async with AuthUnitOfWork() as uow:
        await uow.users.forget_credentials(UUID(event.aggregate_id))
        await uow.commit()

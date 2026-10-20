"""Text messages and push notifications: who they are for, and what they say.

Both are bounded, and their single-line fields refuse line breaks and
control characters. Their ``repr`` names neither the recipient nor the
text, which may hold a one-time code.
"""

import re
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated, Any, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

MAX_SMS_LENGTH: Final = 1600
"""Characters in a text message: ten concatenated parts at most."""
MAX_TITLE_LENGTH: Final = 255
MAX_PUSH_BODY_LENGTH: Final = 4096
MAX_DATA_ITEMS: Final = 50
MAX_DATA_LENGTH: Final = 1024
"""Characters in each key and value of a push notification's ``data``."""
_PHONE: Final = r"^\+[1-9][0-9]{6,14}$"
"""E.164: a plus, a country code, and the number, 15 digits at most."""
_SENDER: Final = r"^(?:[A-Za-z0-9 ]{1,11}|\+[1-9][0-9]{6,14})$"
"""An alphanumeric sender id (11 characters at most), or a number."""
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def check_line(value: str) -> str:
    """Return ``value`` if it holds no line break or control character.

    Raises:
        ValueError: It holds one.
    """
    if _CONTROL.search(value):
        raise ValueError("line breaks and control characters are not allowed")
    return value


class _Notification(BaseModel):
    """What text messages and push notifications share: an id, and a hidden repr."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: uuid.UUID | None = None
    """Set when queued, so a provider can drop a copy a retry sends again."""

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """Show the id alone: the rest is personal, or a secret."""
        yield "message_id", self.message_id

    def prepared(self) -> Self:
        """This notification with an id."""
        if self.message_id is not None:
            return self
        return type(self).model_validate(
            {**self.model_dump(), "message_id": uuid.uuid4()}
        )


class SmsMessage(_Notification):
    """A text message to a phone number in E.164 form (``+33612345678``)."""

    to: str = Field(pattern=_PHONE)
    body: str = Field(min_length=1, max_length=MAX_SMS_LENGTH)
    sender: str | None = Field(default=None, pattern=_SENDER)
    """The sender id the provider shows, if it allows one; None: its default."""

    async def send(self, uow: "UnitOfWork | None" = None) -> "SmsMessage":
        """Queue the message; see ``domainkit.contrib.notifications.send_sms``."""
        from .delivery import queue_sms  # Imports this module.

        return await queue_sms(self, uow)


class PushMessage(_Notification):
    """A push notification to one device, by the token its platform gave it."""

    token: Annotated[
        str, Field(min_length=1, max_length=4096), AfterValidator(check_line)
    ]
    title: Annotated[
        str, Field(max_length=MAX_TITLE_LENGTH), AfterValidator(check_line)
    ]
    body: str = Field(max_length=MAX_PUSH_BODY_LENGTH)
    data: dict[str, str] = Field(default_factory=dict, max_length=MAX_DATA_ITEMS)
    """Values the app receives with the notification."""

    @model_validator(mode="after")
    def check_data(self) -> Self:
        """Keys and values of ``data`` are bounded."""
        for key, value in self.data.items():
            if not (0 < len(key) <= MAX_DATA_LENGTH and len(value) <= MAX_DATA_LENGTH):
                raise ValueError(
                    f"data keys have 1 to {MAX_DATA_LENGTH} characters, and values "
                    f"{MAX_DATA_LENGTH} or fewer"
                )
        return self

    async def send(self, uow: "UnitOfWork | None" = None) -> "PushMessage":
        """Queue the notification; see ``send_push``."""
        from .delivery import queue_push  # Imports this module.

        return await queue_push(self, uow)

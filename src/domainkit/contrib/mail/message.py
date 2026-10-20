"""One email: who it is for, what it says, and its MIME form.

``EmailMessage`` refuses what could change the message's headers (header
injection): line breaks and other control characters in the subject,
addresses, and extra headers, and extra headers that would replace the
ones the message sets itself. Addresses are ``user@example.com`` or
``Name <user@example.com>``, with an ASCII address (an internationalized
domain in its ``xn--`` form).
"""

import email.policy
import re
from collections.abc import Iterator
from email.message import EmailMessage as MIMEMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import TYPE_CHECKING, Annotated, Any, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from domainkit import clock

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

MAX_RECIPIENTS: Final = 100
"""Addresses one message may go to: ``to``, ``cc``, and ``bcc`` together."""
MAX_SUBJECT_LENGTH: Final = 998
MAX_BODY_LENGTH: Final = 1_000_000
"""Characters in the text body, and in the HTML one."""
MAX_HEADERS: Final = 20
MAX_ADDRESS_LENGTH: Final = 320
"""Characters in an address with its display name."""
RESERVED_HEADERS: Final = frozenset(
    {
        "bcc",
        "cc",
        "content-transfer-encoding",
        "content-type",
        "date",
        "from",
        "message-id",
        "mime-version",
        "reply-to",
        "subject",
        "to",
    }
)
"""Headers the message sets itself; ``headers`` may not replace them."""
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
"""Characters that break a header line, or that no header should hold."""
_LOCAL: Final = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
)
_DOMAIN: Final = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_HEADER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
_MESSAGE_ID: Final = r"^<[^<>@\s]+@[^<>@\s]+>$"


def check_line(value: str) -> str:
    """Return ``value`` if it holds no line break or control character.

    Raises:
        ValueError: It holds one.
    """
    if _CONTROL.search(value):
        raise ValueError("line breaks and control characters are not allowed")
    return value


def check_address(value: str) -> str:
    """Return ``value``, trimmed, if it is one email address.

    Raises:
        ValueError: It is not; the message does not quote it.
    """
    text = check_line(value.strip())
    _, address = parseaddr(text)
    local, at, domain = address.rpartition("@")
    if not (
        at
        and (text == address or text.endswith(f"<{address}>"))
        and len(text) <= MAX_ADDRESS_LENGTH
        and len(address) <= 254
        and len(local) <= 64
        and _LOCAL.fullmatch(local)
        and _DOMAIN.fullmatch(domain)
    ):
        raise ValueError("not an email address: user@example.com or Name <...>")
    return text


def address_of(value: str) -> str:
    """The address alone (``user@example.com``) of a checked address."""
    return parseaddr(value)[1]


Address = Annotated[str, AfterValidator(check_address)]
"""An email address, checked; a display name may come with it."""


class EmailMessage(BaseModel):
    """An email: a plain-text body, and an HTML one if it has one.

    Queue it with ``await message.send(uow)``; the sender (``from_email``)
    defaults to ``MAIL.DEFAULT_FROM_EMAIL``. Its ``repr`` names no
    recipient and none of its text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: Annotated[
        str, Field(max_length=MAX_SUBJECT_LENGTH), AfterValidator(check_line)
    ]
    body: str = Field(max_length=MAX_BODY_LENGTH)
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    bcc: tuple[Address, ...] = ()
    """Recipients no other recipient sees."""
    reply_to: tuple[Address, ...] = Field(default=(), max_length=MAX_RECIPIENTS)
    from_email: Address | None = None
    html: str | None = Field(default=None, max_length=MAX_BODY_LENGTH)
    """The same message in HTML, sent as an alternative to ``body``."""
    headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_HEADERS)
    """More headers, such as ``List-Unsubscribe``."""
    message_id: str | None = Field(default=None, pattern=_MESSAGE_ID)
    """Set when the message is queued, so a retry sends the same message."""

    @model_validator(mode="after")
    def check_message(self) -> Self:
        """Some recipients, not too many, and headers that change nothing else."""
        count = len(self.to) + len(self.cc) + len(self.bcc)
        if not 0 < count <= MAX_RECIPIENTS:
            raise ValueError(
                f"a message goes to 1 to {MAX_RECIPIENTS} addresses (to, cc, bcc)"
            )
        for name, value in self.headers.items():
            if not _HEADER_NAME.fullmatch(name) or name.lower() in RESERVED_HEADERS:
                raise ValueError(
                    "headers are named with letters, digits, and '-', and may not "
                    "replace From, To, Subject, or the message's own headers"
                )
            if len(value) > MAX_SUBJECT_LENGTH:
                raise ValueError(f"header values have {MAX_SUBJECT_LENGTH} or fewer")
            check_line(value)
        return self

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """Show the message id alone: the rest may be personal, or a secret."""
        yield "message_id", self.message_id

    def recipients(self) -> list[str]:
        """Every address it goes to (``to``, ``cc``, ``bcc``), once each."""
        addresses = (address_of(value) for value in (*self.to, *self.cc, *self.bcc))
        return list(dict.fromkeys(addresses))

    def prepared(self, default_from: str) -> "EmailMessage":
        """This message with a sender (``default_from`` if it has none) and an id."""
        sender = self.from_email or default_from
        domain = address_of(sender).rpartition("@")[2]
        return type(self).model_validate(
            {
                **self.model_dump(),
                "from_email": sender,
                "message_id": self.message_id or make_msgid(domain=domain),
            }
        )

    def to_mime(self) -> MIMEMessage:
        """The message as the standard library's ``EmailMessage``, ready to send.

        ``bcc`` is left out of the headers; ``recipients()`` still has it.

        Raises:
            ValueError: The message has no sender or id yet (``prepared``).
        """
        if self.from_email is None or self.message_id is None:
            raise ValueError("Prepare the message before sending it: prepared().")
        message = MIMEMessage(policy=email.policy.SMTP)
        message["Subject"] = self.subject
        message["From"] = self.from_email
        for name, values in (
            ("To", self.to),
            ("Cc", self.cc),
            ("Reply-To", self.reply_to),
        ):
            if values:
                message[name] = ", ".join(values)
        message["Date"] = formatdate(clock.now().timestamp(), usegmt=True)
        message["Message-ID"] = self.message_id
        for name, value in self.headers.items():
            message[name] = value
        message.set_content(self.body)
        if self.html is not None:
            message.add_alternative(self.html, subtype="html")
        return message

    async def send(self, uow: "UnitOfWork | None" = None) -> "EmailMessage":
        """Queue the message; see ``domainkit.contrib.mail.send``."""
        from .delivery import send  # Imports this module.

        return await send(self, uow)

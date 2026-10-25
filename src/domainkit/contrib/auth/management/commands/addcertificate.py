"""``addcertificate``: let a client certificate log in as a user (mutual TLS)."""

from argparse import ArgumentParser
from pathlib import Path

from domainkit.management.base import BaseCommand, CommandError, run_async

from ...authentication import AuthenticationFailed
from ...certificates import fingerprint
from ...events import TokenCreated
from ...uow import AuthUnitOfWork
from .createtoken import NAME_MAX_LENGTH


class Command(BaseCommand):
    """Register a certificate's SHA-256 for a user; print the fingerprint."""

    help = (
        "Let a client certificate (PEM) authenticate as a user, with "
        'AUTH.CLIENT_CERT.MATCH = "fingerprint"; cleartokens --user removes it.'
    )
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """The user, the certificate file, and a name."""
        parser.add_argument("username", help="whom the certificate logs in as")
        parser.add_argument("certificate", type=Path, help="the certificate, in PEM")
        parser.add_argument("--name", default="certificate", help="what it is for")

    def handle(self, *, username: str, certificate: Path, name: str) -> None:
        """Store the fingerprint and print it."""
        if len(name) > NAME_MAX_LENGTH:
            raise CommandError(f"--name has at most {NAME_MAX_LENGTH} characters.")
        try:
            digest = fingerprint(certificate.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, AuthenticationFailed) as exc:
            raise CommandError(f"{certificate} is not a PEM certificate.") from exc
        run_async(self._add(username, digest, name))
        self.write(f"SHA-256 {digest} logs in as {username!r}.")

    async def _add(self, username: str, digest: str, name: str) -> None:
        """Store the certificate as a token of its kind; record ``TokenCreated``."""
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            existing = await uow.tokens.find_digest(digest)
            if existing is not None:
                raise CommandError("That certificate is registered already.")
            await uow.tokens.add_certificate(user.id, digest, name=name)
            user.record(TokenCreated(user_id=user.id, name=name))
            await uow.commit()

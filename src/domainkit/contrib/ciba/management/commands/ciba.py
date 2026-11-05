"""``ciba``: register, list, rotate, and remove CIBA clients."""

from pathlib import Path
from typing import Any

from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import ActionCommand, CommandError, action, option

from ...clients import METHODS, register, rotate_secret
from ...stores import CibaUnitOfWork


class Command(ActionCommand):
    """``list`` (the default), ``add``, ``rotate``, and ``remove`` clients."""

    help = "Register, list, rotate, and remove the OIDC clients that use CIBA."
    default_action = "list"

    @action(name="list", help="every client: its id, name, method, and scopes")
    async def listed(self) -> None:
        """One line per client."""
        async with CibaUnitOfWork() as uow:
            found = await uow.clients.all()
        if not found:
            self.write("No CIBA client is registered.")
        for client in found:
            self.write(
                f"{client.client_id}  {client.name!r}  {client.auth_method}  "
                f"scopes: {client.scopes or '-'}"
            )

    @action(
        help="register a client; a secret is shown once",
        options=[
            option("name", help="what the user's phone shows"),
            option("--scope", nargs="*", default=[], help="permission keys allowed"),
            option("--method", choices=METHODS, default="client_secret_basic"),
            option("--jwks", type=Path, help="a file of the client's JWK Set"),
        ],
        operator=True,
    )
    async def add(self, **options: Any) -> None:  # noqa: ANN401 - the options.
        """Register the client and print its id, and its secret if any."""
        path: Path | None = options["jwks"]
        try:
            jwks = None if path is None else _read(path)
            client, secret = await register(
                options["name"],
                scopes=options["scope"],
                method=options["method"],
                jwks=jwks,
            )
        except (ImproperlyConfigured, OSError) as exc:
            raise CommandError(str(exc)) from None
        self.write(f"client_id: {client.client_id}")
        if secret is not None:
            self.write(f"client_secret: {secret}")
            self.write("Keep the secret now: it is shown once, and stored hashed.")

    @action(
        help="give a client a new secret; the old one stops working",
        options=[option("client_id")],
        operator=True,
    )
    async def rotate(self, *, client_id: str) -> None:
        """Print the new secret."""
        try:
            secret = await rotate_secret(client_id)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        self.write(f"client_secret: {secret}")

    @action(
        help="remove a client: it can start and poll no more; tokens it holds run out",
        options=[option("client_id")],
        operator=True,
    )
    async def remove(self, *, client_id: str) -> None:
        """Remove the client."""
        async with CibaUnitOfWork() as uow:
            removed = await uow.clients.remove(client_id)
            await uow.commit()
        if not removed:
            raise CommandError(f"No client has id {client_id!r}.")
        self.write(f"Removed {client_id}.")


def _read(path: Path) -> str:
    """The text of a small local file, read before the database is."""
    return path.read_text(encoding="utf-8")

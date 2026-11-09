"""``unbinddevices``: unbind a user's devices, for one who lost their phone."""

from argparse import ArgumentParser

from domainkit.contrib.auth.integration import UserLoggedOut
from domainkit.management.base import BaseCommand, CommandError, run_async

from ...events import DeviceUnbound
from ...stores import DevicesUnitOfWork


class Command(BaseCommand):
    """Unbind every device of a user, and their push tokens; end their sessions."""

    help = (
        "Unbind every device of a user who lost their phone, and forget its push "
        "tokens; every session of the user ends. With CIBA.APPROVE_LOGINS, the "
        "user then logs in with their other factors and binds the new phone; "
        "with APPROVE_LOGINS = 'always', the new phone enrolls (/devices/enroll). "
        "Check who is asking first."
    )
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """The user."""
        parser.add_argument(
            "username",
            help="whose devices: a username, email address, or mobile number",
        )

    def handle(self, *, username: str) -> None:
        """Unbind the devices and say how many."""
        count = run_async(self._unbind(username))
        if count:
            self.write(f"Unbound {count} device(s) of {username!r}; they log in again.")
        else:
            self.write(f"{username!r} had no bound device.")

    async def _unbind(self, username: str) -> int:
        """Unbind, forget the tokens, end the sessions; record each unbinding."""
        async with DevicesUnitOfWork() as uow:
            user = await uow.users.find_for_staff(username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            devices = await uow.devices.of_user(user.id)
            if not devices:
                return 0
            await uow.devices.forget_user(user.id)
            await uow.push_tokens.forget_user(user.id)
            await uow.tokens.revoke_user(user.id)
            for device in devices:
                user.record(DeviceUnbound(user_id=user.id, device_id=device.id))
            user.record(UserLoggedOut(user_id=user.id, everywhere=True))
            await uow.commit()
        return len(devices)

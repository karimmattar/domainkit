"""The unit of work over users, groups, and permissions."""

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyUnitOfWork

from .repositories import (
    GroupRepository,
    MfaStore,
    PasswordHistoryStore,
    PermissionStore,
    PrincipalStore,
    ScaStore,
    SignupStore,
    StepUpStore,
    TokenStore,
    UserRepository,
)


class AuthUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the auth tables, wherever ``auth`` is routed.

    For example::

        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_user("alice", password)
            user.join(await uow.groups.create("editors"))
            await uow.commit()
    """

    users: UserRepository
    groups: GroupRepository
    permissions: PermissionStore
    principals: PrincipalStore
    tokens: TokenStore
    mfa: MfaStore
    signups: SignupStore
    step_ups: StepUpStore
    sca: ScaStore
    passwords: PasswordHistoryStore

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repositories to the session."""
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)
        self.permissions = PermissionStore(session)
        self.principals = PrincipalStore(session)
        self.tokens = TokenStore(session)
        self.mfa = MfaStore(session)
        self.signups = SignupStore(session)
        self.step_ups = StepUpStore(session)
        self.sca = ScaStore(session)
        self.passwords = PasswordHistoryStore(session)

"""SEC-12a-2: ``AuthConfig.issue_test_token_for`` mints a token where deployed.

``token_for(user)`` and the clients' ``authenticated_as(user)`` sign a test
in as a stored user, as DRF's ``force_authenticate`` does: they ask the
first installed domain that is a ``StoredUserTokenIssuer``, and
``AuthConfig`` is one in every application that installs auth, deployed
or not. In production it still hands out a full access token for any
user id, a superuser's included: no password, no second factor, no
``auth.login`` audit record, no operator named. The database fixtures
refuse settings whose ``ENVIRONMENT`` is not ``"test"``
(``check_test_settings``); this testing door does not, so whatever code
runs in a deployed process (a shell, a task, a plugin that imports
``domainkit.testing``) gets a bearer token for anyone. Where deployed it
must be refused.
"""

import pytest

from domainkit.conf import override_settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.contrib.auth.testing import create_user
from domainkit.testing import token_for

pytestmark = pytest.mark.anyio


async def test_no_test_token_for_a_stored_user_where_deployed(
    auth_project: str,
) -> None:
    """``token_for`` refuses in production, as the database fixtures do."""
    admin = await create_user(superuser=True, username="root")
    assert "production" in DEPLOYED_ENVIRONMENTS
    with override_settings(ENVIRONMENT="production"):
        try:
            token = await token_for(admin)
        except Exception:  # noqa: BLE001 - any refusal will do.
            return
    pytest.fail(
        f"a deployed application minted an access token for superuser "
        f"{admin.id} without credentials: {token[:12]}..."
    )

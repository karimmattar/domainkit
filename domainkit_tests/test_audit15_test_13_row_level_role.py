"""TEST-13: the test databases of a project with two roles are unusable.

``docs/deploying.md`` and ``docs/security.md`` ask for two PostgreSQL roles:
the application's, which cannot change the schema, in ``URL``, and the
schema owner's in ``MIGRATION_URL``. ``docs/testing.md`` says the plugin
creates ``test_<name>`` as the owner. It does, and migrates it as the owner,
then runs every test as ``URL``'s role, which was granted nothing on the
tables of a database made a moment ago (default privileges belong to a
database, so the production database's do not carry over). The first test
that stores a row fails with "permission denied". CI never sees it: its
PostgreSQL job runs every test as the superuser ``postgres``. PostgreSQL
only; fails on ``d126394``.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import make_url

from domainkit.conf import override_settings
from domainkit_tests.conftest import MIDDLEWARE, POSTGRES_VARIABLE, on_server
from domainkit_tests.test_testing import prefix, run_plugin  # noqa: F401 - fixture.

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
)


def test_a_row_level_application_role_can_run_the_projects_tests(
    auth_project: str,
    prefix: str,  # noqa: F811 - the fixture imported above.
    pytester: pytest.Pytester,
) -> None:
    """The application's role reads and writes the test databases' tables."""
    name = f"dk_{uuid4().hex[:12]}"
    role = f"dk_app_{uuid4().hex[:12]}"
    server = make_url(os.environ[POSTGRES_VARIABLE])
    on_server(
        os.environ[POSTGRES_VARIABLE],
        f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'app' NOSUPERUSER NOCREATEDB",
    )
    pytester.makepyfile(
        test_roles="""
        def test_the_application_stores_a_user(client_for):
            response = client_for("shop.view_product").get("/products")
            assert response.status_code == 200
        """
    )
    owner = server.set(database=name).render_as_string(hide_password=False)
    application = (
        server.set(database=name, username=role, password="app")
    ).render_as_string(hide_password=False)
    try:
        with override_settings(
            DATABASES={"default": {"URL": application, "MIGRATION_URL": owner}},
            ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
            MIDDLEWARE=MIDDLEWARE[:3],
        ):
            result = run_plugin(pytester)

        assert "permission denied" not in result.stdout.str()
        result.assert_outcomes(passed=1)
    finally:
        on_server(
            os.environ[POSTGRES_VARIABLE],
            f'DROP DATABASE IF EXISTS "test_{name}" WITH (FORCE)',
        )
        on_server(os.environ[POSTGRES_VARIABLE], f'DROP ROLE IF EXISTS "{role}"')

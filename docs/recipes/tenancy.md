# Organizations in the path: one deployment, many customers

This recipe serves many organizations from one deployment. Each one
works at its own path, `/orgs/{org_id}/...`, sees only its own rows, and
gives its members what their role allows.

## The problem

A project-tracking service sells to companies. Acme and Globex use the
same deployment and the same tables. Three things must hold:

- **Rows stay apart.** A query that forgets a `WHERE org = ...` shows
  Acme's projects to Globex. It must be impossible to forget.
- **Strangers learn nothing.** A Globex user who types an Acme URL must
  not even learn that Acme exists.
- **Roles hold in their organization only.** Ann owns Acme's projects
  and only reads Globex's. A permission granted everywhere cannot say
  that.

domainkit's tenancy does each part ([tenancy](../tenancy.md)):

- a `TenantOwned` aggregate's repository reads and changes only the
  current tenant's rows, and sets the tenant on what it adds;
- `PathTenant` takes the tenant from the path, and answers a caller who
  does not belong `404`, word for word as an unknown route;
- `MEMBER_PERMISSIONS` names a function that says what a user holds as a
  member of one tenant, and `MembershipBackend` counts it in every
  permission check of that tenant's requests.

This recipe keeps to rows, routes, and members' permissions. You need a
project with `domainkit[sqlite,testing]` installed. Every step here is
run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project and a domain

Make a project and a `projects` domain. `--manual` neither installs nor
mounts it: you mount it under the organization's path yourself, in step
5:

```bash
domainkit init teamsAPI teams
cd teams
python manage.py startdomain projects --aggregate Project --minimal --manual
```

### Step 2: make projects rows of one organization

A project belongs to one organization. In
`src/domains/projects/domain/model.py`, make `Project` a `TenantOwned`
aggregate, and add a `Membership`: who belongs to which organization,
and with which role. Memberships are not owned by a tenant: they are
the list that says who may enter each one.

```python title="src/domains/projects/domain/model.py"
from domainkit.ddd.tenancy import TenantOwned

MembershipId = NewType("MembershipId", UUID)


class Project(TenantOwned, AggregateRoot[ProjectId]):
    """One project of one organization; change it only through its methods."""

    name: Annotated[str, MaxLength(NAME_LENGTH)]

    @classmethod
    def create(cls, name: str) -> Self:
        """Start a new project, in the organization being served."""
        return cls(id=ProjectId(uuid4()), name=_checked(name))

    def rename(self, name: str) -> None:
        """Change the name."""
        self.name = _checked(name)


class Membership(AggregateRoot[MembershipId]):
    """A user's role in one organization."""

    user_id: UUID
    org: Annotated[str, MaxLength(63)]
    role: Annotated[str, MaxLength(20)]

    @classmethod
    def of(cls, user_id: UUID, org: str, role: str) -> Self:
        """``user_id`` as a member of ``org``, in ``role``."""
        return cls(id=MembershipId(uuid4()), user_id=user_id, org=org, role=role)


class MembershipRepository(Repository[Membership, MembershipId]):
    """Every membership, in every organization (a port)."""
```

`TenantOwned` adds a `tenant_id` field, which `table_for` makes a
column. The repository fills it when it adds a project, from the tenant
being served, and keeps every read to it.

Give the unit of work the memberships, in
`src/domains/projects/application/unit_of_work.py`:

```python title="src/domains/projects/application/unit_of_work.py"
from domains.projects.domain.model import MembershipRepository


class ProjectsUnitOfWork(UnitOfWork, ABC):
    ...
    memberships: MembershipRepository
```

Store them: a table in `src/domains/projects/infrastructure/orm.py`,
and a repository in `src/domains/projects/infrastructure/unit_of_work.py`:

```python title="src/domains/projects/infrastructure/orm.py"
from domains.projects.domain.model import Membership

memberships = table_for(Membership, "projects_memberships", domain="projects")


def start_mappers() -> None:
    """Map the aggregates onto their tables (idempotent)."""
    map_imperatively(Project, projects)
    map_imperatively(Membership, memberships)
```

```python title="src/domains/projects/infrastructure/unit_of_work.py"
from domains.projects.domain.model import (
    Membership,
    MembershipId,
    MembershipRepository,
)


class SqlAlchemyMembershipRepository(
    SqlAlchemyRepository[Membership, MembershipId], MembershipRepository
):
    """Memberships in the ``projects_memberships`` table."""

    model = Membership


class SqlAlchemyProjectsUnitOfWork:
    ...

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repositories to ``session``."""
        self.projects = SqlAlchemyProjectRepository(session)
        self.memberships = SqlAlchemyMembershipRepository(session)
```

The tests' in-memory unit of work needs them too, in
`tests/domains/projects/fakes.py`:

```python title="tests/domains/projects/fakes.py"
from domains.projects.domain.model import (
    Membership,
    MembershipId,
    MembershipRepository,
)


class InMemoryMembershipRepository(
    InMemoryRepository[Membership, MembershipId], MembershipRepository
):
    """Dictionary-backed ``MembershipRepository``."""


class FakeProjectsUnitOfWork:
    ...

    def __init__(self, events: EventBus | None = None) -> None:
        """Start empty; publish to ``events`` (a private bus in tests)."""
        super().__init__(events=events or EventBus())
        self.projects = InMemoryProjectRepository()
        self.memberships = InMemoryMembershipRepository()
        self.commits = 0
```

### Step 3: say what each role allows

A member's role gives them permission keys in their organization. Make
the new file `src/domains/projects/tenancy.py`:

```python title="src/domains/projects/tenancy.py (new file)"
"""What a user may do as a member of one organization."""

from typing import Any, Final

from domainkit.ddd import F

from domains.projects.infrastructure.unit_of_work import SqlAlchemyProjectsUnitOfWork

ROLES: Final = {
    "owner": {
        "projects.view_project",
        "projects.add_project",
        "projects.change_project",
    },
    "viewer": {"projects.view_project"},
}
"""The permission keys of each role, in its organization only."""


async def member_permissions(user: Any, org: str) -> set[str] | None:
    """The keys ``user`` holds in ``org``, or None for one who is not a member."""
    if not user.is_authenticated:
        return None
    async with SqlAlchemyProjectsUnitOfWork() as uow:
        membership = await uow.memberships.first(
            (F("user_id") == user.id) & (F("org") == org)
        )
    return None if membership is None else ROLES[membership.role]
```

### Step 4: turn tenancy on

Install the domain, name the function, and add `MembershipBackend`,
which grants a member their role's keys in the tenant being served. Add
these lines at the end of `src/config/settings/base.py`:

```python title="src/config/settings/base.py"
INSTALLED_DOMAINS = [*INSTALLED_DOMAINS, "domains.projects"]
TENANCY = {"MEMBER_PERMISSIONS": "domains.projects.tenancy.member_permissions"}
AUTH = {
    **AUTH,
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "domainkit.contrib.auth.backends.MembershipBackend",
    ],
}
```

The routes keep the rule that `startdomain` gave them,
`ModelActions(Project)`: it asks for `projects.view_project` to read and
`projects.add_project` to create. With `MembershipBackend`, a member's
role answers it, in the organization the path names, and nowhere else.

### Step 5: mount the domain under the organization's path

`PathTenant("org_id")` serves every route of the router as the tenant
the path names, once it has checked that the caller is a member. Add
these lines at the end of `src/config/urls.py`:

```python title="src/config/urls.py"
from domainkit.tenancy import PathTenant
from fastapi import Depends

from domains.projects.api.routes import router as projects_router

router.include_router(
    projects_router,
    prefix="/orgs/{org_id}",
    dependencies=[Depends(PathTenant("org_id"))],
)
```

`PathTenant` is the router's first dependency, before any rule: a rule
asked before it would see no tenant (`check` refuses that, as
`tenancy.E002`). Make the tables:

```bash
python manage.py makemigrations -m "projects and memberships"
python manage.py migrate
```

### Step 6: test it

`startdomain` wrote tests of a project with no organization. Replace
them. In `tests/domains/projects/test_services.py`, the use cases run in
an organization, as a request would serve them:

```python title="tests/domains/projects/test_services.py"
from domainkit.tenancy import tenant


async def test_create_and_rename_commit_once_each() -> None:
    """The name is trimmed, and the project is Acme's."""
    uow = FakeProjectsUnitOfWork()

    with tenant("acme"):
        project = await services.create_project(CreateProject(name=" First "), uow)
        command = RenameProject(project_id=project.id, name="Second")
        await services.rename_project(command, uow)

    assert (project.name, project.tenant_id, uow.commits) == ("Second", "acme", 2)


async def test_a_blank_name_is_refused_before_anything_is_stored() -> None:
    """The aggregate's rule holds in every organization."""
    uow = FakeProjectsUnitOfWork()

    with tenant("acme"), pytest.raises(InvalidProjectName):
        await services.create_project(CreateProject(name="   "), uow)

    assert (uow.projects.items, uow.commits) == ({}, 0)
```

In `tests/domains/projects/test_api.py`, Ann owns Acme's projects, and
Bob belongs to Globex:

```python title="tests/domains/projects/test_api.py"
from uuid import UUID

from domainkit.contrib.auth.testing import create_user

from domains.projects.domain.model import Membership
from domains.projects.infrastructure.unit_of_work import SqlAlchemyProjectsUnitOfWork


async def join(user_id: UUID, org: str, role: str) -> None:
    """Store a membership, as an invitation accepted would."""
    async with SqlAlchemyProjectsUnitOfWork() as uow:
        await uow.memberships.add(Membership.of(user_id, org, role))
        await uow.commit()


def member(client: TestClient, org: str, role: str) -> TestClient:
    """A client of a new user, a member of ``org`` in ``role``."""
    user = client.run(create_user)
    client.run(join, user.id, org, role)
    return client.authenticated_as(user)


def test_create_read_rename_and_list(client: TestClient) -> None:
    """An owner works in their organization; a viewer reads and changes nothing."""
    ann = member(client, "acme", "owner")
    viewer = member(client, "acme", "viewer")

    created = ann.post("/orgs/acme/projects", json={"name": "Site"})
    item = created.headers["location"]
    renamed = ann.post(f"{item}/rename", json={"name": "Website"})
    refused = viewer.post("/orgs/acme/projects", json={"name": "Mine"})

    assert created.status_code == 201
    assert item.startswith("/orgs/acme/projects/")
    assert renamed.json()["name"] == "Website"
    listed = viewer.get("/orgs/acme/projects").json()["items"]
    assert [each["name"] for each in listed] == ["Website"]
    assert (refused.status_code, refused.json()["code"]) == (403, "permission_denied")


def test_callers_log_in_and_names_follow_the_rule(client: TestClient) -> None:
    """A stranger gets 404, as for no route; rows and roles stay in their tenant."""
    ann = member(client, "acme", "owner")
    bob = member(client, "globex", "owner")
    item = ann.post("/orgs/acme/projects", json={"name": "Site"}).headers["location"]
    project = item.rsplit("/", 1)[1]

    assert bob.get("/orgs/acme/projects").status_code == 404
    assert bob.get(f"/orgs/globex/projects/{project}").status_code == 404
    assert bob.get("/orgs/globex/projects").json()["items"] == []
    assert client.get("/orgs/acme/projects").status_code == 401
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format .
pytest
```

`ruff --fix` sorts the imports the edits added; `ruff format` lays out
the lines they made long.

## How it works

1. A request to `/orgs/acme/projects` is routed, then `PathTenant`
   reads `acme` from the path and asks `member_permissions` about the
   caller. A non-member who logged in gets `404 not_found`, as for a
   route that does not exist; an anonymous caller gets `401`.
2. For a member, the request is served as `acme`: the repository of
   `Project` reads and changes only rows whose `tenant_id` is `acme`,
   and a project made in the request gets it.
3. The route's rule asks for `projects.add_project`. `MembershipBackend`
   answers with the keys of the member's role in `acme`, asked once per
   request. The same user in Globex holds Globex's role, and nothing of
   Acme's.

Keep these rules in mind:

- **Never take the tenant from the body.** It comes from the path, which
  `PathTenant` checks; a field the client sends is checked by nothing.
- **Memberships are not tenant rows.** They say who may enter a tenant,
  so they are read before any tenant is served.
- **A role's keys stay in its tenant.** They count toward no grant a
  member makes, and no token's scopes
  ([tenancy](../tenancy.md#members-permissions)).

Caches, rate limits, logs, and background tasks follow the tenant too,
as do a database or a schema per tenant ([tenancy](../tenancy.md)).
Approval requests and live events are their tenant's own: another
tenant's are not found, and not heard. Limits and the books are rows
that every tenant shares (`check` lists them as `tenancy.I001`): their
routes count only the permissions a user holds everywhere, never a
role's, so a member's role that names `limits.change_limit` changes no
limit. The [tenancy](../tenancy.md) page says how, under "Live events
and approvals" and "Rows of no tenant".

## See also

- [Multi-tenancy](../tenancy.md): every way to name the tenant, and
  every part it scopes.
- [Resources](../resources.md#a-resource-whose-items-are-tenants): a
  resource whose items are the tenants themselves.
- [Protecting routes](../auth/routes.md#protecting-routes): the rules
  routes ask, and `MemberActions`, which counts a member's role alone.

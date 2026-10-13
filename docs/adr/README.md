# Architecture decision records

Each record explains one decision that is costly to reverse: its context, what
was decided, and what follows from it. Add a record before such a change, by
copying [template.md](template.md) to the next number. Never rewrite an
accepted record: supersede it with a new one and link both ways.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-django-conventions-for-ddd-services.md) | Django's conventions for domain-driven services | Accepted |
| [0003](0003-enforced-layer-rules.md) | Layer rules enforced by `check` | Accepted |
| [0004](0004-async-sqlalchemy-imperative-mapping.md) | Async SQLAlchemy with imperative mapping | Accepted |
| [0005](0005-transactional-outbox.md) | A transactional outbox instead of a broker | Accepted |
| [0006](0006-environment-prefix-from-project-name.md) | Environment variable prefix from the project name | Accepted |
| [0007](0007-own-repository-released-to-pypi.md) | Its own repository, released to PyPI | Accepted |

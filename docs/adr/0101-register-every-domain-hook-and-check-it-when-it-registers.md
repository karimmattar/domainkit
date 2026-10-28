# 0101. Register every domain hook, and check it when it registers

- Status: Accepted
- Date: 2026-10-28
- Amends: [0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md)

## Context

[0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md)
moved the SQL commit and migration hooks of `DomainConfig` to typed
points a domain registers on in `ready()`, and left the rest as methods
every domain inherits: `startup`, `shutdown`, `check`, `seed`,
`erase_subject`, `export_subject`, `check_application`,
`deployment_warnings`, `holds_table`, and `databases` (DSGN-03 of the
0.9.0 design audit). The framework called each on every installed domain.

What a project lost when a domain got one wrong, on 0.11.1 (a domain with
a misspelt `erase_subjects`, a `stage(self, uow)`, and a plain `def
check(self)`):

```text
setup ok: no error, no warning
erase_subject -> typo {}
misspelt hook called: []
Readiness check 'typo' failed
TypeError: object NoneType can't be used in 'await' expression
failing readiness: ['typo']
commit raised: TypeError TypoConfig.stage() takes 2 positional arguments but 4 were given
```

The misspelt erasure hook is never called, silently: `privacy erase`
reports success and the copies stay. The others start, and fail every
readiness probe and every commit, in production.

What the broadcast costs, in Python calls, with the 12 contrib apps and
10 domains of a project that have no hooks (22 domains):

| | 0.11.1 | 0.12 |
| --- | --- | --- |
| An empty commit | 350 | 350 |
| A round of `/ready` checks of the domains | 1,736 | 4 |
| The domains' lifespan (start, then stop) | 2,484 | 315 |

A commit costs the same: 0.10 already called only the domains that
registered. A readiness round started a task and a timer for each of the
22 domains, to run 21 empty methods; it runs none now.

## Decision

- Each hook method `x` of `DomainConfig` is now the point `on_x`:
  `domainkit.apps.hooks` declares `on_startup`, `on_shutdown`, `on_check`,
  `on_seed`, `on_erase_subject`, `on_export_subject`,
  `on_check_application`, and `on_deployment_warnings`, beside the SQL
  points of `domainkit.db.hooks`. A domain registers a function in
  `ready()`; only domains that registered are called. They live in the
  core, not in `contrib.privacy`, as 0085 suggested for erasure: messaging,
  webhooks, and idempotency keep copies whether privacy is installed or
  not, and must not import it.
- A point checks a function when it registers, against how it calls its
  hooks: `HookPoint(name, arguments=n, awaited=True)` binds `n` positional
  arguments to the function's signature, and refuses a plain function
  where it awaits (or the reverse). A mistake fails `setup()`, naming the
  domain, the point, and the function. A misspelt point is an
  `ImportError`. A function without a signature (a built-in) is trusted.
  A point made without `arguments` or `awaited` checks nothing, as before.
- A domain's several hooks of one kind keep its one identity: its startup
  hooks run in turn, and its shutdown hooks run, in reverse domain order,
  once all of them ran; its readiness checks run in turn under its label;
  its erasures are summed into one audit record, and its exports merged
  under its label.
- Points belong to the registry that loads the domains: a
  `DomainRegistry` of one's own keeps its registrations
  (`point.hooks(registry)`, `point.of(label, registry)`).
- The methods keep working in 0.12: an override is called through its
  point, after the domain's registered hooks, as in 0085. Loading a domain
  that overrides one warns `DomainkitDeprecationWarning`, naming the
  domain, the class, and the point to use, at the override's own line,
  so the default filter shows it once and a project's
  `filterwarnings` for its own modules catches it. The base methods are
  marked `@warnings.deprecated`, which type checkers report. 0.13 removes
  them; it keeps refusing a configuration that still defines one of
  their names (an `ImproperlyConfigured` naming the point), since a
  method no one calls would otherwise be silently dead.
- `databases(router)` and `holds_table(table)` stay methods. They are
  not things a domain joins but answers about it, which the framework
  needs from every domain, with a default. As a point, each domain would
  need one registration at most (a rule a method never needs) and a
  default for the rest: a PoC of `on_databases` made `migration_targets()`
  cost 763 calls instead of 685, and every domain was still asked. Nor
  are they broadcast for nothing: their default answer is the one most
  domains need. `ready()`, `settings_class`, `requires`, and `RETENTION`
  stay too: they describe the domain.
- The contrib apps, the generated templates, the docs, and the tutorial
  register; nothing in the framework overrides a deprecated method.

## Consequences

- A hook with the wrong arguments, or not async where it must be, is
  found when the domains load, not in the first commit, probe, or erasure
  that calls it. A project's type checker sees each point's signature
  (`HookPoint[Callable[[str, str], Awaitable[Mapping[str, int]]]]`).
- `DomainConfig` is a description of the domain and one method,
  `ready()`, that wires it; reading a domain's `ready()` lists everything
  it takes part in.
- A readiness round, and the application's lifespan, cost nothing for
  domains that have no hook.
- Projects move each override to a registration before 0.13:
  `docs/upgrading.md` lists them. A method that is not a plain function
  (a callable object) is warned of at the top of its module.
- The duck-typed protocols some contrib apps answer (`QueueDeclarer`,
  `OperatorRecorder`, `DenialRecorder`, `TokenIssuer`) are looked up on
  configurations with `isinstance`, not inherited or broadcast; they are
  left as they are.

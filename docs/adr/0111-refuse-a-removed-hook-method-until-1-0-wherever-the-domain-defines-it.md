# 0111. Refuse a removed hook method until 1.0, wherever the domain defines it

- Status: Accepted
- Date: 2026-10-29
- Amends: [0101](0101-register-every-domain-hook-and-check-it-when-it-registers.md)

## Context

0.12 deprecated overriding the hook methods of `DomainConfig` (`startup`,
`shutdown`, `check`, `seed`, `erase_subject`, `export_subject`,
`check_application`, `deployment_warnings`, `stage`, `committed`,
`stage_transaction`, `transaction_committed`, `migrated`): an override was
still called, after the domain's registered hooks, and warned of
([0101](0101-register-every-domain-hook-and-check-it-when-it-registers.md)).
0.13 removes them. A domain that overrides `erase_subject`, on 0.12.0 and
with the methods simply deleted:

```text
0.12.0:  warned DomainkitDeprecationWarning: Domain 'oldstyle': OldStyleConfig.erase_subject
         is a deprecated hook method; register the function with
         domainkit.apps.hooks.on_erase_subject(...) in ready(), ...
         erase hooks returned [{'copies': 1}], called with ['subject-1']
deleted: erase hooks returned [], called with []
```

Deleted, the method is a name no one calls: `privacy erase` reports
success and the copies stay, as with 0.12's SEC-12a-1. 0101 and
`versioning.md` promised that 0.13 refuses such a configuration instead.
Three choices were left open: when to look, where, and for how long.

**When.** A check in `DomainConfig.__init_subclass__` would fail at
import, before `setup()`, but sees only the class as written. A check in
`setup()` sees the domain as loaded. Each way a project could define a
name, tried both ways:

| The name is | At class creation | In `setup()` |
| --- | --- | --- |
| a method in the class body | found | found |
| a method of a base that is no `DomainConfig` (a mixin) | found | found |
| a property | found | found |
| a `staticmethod` | found | found |
| a callable object | found | found |
| set on the class after it is made (`Config.erase_subject = f`) | missed | found |
| set on the instance (`self.shutdown = ...` in `__init__`) | missed | found |

**Where.** `hasattr(config, name)` would run a property, and finds names
on nothing that a lookup on the instance would not. Looking in
`vars(config)`, then in `vars(cls)` for each class of the MRO, finds
exactly what `config.name` would, without calling anything, and gives the
class to name in the error.

**How long.** The check costs 6.5 µs a domain at `setup()` (17 µs for a
domain with eight bases), 150 µs for the 23 domains of the generated
project, once per process. Dropping it in 0.14 would bring the silent
skip back for a project that upgrades from 0.12 straight to 0.14, which
the upgrade guide advises against but cannot prevent. Keeping it keeps
the 13 names off `DomainConfig` subclasses: a domain cannot have its own
method called `check` or `stage`. Neither costs much; the names' old
meaning is the one a reader expects.

## Decision

- `setup()` (`DomainRegistry.populate`) refuses a domain that defines any
  of the 13 names, anywhere a lookup on its configuration would find
  it: the instance, then its class and every base, whatever the value
  (method, property, `staticmethod`, a callable object, a function set
  later). It raises `CheckFailed` (an `ImproperlyConfigured`) after the
  settings and `requires` checks and before any domain's `ready()`
  runs, with one check `apps.E002` per name, in `INSTALLED_DOMAINS`
  order: the domain's label, the class that defines it, and, as the hint,
  the point to register on, under a name of its own.
- The refusal stays until 1.0. `REMOVED_HOOKS` in `domainkit.apps.config`
  keeps the names and their points; 1.0 decides whether to drop it.
- The methods, their `@warnings.deprecated` markers, the load-time
  warning, `HookPoint(method=)`, and the path that called an override
  are removed. A point calls what domains registered, nothing else.

## Consequences

- A domain that still overrides a hook method cannot start, in any
  environment, and the error says what to write instead. Nothing is
  silently skipped, whether the name was written in the class or set on
  it later.
- 13 names stay unavailable to domain configurations until 1.0.
- A `__getattr__` on a configuration class is not consulted: it would
  have to be called to know, and 0.12 did not call one either.
- `HookPoint` loses `method=`, which only named one of these methods;
  a project's own point made with it fails when it is made (`TypeError`),
  and `docs/upgrading.md` says to drop the argument.

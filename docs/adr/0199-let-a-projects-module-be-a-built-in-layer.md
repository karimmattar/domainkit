# 0199. Let a project's module be a built-in layer

- Status: Accepted
- Date: 2026-11-23
- Amends: [0003](0003-enforced-layer-rules.md)

## Context

In the simple layout, four modules are the layers: `domain.py`,
`controllers.py`, `models.py`, and `routes.py` (`LAYER_MODULES`). A
project keeps its calls to providers in `gateways.py`: it calls a
service through `domainkit.outbound`, takes uploads through
`domainkit.http.uploads`, and refuses a file with
`domainkit.http.problems`, as an infrastructure layer does (user report
D2).

`gateways.py` is no layer, so no rule holds it. Made a layer of its own
(`EXTRA_LAYERS`), it takes none of the infrastructure layer's
exceptions: nothing extends `allowed_modules`, so a library rule on
`domainkit.http` forbids all three modules there, the client's 2.8 path
and its 2.9 home with them (`MOVED`). The warning of the names 3.0
refuses (`architecture.W001`, ADR 0188) reads only a layer that both
forbids a library and allows a module of it, so it never reaches the
module either. The project keeps a layer that may not import
`routes.py`, and no library rule.

Options weighed:

1. **Let `ARCHITECTURE` add exceptions** (`ALLOWED_MODULES` for an
   extra layer). The settings would remove rules, which they never do
   (ADR 0003's amendment of 2026-10-25). The project would copy the
   infrastructure layer's tables, and the copy would drift at the next
   release, as the client's move in 2.9 shows (`MOVED`, ADR 0195).
2. **An extra layer that copies a built-in one** (`EXTRA_LAYERS` as a
   mapping, `{"gateways": {"like": "infrastructure"}}`). `EXTRA_LAYERS`
   changes type, and the layer is still another layer: `FORBIDDEN_LAYERS`
   of the built-in layers does not name it, so the application layer may
   import it unless the project says so.
3. **Name the module as a built-in layer**
   (`ARCHITECTURE["LAYER_MODULES"]`, `{"gateways": "infrastructure"}`).
   The module is that layer, as `models.py` is: every rule of the layer,
   today's and those a release adds, holds it, with no copy. Import
   checkers name layers the same way: import-linter's layers contract
   lets one layer hold several sibling modules, listed on its line
   ([import-linter: layers](https://import-linter.readthedocs.io/en/stable/contract_types/layers/)).

## Decision

- **`ARCHITECTURE["LAYER_MODULES"]`** maps a module's name to one of the
  built-in layers: `domain`, `application`, `infrastructure`, or `api`.
  A domain's module or package of that name, at its top, is that layer,
  for every rule:
  - what it imports: the layer's libraries, their exceptions
    (`ALLOWED_MODULES`, with the client's 2.9 home and the `MOVED`
    reading of a project's rule on a 2.8 path), the `__all__` of
    `domainkit.http.problems`, and the warning of what 3.0 refuses
    (`architecture.W001`);
  - the layers that may not import it, and those it may not import
    (`FORBIDDEN_LAYERS`, through the layer's names, `_names_of`);
  - for the domain layer, its own layer: `domain.py` and a module named
    as the domain layer import each other.
- **Validation** (`ArchitectureConfig`), when the settings load:
  - a layer that is not built in is refused, an extra layer too: a
    module of an extra layer's name is that layer already;
  - a built-in layer or module (`models`, `api`) is refused: it keeps
    its layer;
  - a module in `EXTRA_LAYERS` too is refused. It would be two layers,
    each with rules of its own; taking one of them silently would drop
    the other's rules without a word.
  - Up to 64 modules, each a name, as `EXTRA_LAYERS` takes.
- **Other domains** import a named module only where `PUBLIC_SURFACE`
  names it, which may now start with one. A module named as the
  application layer is not opened on its own: the setting adds rules,
  and opening a module to every domain is the project's to write.
- **The table of the simple layout's modules moves** to
  `domainkit.conf.global_settings`, which validates against it without
  importing the checker (audit A28-ARCH-04, as `LAYERS` did).
  `domainkit.architecture.LAYER_MODULES` is the same object.
- `LayerRules` holds the modules (`layer_modules`); `extend(layer_modules=...)`
  adds to them, and a module named there already keeps its layer.

## Consequences

- A project's `gateways.py` is held as `models.py` is, and `check`
  reports it so: errors for a forbidden library, `architecture.W001` for
  Starlette's `UploadFile` from `domainkit.http.uploads`. A project that
  made it an extra layer moves it from `EXTRA_LAYERS` to
  `LAYER_MODULES`; the two together are refused. Its `FORBIDDEN_LAYERS`
  and `FORBIDDEN_LIBRARIES` entries name the module's layer from then
  on: a rule keyed by the module is refused, and the message names the
  layer to key it by.
- A module inside a layer package (`infrastructure/gateways.py`) is that
  package's layer already; `LAYER_MODULES` names only a domain's top.
- To watch: a module named as a layer in one domain is that layer in
  every domain; a domain whose module of that name is something else
  needs it renamed.

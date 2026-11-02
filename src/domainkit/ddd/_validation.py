"""One validation path: objects check their fields' markers themselves.

The metaclasses in ``_dataclass`` call in here. An entity or aggregate
checks a field whenever it is set, which covers its ``__init__`` and every
later change; a frozen class (value objects, commands, events) checks its
fields once, after ``__init__``. ``clean()``, for rules across fields, runs
after an entity's ``__init__`` and, for each aggregate a unit of work saw,
before it commits.

Each class gets code made for its own checks, on first use, when its hints
resolve: a class without markers keeps ``object.__setattr__`` and its
dataclass ``__init__``, and pays nothing. An ORM loading a row sets
``__dict__`` directly, so stored data is not checked again.
"""

import dataclasses
import inspect
import sys
from collections.abc import Callable, Mapping
from types import NoneType, UnionType
from typing import Annotated, Any, Final, Union, get_args, get_origin, get_type_hints

from domainkit.ddd.exceptions import InvalidField
from domainkit.ddd.markers import MARKERS

_CHECKS: Final = "__domainkit_checks__"
"""Where a class keeps its checks, by field name, once worked out."""

type Checks = Mapping[str, tuple[Any, ...]]


def checks_of(cls: type) -> Checks:
    """The markers each of ``cls``'s fields carries, by field name.

    Worked out on first use, when every hint can be resolved, and kept on
    the class. A hint that cannot be resolved yet carries none.
    """
    found: Checks | None = cls.__dict__.get(_CHECKS)
    if found is None:
        found = {
            name: markers
            for name, hint in _hints(cls).items()
            if (markers := _markers(hint))
        }
        setattr(cls, _CHECKS, found)
    return found


def setter_for(owner: type) -> Callable[[Any, str, Any], None]:
    """A ``__setattr__`` for ``owner`` that checks fields' markers before setting them.

    ``object.__setattr__`` itself when no field of ``owner`` has markers.
    """
    checks = checks_of(owner)
    set_ = object.__setattr__
    if not checks:
        return set_

    def __setattr__(self: Any, name: str, value: Any) -> None:  # noqa: ANN401 - any field.
        markers = checks.get(name)
        if markers is not None and value is not None:
            for marker in markers:
                if (problem := marker.problem(value)) is not None:
                    raise InvalidField(f"{name}: {problem}", field=name)
        set_(self, name, value)

    return __setattr__


def checking(own: Callable[[Any, str, Any], None]) -> Callable[[Any, str, Any], None]:
    """``own``, a class's own ``__setattr__``, run once the value given passes.

    The value is checked against the markers of the instance's class before
    ``own`` sees it.
    """

    def __setattr__(self: Any, name: str, value: Any) -> None:  # noqa: ANN401 - any field.
        markers = checks_of(type(self)).get(name)
        if markers is not None and value is not None:
            _refuse(name, value, markers)
        own(self, name, value)

    return __setattr__


def check_values(instance: object, checks: Checks) -> None:
    """Check each field of ``instance`` named in ``checks``.

    Raises:
        InvalidField: A marker rules a field's value out.
    """
    for name, markers in checks.items():
        value = getattr(instance, name)
        if value is not None:
            for marker in markers:
                if (problem := marker.problem(value)) is not None:
                    raise InvalidField(f"{name}: {problem}", field=name)


def _refuse(name: str, value: object, markers: tuple[Any, ...]) -> None:
    """Raise for the first marker that rules ``value`` out.

    Raises:
        InvalidField: One does.
    """
    for marker in markers:
        problem = marker.problem(value)
        if problem is not None:
            raise InvalidField(f"{name}: {problem}", field=name)


def _hints(cls: type) -> dict[str, Any]:
    """The dataclass fields' hints, with their ``Annotated`` metadata.

    A hint that names what only type checkers import is left out, and the
    others kept.
    """
    try:
        hints = get_type_hints(cls, include_extras=True)
    except (NameError, TypeError):  # A forward reference not defined yet.
        hints = {}
        for klass in reversed(cls.__mro__):
            space = vars(sys.modules[klass.__module__])
            for name, hint in _own_annotations(klass).items():
                try:
                    hints[name] = _evaluated(hint, space, dict(vars(klass)))
                except (NameError, TypeError, SyntaxError):
                    hints.pop(name, None)
    names = {field.name for field in dataclasses.fields(cls)}
    return {name: hint for name, hint in hints.items() if name in names}


def _own_annotations(klass: type) -> dict[str, Any]:
    """The annotations ``klass`` itself declares, unresolved names left as they are.

    Python 3.14 makes them on first use (PEP 649) rather than keeping them
    in the class's ``__dict__``; ``annotationlib``'s forward references
    stand for the names it cannot resolve.
    """
    if sys.version_info >= (3, 14):
        import annotationlib

        return annotationlib.get_annotations(
            klass, format=annotationlib.Format.FORWARDREF
        )
    return inspect.get_annotations(klass)


def _evaluated(hint: object, space: dict[str, Any], local: dict[str, Any]) -> object:
    """``hint``, evaluated in its class's module if it is a string.

    Raises:
        NameError: It names what the module does not define, as a name only
            type checkers import.
    """
    if not isinstance(hint, str):
        return hint
    return eval(hint, space, local)  # noqa: S307 - the class's own annotation.


def _markers(hint: object) -> tuple[Any, ...]:
    """The markers ``hint`` carries, on itself or on the non-None part of a union."""
    origin = get_origin(hint)
    if origin is Annotated:
        return tuple(each for each in get_args(hint)[1:] if isinstance(each, MARKERS))
    if origin in (Union, UnionType):
        parts = [each for each in get_args(hint) if each is not NoneType]
        if len(parts) == 1:
            return _markers(parts[0])
    return ()

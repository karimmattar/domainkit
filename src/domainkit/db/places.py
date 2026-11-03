"""``Point`` columns: PostGIS's ``geography`` on PostgreSQL, JSON elsewhere.

On PostgreSQL a ``Point`` field is ``geography(Point, 4326)``: PostGIS
must be installed (``CREATE EXTENSION postgis``, which a migration can
run), and a spatial index (``Index(..., postgresql_using="gist")``)
makes ``within`` fast. Values are written as EWKT through
``ST_GeogFromText`` and read through ``ST_AsText``, so no driver needs a
codec of its own. Other databases store ``[longitude, latitude]`` as
JSON text (GeoJSON's order), and ``within`` works out the great-circle
distance in SQL (SQLite's math functions).
"""

import json
import re
from typing import Any, Final

from sqlalchemy import String, func, literal
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator, TypeEngine, UserDefinedType

from domainkit.ddd.places import EARTH_RADIUS, Point

__all__ = ["Geography", "PointType", "within"]

_WKT: Final = re.compile(r"POINT\(\s*(\S+)\s+(\S+)\s*\)")


class Geography(UserDefinedType[Any]):
    """PostGIS's ``geography(Point, 4326)``."""

    cache_ok = True

    def get_col_spec(self, **options: Any) -> str:  # noqa: ANN401 - SQLAlchemy's.
        """The column's type in DDL."""
        return "geography(Point, 4326)"


class PointType(TypeDecorator[Point]):
    """A ``Point``: geography on PostgreSQL, ``[lon, lat]`` JSON text elsewhere."""

    impl = String(64)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """``geography`` on PostgreSQL; text elsewhere."""
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Geography())
        return dialect.type_descriptor(String(64))

    def bind_expression(self, bindvalue: Any) -> Any:  # noqa: ANN401 - SQLAlchemy's.
        """PostgreSQL reads the EWKT text through ``ST_GeogFromText``."""
        return _ByDialect(bindvalue)

    def column_expression(self, column: Any) -> Any:  # noqa: ANN401 - SQLAlchemy's.
        """PostgreSQL answers ``ST_AsText``; others the stored text."""
        return _ReadByDialect(column)

    def process_bind_param(self, value: Point | None, dialect: Dialect) -> str | None:
        """EWKT for PostgreSQL; ``[lon, lat]`` elsewhere."""
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value.wkt()
        return json.dumps([value.longitude, value.latitude])

    def process_result_value(self, value: str | None, dialect: Dialect) -> Point | None:
        """A ``Point`` from ``POINT(lon lat)`` or ``[lon, lat]``."""
        if value is None:
            return None
        if match := _WKT.search(value):
            return Point(latitude=float(match[2]), longitude=float(match[1]))
        longitude, latitude = json.loads(value)
        return Point(latitude=float(latitude), longitude=float(longitude))


class _ByDialect(ColumnElement[Any]):
    """A bound point: ``ST_GeogFromText(:value)`` on PostgreSQL, else the value."""

    inherit_cache = True

    def __init__(self, bindvalue: Any) -> None:  # noqa: ANN401
        """Wrap ``bindvalue``."""
        self.bindvalue = bindvalue
        self.type = bindvalue.type


class _ReadByDialect(ColumnElement[Any]):
    """A read point: ``ST_AsText(column)`` on PostgreSQL, else the column."""

    inherit_cache = True

    def __init__(self, column: Any) -> None:  # noqa: ANN401
        """Wrap ``column``."""
        self.column = column
        self.type = column.type


def _compiles() -> None:
    """Teach the compiler the two wrappers (once, on import)."""
    from sqlalchemy.ext.compiler import compiles

    @compiles(_ByDialect)
    def _bind(element: _ByDialect, compiler: Any, **kw: Any) -> str:  # noqa: ANN401
        inner = compiler.process(element.bindvalue, **kw)
        if compiler.dialect.name == "postgresql":
            return f"ST_GeogFromText({inner})"
        return str(inner)

    @compiles(_ReadByDialect)
    def _read(element: _ReadByDialect, compiler: Any, **kw: Any) -> str:  # noqa: ANN401
        inner = compiler.process(element.column, **kw)
        if compiler.dialect.name == "postgresql":
            return f"ST_AsText({inner})"
        return str(inner)


_compiles()


def within(
    column: Any,  # noqa: ANN401 - a column of any table.
    point: Point,
    meters: float,
    dialect: str,
) -> ColumnElement[bool]:
    """Whether ``column`` is within ``meters`` of ``point``, in ``dialect``'s SQL."""
    if dialect == "postgresql":
        return func.ST_DWithin(
            column, func.ST_GeogFromText(literal(point.wkt(), String())), meters
        )
    lon = func.json_extract(column, "$[0]")
    lat = func.json_extract(column, "$[1]")
    lat1, lat2 = func.radians(literal(point.latitude)), func.radians(lat)
    dlat = lat2 - lat1
    dlon = func.radians(lon) - func.radians(literal(point.longitude))
    h = func.pow(func.sin(dlat / 2), 2) + func.cos(lat1) * func.cos(lat2) * func.pow(
        func.sin(dlon / 2), 2
    )
    distance = 2 * EARTH_RADIUS * func.asin(func.min(1.0, func.sqrt(h)))
    return distance <= meters

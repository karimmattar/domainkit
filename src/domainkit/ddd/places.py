"""Places on the Earth: a point, and how far two are apart.

``Point`` is a value object, so an aggregate holds one like any other
field; a repository finds those near a place::

    class Shop(AggregateRoot[ShopId]):
        location: Point

    near = await uow.shops.filter(F("location").within(Point(48.8566, 2.3522), 2_000))

On PostgreSQL the column is PostGIS's ``geography(Point, 4326)`` and
``within`` is ``ST_DWithin``, which uses the column's spatial index; on
SQLite it is the great-circle distance worked out in SQL, and in memory
in Python (``distance_to``).
"""

import math
from typing import Final

from domainkit.ddd.model import ValueObject

__all__ = ["EARTH_RADIUS", "Point"]

EARTH_RADIUS: Final = 6_371_008.8
"""The Earth's mean radius, in meters (IUGG): what distances are worked out on."""


class Point(ValueObject):
    """A place: latitude and longitude in degrees (WGS 84, as GPS gives them)."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        """Refuse a latitude or longitude off the Earth.

        Raises:
            ValueError: The latitude is outside -90 to 90, or the longitude
                outside -180 to 180, or either is not a number.
        """
        if not (
            math.isfinite(self.latitude)
            and math.isfinite(self.longitude)
            and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180
        ):
            raise ValueError(
                "a point is a latitude of -90 to 90, a longitude of -180 to 180"
            )

    def distance_to(self, other: "Point") -> float:
        """Meters along the Earth's surface to ``other`` (haversine)."""
        lat1, lat2 = math.radians(self.latitude), math.radians(other.latitude)
        dlat = lat2 - lat1
        dlon = math.radians(other.longitude - self.longitude)
        h = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return 2 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(h)))

    def wkt(self) -> str:
        """The point as PostGIS reads it: ``SRID=4326;POINT(lon lat)``."""
        return f"SRID=4326;POINT({self.longitude!r} {self.latitude!r})"

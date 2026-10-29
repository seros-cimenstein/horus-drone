"""Latitude/longitude to local NED metres and back, flat-earth about an origin (the vehicle's).

The same approximation the flight link uses for GLOBAL_POSITION_INT before LOCAL_POSITION_NED:
111,320 m per degree of latitude, and per degree of longitude scaled by cos(origin latitude).
Within a few kilometres of the origin it is good to well under a metre.
"""

from __future__ import annotations

import math

METRES_PER_DEGREE = 111_320.0


def ned_from_latlon(latitude_deg: float, longitude_deg: float, origin: tuple[float, float]) -> tuple[float, float]:
    origin_lat, origin_lon = origin
    return (
        (latitude_deg - origin_lat) * METRES_PER_DEGREE,
        (longitude_deg - origin_lon) * METRES_PER_DEGREE * math.cos(math.radians(origin_lat)),
    )


def latlon_from_ned(north_m: float, east_m: float, origin: tuple[float, float]) -> tuple[float, float]:
    origin_lat, origin_lon = origin
    return (
        origin_lat + north_m / METRES_PER_DEGREE,
        origin_lon + east_m / (METRES_PER_DEGREE * math.cos(math.radians(origin_lat))),
    )

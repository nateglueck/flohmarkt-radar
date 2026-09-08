from __future__ import annotations

import csv
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

EARTH_RADIUS_KM = 6371.0088

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "AT.txt"


@dataclass(frozen=True)
class PlzEntry:
    plz: str
    place: str
    state: str
    lat: float
    lon: float


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


class PlzIndex:
    """Offline Austrian postal code index built from the GeoNames AT dataset."""

    def __init__(self, path: Path = DATA_FILE) -> None:
        self.entries: list[PlzEntry] = []
        self._centroids: dict[str, tuple[float, float]] = {}
        self._load(path)

    def _load(self, path: Path) -> None:
        grouped: dict[str, list[tuple[float, float]]] = {}
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh, delimiter="\t"):
                if len(row) < 11 or not row[9] or not row[10]:
                    continue
                lat, lon = float(row[9]), float(row[10])
                self.entries.append(PlzEntry(row[1], row[2], row[3], lat, lon))
                grouped.setdefault(row[1], []).append((lat, lon))

        for plz, points in grouped.items():
            self._centroids[plz] = (
                sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points),
            )

    def centroid(self, plz: str) -> tuple[float, float] | None:
        return self._centroids.get(plz.strip())

    def nearest(self, lat: float, lon: float) -> PlzEntry | None:
        if not self.entries:
            return None
        return min(self.entries, key=lambda e: haversine_km(lat, lon, e.lat, e.lon))

    def plz_within(self, lat: float, lon: float, radius_km: float) -> list[str]:
        found = {
            plz
            for plz, (plat, plon) in self._centroids.items()
            if haversine_km(lat, lon, plat, plon) <= radius_km
        }
        return sorted(found)

    def search_prefixes(self, lat: float, lon: float, radius_km: float) -> list[str]:
        """Compact PLZ filter for flohmarkt.at.

        The site matches on PLZ prefixes, so nearby codes collapse into a few
        3-digit prefixes. A buffer covers events whose postal area centroid sits
        outside the radius while the venue itself is inside it.
        """
        buffered = radius_km + 15.0
        codes = self.plz_within(lat, lon, buffered)
        if not codes:
            nearest = self.nearest(lat, lon)
            codes = [nearest.plz] if nearest else []

        prefixes = sorted({c[:3] for c in codes})
        # Collapse to 2-digit prefixes when a region is broadly covered anyway.
        by_two: dict[str, set[str]] = {}
        for p in prefixes:
            by_two.setdefault(p[:2], set()).add(p)
        collapsed = {two if len(subs) >= 7 else None for two, subs in by_two.items()}
        result = {two for two in collapsed if two}
        result |= {p for p in prefixes if p[:2] not in result}
        return sorted(result)

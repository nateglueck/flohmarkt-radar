from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class Market(BaseModel):
    id: str
    title: str
    date_from: date | None = None
    date_to: date | None = None
    date_raw: str = ""
    time_raw: str = ""
    plz: str = ""
    city: str = ""
    address: str = ""
    organizer: str | None = None
    description: str | None = None
    phone: str | None = None
    email: str | None = None
    homepage: str | None = None
    image_url: str | None = None
    lat: float | None = None
    lon: float | None = None
    geo_precision: str | None = None
    distance_km: float | None = None


class SearchResult(BaseModel):
    origin_lat: float
    origin_lon: float
    radius_km: float
    date_from: date
    date_to: date
    total_scraped: int
    markets: list[Market]

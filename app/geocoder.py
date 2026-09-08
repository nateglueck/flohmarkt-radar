from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from .cache import Cache

PHOTON_URL = "https://photon.komoot.io/api/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "flohmarkt-radar/0.1 (local hobby app)"

# Austria bounding box, used to reject nonsense geocoder hits.
AT_BOUNDS = (46.3, 9.4, 49.1, 17.2)

_NOISE_RE = re.compile(
    r"\s*(?:,\s*)?\b(?:auf\s+(?:dem\s+)?parkdeck|parkdeck|parkplatz|eingang|zufahrt|"
    r"kontakt\b.*|anmeldung\b.*|siehe\b.*|neben\s+dem|gegen[uü]ber\s+(?:dem|von)|"
    r"vor\s+dem|beim\s+eingang)\b.*$",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\([^)]*\)")
_DISTRICT_RE = re.compile(r"^\s*\d{2}\.\s*,?\s*")


@dataclass
class GeoResult:
    lat: float | None
    lon: float | None
    precision: str
    source: str


def _in_austria(lat: float, lon: float) -> bool:
    return AT_BOUNDS[0] <= lat <= AT_BOUNDS[2] and AT_BOUNDS[1] <= lon <= AT_BOUNDS[3]


def normalize_address(address: str, plz: str, city: str) -> str:
    """Build a stable geocoding key: cleaned street part + PLZ + city."""
    street = _PAREN_RE.sub(" ", address or "")
    street = _DISTRICT_RE.sub("", street)
    cleaned = _NOISE_RE.sub("", street)
    if len(cleaned.strip(" -,.")) < 3:
        cleaned = street
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,.")
    parts = [p for p in (cleaned, plz.strip(), city.strip()) if p]
    return ", ".join(parts)


_STREET_NUM_RE = re.compile(r"^\s*([^\d,]{3,}?\s\d+[a-zA-Z]?)\b")
_PREP_RE = re.compile(
    r"\s+\b(?:beim|bei|vor|neben|hinter|gegen[uü]ber|n[aä]he|nahe|im|in|am\s+ende)\b\s+.*$",
    re.IGNORECASE,
)


def _street_candidates(query: str) -> list[str]:
    """Query variants, from full string down to single street segments."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) < 3:
        return [query]

    tail = parts[-2:]  # PLZ, city
    streets = parts[:-2]
    variants = [query]
    for segment in (streets[0], streets[-1]):
        variants.append(", ".join([segment, *tail]))
    match = _STREET_NUM_RE.match(streets[0])
    if match:
        variants.append(", ".join([match.group(1).strip(), *tail]))
    trimmed = _PREP_RE.sub("", streets[0]).strip(" -,.")
    if len(trimmed) >= 3:
        variants.append(", ".join([trimmed, *tail]))

    expanded: list[str] = []
    for variant in variants:
        expanded.extend(query_variants(variant))
    return list(dict.fromkeys(expanded))[:6]


def _has_street(query: str) -> bool:
    return bool(re.search(r"[A-Za-zÄÖÜäöüß]{3,}", query.split(",")[0]))


_QUERY_PLZ_RE = re.compile(r",\s*(\d{4})\s*,")


def _plz_matches(query: str, found_postcode: str | None) -> bool:
    """Reject geocoder hits that land in a clearly different postal region."""
    expected = _QUERY_PLZ_RE.search(query)
    if not expected or not found_postcode:
        return True
    found = re.sub(r"\D", "", str(found_postcode))[:4]
    if len(found) != 4:
        return True
    return found[:2] == expected.group(1)[:2]


class Geocoder:
    """Street-level geocoding with a permanent SQLite cache.

    Photon is used first (fast, tolerant of bulk use); Nominatim is the fallback
    and is throttled to one request per second per its usage policy.
    """

    def __init__(self, cache: Cache, *, requests_per_second: float = 5.0) -> None:
        self.cache = cache
        self._interval = 1.0 / requests_per_second
        self._last_call = 0.0
        self._throttle = asyncio.Lock()
        self._nominatim_last = 0.0
        self._nominatim_lock = asyncio.Lock()

    async def _wait_turn(self) -> None:
        async with self._throttle:
            loop = asyncio.get_running_loop()
            delay = self._interval - (loop.time() - self._last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = loop.time()

    async def _photon(self, client: httpx.AsyncClient, query: str) -> GeoResult | None:
        await self._wait_turn()
        r = await client.get(
            PHOTON_URL,
            params={"q": query, "limit": 1, "lang": "de", "bbox": "9.4,46.3,17.2,49.1"},
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        features = r.json().get("features") or []
        if not features:
            return None
        lon, lat = features[0]["geometry"]["coordinates"]
        if not _in_austria(lat, lon):
            return None
        props = features[0].get("properties", {})
        if props.get("countrycode") not in (None, "AT"):
            return None
        if not _plz_matches(query, props.get("postcode")):
            return None
        precision = "street" if props.get("street") or props.get("housenumber") else "place"
        return GeoResult(lat, lon, precision, "photon")

    async def _nominatim(self, client: httpx.AsyncClient, query: str) -> GeoResult | None:
        async with self._nominatim_lock:
            loop = asyncio.get_running_loop()
            delay = 1.0 - (loop.time() - self._nominatim_last)
            if delay > 0:
                await asyncio.sleep(delay)
            r = await client.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "at"},
                headers={"User-Agent": USER_AGENT},
            )
            self._nominatim_last = loop.time()
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
        if not _in_austria(lat, lon):
            return None
        return GeoResult(lat, lon, "street", "nominatim")

    def lookup_cached(self, query: str) -> GeoResult | None:
        row = self.cache.get_geocode(query)
        if row is None:
            return None
        return GeoResult(row["lat"], row["lon"], row["precision"], row["source"])

    def lookup_cached_many(self, queries: list[str]) -> dict[str, GeoResult]:
        rows = self.cache.get_geocodes(queries)
        return {
            q: GeoResult(r["lat"], r["lon"], r["precision"], r["source"]) for q, r in rows.items()
        }

    async def resolve(self, client: httpx.AsyncClient, query: str) -> GeoResult:
        cached = self.lookup_cached(query)
        if cached is not None:
            return cached

        result: GeoResult | None = None
        for candidate in _street_candidates(query):
            if not _has_street(candidate):
                continue
            try:
                result = await self._photon(client, candidate)
            except (httpx.HTTPError, ValueError, KeyError):
                result = None
            if result is None:
                try:
                    result = await self._nominatim(client, candidate)
                except (httpx.HTTPError, ValueError, KeyError):
                    result = None
            if result is not None:
                break

        final = result or GeoResult(None, None, "unresolved", "none")
        self.cache.put_geocode(query, final.lat, final.lon, final.precision, final.source)
        return final

    async def resolve_many(
        self,
        client: httpx.AsyncClient,
        queries: list[str],
        *,
        concurrency: int = 4,
    ) -> dict[str, GeoResult]:
        pending = [q for q in dict.fromkeys(queries) if self.lookup_cached(q) is None]
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(q: str) -> tuple[str, GeoResult]:
            async with semaphore:
                return q, await self.resolve(client, q)

        results = await asyncio.gather(*(worker(q) for q in pending), return_exceptions=True)
        out: dict[str, GeoResult] = {}
        for item in results:
            if isinstance(item, tuple):
                out[item[0]] = item[1]
        return out


_STREET_SUFFIX_RE = re.compile(r"\b([A-Za-zÄÖÜäöüß]{3,}?)(?:er)?(str\.|strasse|straße|str)\b", re.IGNORECASE)
_SS_RE = re.compile(r"\b(str)asse\b", re.IGNORECASE)


def query_variants(text: str) -> list[str]:
    """German street spellings differ wildly; try the common rewrites.

    Photon indexes "Nußdorfer Straße" as two words, so a user typing
    "Nussdorferstrasse 73" only matches after the compound is split.
    """
    text = re.sub(r"\s+", " ", text).strip()
    variants = [text]

    sharp_s = _SS_RE.sub(r"\1aße", text)
    variants.append(sharp_s)

    def split_compound(match: re.Match[str]) -> str:
        stem = match.group(1)
        joiner = "er " if match.group(0)[len(stem) :].lower().startswith("er") else " "
        return f"{stem}{joiner}Straße"

    for base in (text, sharp_s):
        split = _STREET_SUFFIX_RE.sub(split_compound, base)
        variants.append(split)

    return [v for v in dict.fromkeys(variants) if v]


def _label(props: dict) -> str:
    street = " ".join(str(p) for p in (props.get("street"), props.get("housenumber")) if p)
    name = props.get("name")
    place = " ".join(
        str(p) for p in (props.get("postcode"), props.get("city") or props.get("county")) if p
    )
    parts = [p for p in (name, street or None, place or None) if p]
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return ", ".join(deduped)


async def suggest(
    client: httpx.AsyncClient, text: str, *, limit: int = 6
) -> list[dict[str, object]]:
    """Autocomplete candidates for the address box, Austria only."""

    async def photon_variant(variant: str) -> list[dict]:
        try:
            r = await client.get(
                PHOTON_URL,
                params={"q": variant, "limit": limit, "lang": "de", "bbox": "9.4,46.3,17.2,49.1"},
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
            return r.json().get("features") or []
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    variants = query_variants(text)
    batches = await asyncio.gather(*(photon_variant(v) for v in variants))

    out: list[dict[str, object]] = []
    seen_labels: set[str] = set()

    for features in batches:
        for feature in features:
            props = feature.get("properties", {})
            if props.get("countrycode") != "AT":
                continue
            label = _label(props)
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            lon, lat = feature["geometry"]["coordinates"]
            out.append({"label": label, "lat": lat, "lon": lon})
            if len(out) >= limit:
                return out

    if out:
        return out

    # Nominatim understands German compounds better; used only as a last resort.
    try:
        r = await client.get(
            NOMINATIM_URL,
            params={
                "q": text,
                "format": "jsonv2",
                "limit": limit,
                "countrycodes": "at",
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        for item in r.json():
            parts = str(item.get("display_name", "")).split(", ")
            label = ", ".join(parts[:3] + parts[-2:-1]) if len(parts) > 4 else item["display_name"]
            if label in seen_labels:
                continue
            seen_labels.add(label)
            out.append({"label": label, "lat": float(item["lat"]), "lon": float(item["lon"])})
    except (httpx.HTTPError, ValueError, KeyError):
        pass

    return out


async def geocode_free_text(client: httpx.AsyncClient, text: str) -> GeoResult | None:
    """Geocode a user-entered place, used by the 'search by address' box."""
    hits = await suggest(client, text, limit=1)
    if not hits:
        return None
    top = hits[0]
    return GeoResult(float(top["lat"]), float(top["lon"]), str(top["label"]), "photon")


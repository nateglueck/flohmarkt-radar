from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cache import Cache
from .geo import PlzIndex, haversine_km
from .geocoder import Geocoder, geocode_free_text, normalize_address, suggest
from .models import Market, SearchResult
from .scraper import CATEGORIES, USER_AGENT, scrape

STATIC_DIR = Path(__file__).resolve().parent.parent / "site"
SCRAPE_TTL_S = 6 * 3600
INLINE_GEOCODE_BUDGET_S = 12.0
# PLZ centroids (especially in Vienna) can sit several km off the real venue,
# so approximate hits get a buffer until street geocoding refines them.
APPROX_BUFFER_KM = 12.0

state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = Cache()
    state["cache"] = cache
    state["plz"] = PlzIndex()
    state["geocoder"] = Geocoder(cache)
    state["client"] = httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    state["pending_geocodes"] = 0
    state["background"] = set()
    yield
    for task in list(state["background"]):
        task.cancel()
    await state["client"].aclose()


app = FastAPI(title="Flohmarkt Radar", lifespan=lifespan)


def _cache_key(plz: str, date_from: date, date_to: date, category: int | None, q: str) -> str:
    raw = f"{plz}|{date_from}|{date_to}|{category or ''}|{q}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _load_markets(
    *,
    plz_filter: str,
    date_from: date,
    date_to: date,
    category: int | None,
    query: str,
    refresh: bool,
) -> tuple[list[Market], float]:
    cache: Cache = state["cache"]
    key = _cache_key(plz_filter, date_from, date_to, category, query)

    if not refresh:
        hit = cache.get_scrape(key, SCRAPE_TTL_S)
        if hit:
            payload, fetched_at = hit
            return [Market.model_validate(item) for item in payload], fetched_at

    markets = await scrape(
        state["client"],
        plz=plz_filter,
        date_from=date_from,
        date_to=date_to,
        category=category,
        query=query,
    )
    fetched_at = cache.put_scrape(key, [m.model_dump(mode="json") for m in markets])
    return markets, fetched_at


def _apply_coords(markets: list[Market], resolved: dict[str, object]) -> list[str]:
    """Attach best known coordinates; return address keys still lacking a hit."""
    plz_index: PlzIndex = state["plz"]
    unresolved: list[str] = []

    for market in markets:
        key = normalize_address(market.address, market.plz, market.city)
        hit = resolved.get(key)
        if hit is not None and getattr(hit, "lat", None) is not None:
            market.lat, market.lon = hit.lat, hit.lon
            market.geo_precision = hit.precision
            continue

        centroid = plz_index.centroid(market.plz)
        if centroid:
            market.lat, market.lon = centroid
            market.geo_precision = "plz"
        if hit is None:
            unresolved.append(key)
    return unresolved


async def _geocode_background(keys: list[str]) -> None:
    geocoder: Geocoder = state["geocoder"]
    try:
        await geocoder.resolve_many(state["client"], keys)
    finally:
        state["pending_geocodes"] = max(0, int(state["pending_geocodes"]) - len(keys))


@app.get("/api/categories")
async def categories() -> dict[str, str]:
    return {str(k): v for k, v in CATEGORIES.items()}


@app.get("/api/stats")
async def stats() -> dict[str, object]:
    cache: Cache = state["cache"]
    return {**cache.stats(), "pending_geocodes": int(state["pending_geocodes"])}


@app.get("/api/suggest")
async def suggest_addresses(
    q: str = Query(min_length=2),
    limit: int = Query(default=6, ge=1, le=10),
) -> list[dict[str, object]]:
    return await suggest(state["client"], q, limit=limit)


@app.get("/api/geocode")
async def geocode(q: str = Query(min_length=2)) -> dict[str, object]:
    result = await geocode_free_text(state["client"], q)
    if result is None or result.lat is None:
        raise HTTPException(status_code=404, detail="Adresse nicht gefunden")
    return {"lat": result.lat, "lon": result.lon, "label": result.precision}


@app.get("/api/search", response_model=SearchResult)
async def search(
    lat: float = Query(ge=45.0, le=50.0),
    lon: float = Query(ge=8.0, le=18.0),
    radius_km: float = Query(default=25.0, gt=0, le=300),
    days: int = Query(default=30, ge=1, le=400),
    category: int | None = Query(default=None, ge=1, le=9),
    q: str = "",
    refresh: bool = False,
) -> SearchResult:
    plz_index: PlzIndex = state["plz"]
    geocoder: Geocoder = state["geocoder"]

    date_from = date.today()
    date_to = date_from + timedelta(days=days)
    prefixes = plz_index.search_prefixes(lat, lon, radius_km)

    markets, _ = await _load_markets(
        plz_filter=",".join(prefixes),
        date_from=date_from,
        date_to=date_to,
        category=category,
        query=q,
        refresh=refresh,
    )

    keys = [normalize_address(m.address, m.plz, m.city) for m in markets]
    resolved = geocoder.lookup_cached_many(keys)
    missing = [k for k in dict.fromkeys(keys) if k not in resolved]

    if missing:
        deadline = time.monotonic() + INLINE_GEOCODE_BUDGET_S
        inline: list[str] = []
        for key in missing:
            if time.monotonic() > deadline:
                break
            inline.append(key)
            if len(inline) >= 60:
                break
        try:
            fresh = await asyncio.wait_for(
                geocoder.resolve_many(state["client"], inline),
                timeout=INLINE_GEOCODE_BUDGET_S,
            )
            resolved.update(fresh)
        except asyncio.TimeoutError:
            resolved.update(geocoder.lookup_cached_many(inline))

        leftover = [k for k in missing if k not in resolved]
        if leftover:
            state["pending_geocodes"] = int(state["pending_geocodes"]) + len(leftover)
            task = asyncio.create_task(_geocode_background(leftover))
            state["background"].add(task)
            task.add_done_callback(state["background"].discard)

    _apply_coords(markets, resolved)

    within: list[Market] = []
    for market in markets:
        if market.lat is None or market.lon is None:
            continue
        distance = haversine_km(lat, lon, market.lat, market.lon)
        limit = radius_km + (APPROX_BUFFER_KM if market.geo_precision == "plz" else 0.0)
        if distance <= limit:
            market.distance_km = round(distance, 1)
            within.append(market)

    today = date.today()
    within.sort(key=lambda m: (max(m.date_from or date.max, today), m.distance_km or 0.0))

    return SearchResult(
        origin_lat=lat,
        origin_lon=lon,
        radius_km=radius_km,
        date_from=date_from,
        date_to=date_to,
        total_scraped=len(markets),
        markets=within,
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Mounted last so the /api routes above win; html=True serves index.html at /.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")

"""Build the static dataset consumed by the published site.

Scrapes every upcoming Austrian listing once, resolves each distinct address
through the permanent geocode cache and writes plain JSON into ``site/data``.
The published frontend then does radius filtering entirely in the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .cache import Cache
from .geo import PlzIndex
from .geocoder import Geocoder, normalize_address
from .models import Market
from .scraper import CATEGORIES, USER_AGENT, scrape

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "site" / "data"
GEOCACHE_FILE = ROOT / "data" / "geocache.json"

DEFAULT_DAYS = 180
# Radius padding applied by the frontend to events that only have a postal-code
# centroid, mirroring the accuracy of the GeoNames data.
APPROX_BUFFER_KM = 12.0

# Grid used to shard events for the client; roughly 28 x 37 km at Austrian
# latitudes, small enough that a short-radius search fetches one or two cells.
CELL_LAT_DEG = 0.25
CELL_LON_DEG = 0.5


async def scrape_everything(
    client: httpx.AsyncClient, *, days: int, cache: Cache, max_age_s: float
) -> tuple[list[Market], dict[str, int]]:
    """All upcoming events plus a per-event category, one pass per category.

    Each pass is cached so repeated builds (and reruns of a failed CI job) do
    not replay ~1000 requests against flohmarkt.at.
    """
    today = date.today()
    until = today + timedelta(days=days)

    async def pass_for(category: int | None) -> list[Market]:
        key = f"build|{today}|{until}|{category or 'all'}"
        hit = cache.get_scrape(key, max_age_s)
        if hit:
            return [Market.model_validate(item) for item in hit[0]]
        batch = await scrape(
            client, plz="", date_from=today, date_to=until, category=category, max_pages=400
        )
        cache.put_scrape(key, [m.model_dump(mode="json") for m in batch])
        return batch

    markets = await pass_for(None)
    print(f"  all categories: {len(markets)} events")

    categories: dict[str, int] = {}
    for cat_id, name in CATEGORIES.items():
        batch = await pass_for(cat_id)
        for market in batch:
            categories.setdefault(market.id, cat_id)
        print(f"  [{cat_id}] {name}: {len(batch)}")

    return markets, categories


async def geocode_all(
    client: httpx.AsyncClient, geocoder: Geocoder, markets: list[Market]
) -> dict[str, object]:
    keys = list(dict.fromkeys(normalize_address(m.address, m.plz, m.city) for m in markets))
    known = geocoder.lookup_cached_many(keys)
    missing = [k for k in keys if k not in known]
    print(f"  {len(keys)} distinct addresses, {len(missing)} need geocoding")

    if missing:
        started = time.monotonic()
        await geocoder.resolve_many(client, missing, concurrency=1)
        print(f"  geocoded in {time.monotonic() - started:.0f}s")

    return geocoder.lookup_cached_many(keys)


def attach_coords(markets: list[Market], resolved: dict[str, object], plz_index: PlzIndex) -> None:
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


def serialize(market: Market, category: int | None) -> dict[str, object]:
    record = market.model_dump(mode="json", exclude_none=True)
    record.pop("distance_km", None)
    for axis in ("lat", "lon"):
        if axis in record:
            record[axis] = round(record[axis], 6)
    if category:
        record["category"] = category
    return record


def write_shards(records: list[dict[str, object]], out_dir: Path) -> list[dict[str, object]]:
    """Split events into a geographic grid, each cell tagged with its bounding box.

    The browser downloads only the cells whose box intersects the search circle.
    Postal-code shards were tried first but their bounding boxes overlap heavily
    (a Vienna search dragged in all of Lower Austria), so a plain lat/lon grid
    keeps a typical 7 km lookup an order of magnitude smaller.
    """
    shard_dir = out_dir / "markets"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for stale in shard_dir.glob("*.json"):
        stale.unlink()

    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        lat, lon = float(record["lat"]), float(record["lon"])
        cell = f"{int(lat / CELL_LAT_DEG)}_{int(lon / CELL_LON_DEG)}"
        grouped.setdefault(cell, []).append(record)

    shards: list[dict[str, object]] = []
    for name, group in sorted(grouped.items()):
        lats = [float(r["lat"]) for r in group]
        lons = [float(r["lon"]) for r in group]
        (shard_dir / f"{name}.json").write_text(
            json.dumps(group, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        shards.append(
            {
                "id": name,
                "count": len(group),
                "bbox": [min(lats), min(lons), max(lats), max(lons)],
            }
        )
    return shards


async def build(
    days: int, out_dir: Path, *, nominatim_interval_s: float, scrape_max_age_s: float
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = Cache()
    restored = cache.import_geocodes(GEOCACHE_FILE)
    print(f"Restored {restored} cached geocodes from {GEOCACHE_FILE.name}")
    plz_index = PlzIndex()
    geocoder = Geocoder(
        cache, requests_per_second=1.0, nominatim_interval_s=nominatim_interval_s
    )

    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        print("Scraping flohmarkt.at …")
        markets, categories = await scrape_everything(
            client, days=days, cache=cache, max_age_s=scrape_max_age_s
        )

        print("Geocoding …")
        resolved = await geocode_all(client, geocoder, markets)

    print(f"Exported {cache.export_geocodes(GEOCACHE_FILE)} geocodes")

    attach_coords(markets, resolved, plz_index)
    located = [m for m in markets if m.lat is not None]

    records = [serialize(m, categories.get(m.id)) for m in located]
    records.sort(key=lambda r: (r.get("date_from") or "9999-12-31", r.get("plz", "")))

    precision: dict[str, int] = {}
    for market in located:
        key = market.geo_precision or "none"
        precision[key] = precision.get(key, 0) + 1

    shards = write_shards(records, out_dir)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date_from": date.today().isoformat(),
        "date_to": (date.today() + timedelta(days=days)).isoformat(),
        "days": days,
        "total_scraped": len(markets),
        "total_located": len(located),
        "precision": precision,
        "approx_buffer_km": APPROX_BUFFER_KM,
        "categories": {str(k): v for k, v in CATEGORIES.items()},
        "shards": shards,
        "source": "https://www.flohmarkt.at/termine/",
    }
    address_meta = out_dir / "address_meta.json"
    if address_meta.exists():
        meta["address_index"] = json.loads(address_meta.read_text(encoding="utf-8"))
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_kb = sum(p.stat().st_size for p in (out_dir / "markets").glob("*.json")) / 1024
    print(f"Wrote {len(records)} events across {len(shards)} shards ({total_kb:.0f} KB total)")
    print(f"Precision: {precision}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="lookahead window")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    parser.add_argument(
        "--nominatim-interval",
        type=float,
        default=1.0,
        help="seconds between Nominatim calls; use 15 for unattended batch runs",
    )
    parser.add_argument(
        "--scrape-max-age",
        type=float,
        default=0.0,
        help="reuse cached scrape passes younger than this many seconds",
    )
    args = parser.parse_args()
    asyncio.run(
        build(
            args.days,
            args.out,
            nominatim_interval_s=args.nominatim_interval,
            scrape_max_age_s=args.scrape_max_age,
        )
    )


if __name__ == "__main__":
    main()

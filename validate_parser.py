"""Ad-hoc validation of the flohmarkt.at parser against live pages."""

import asyncio
from datetime import date, timedelta

import httpx

from app.geo import PlzIndex, haversine_km
from app.scraper import USER_AGENT, scrape

VIENNA = (48.2082, 16.3738)


async def main() -> None:
    index = PlzIndex()
    print(f"PLZ entries: {len(index.entries)}, unique codes: {len(index._centroids)}")
    prefixes = index.search_prefixes(*VIENNA, 25)
    print(f"prefixes for 25km around Vienna ({len(prefixes)}): {','.join(prefixes)}")

    today = date.today()
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        markets = await scrape(
            client,
            plz=",".join(prefixes),
            date_from=today,
            date_to=today + timedelta(days=60),
        )

    print(f"scraped: {len(markets)}")
    missing_dates = [m for m in markets if not m.date_from]
    print(f"rows without parsed date: {len(missing_dates)}")
    for m in missing_dates[:5]:
        print("   RAW:", repr(m.date_raw))

    geo_hits = 0
    for m in markets:
        c = index.centroid(m.plz)
        if c:
            geo_hits += 1
            m.lat, m.lon = c
            m.distance_km = round(haversine_km(*VIENNA, *c), 1)
    print(f"geocoded by PLZ centroid: {geo_hits}/{len(markets)}")

    within = sorted(
        (m for m in markets if m.distance_km is not None and m.distance_km <= 25),
        key=lambda m: (m.date_from or date.max, m.distance_km),
    )
    print(f"within 25km: {len(within)}\n")
    for m in within[:8]:
        print(f"{m.date_from} | {m.distance_km:5.1f}km | {m.plz} {m.city} | {m.title}")
        print(f"      addr={m.address!r} time={m.time_raw!r} org={m.organizer!r}")
        print(f"      tel={m.phone!r} mail={m.email!r} web={m.homepage!r} img={bool(m.image_url)}")
        print(f"      desc={(m.description or '')[:90]!r}\n")

    with_desc = sum(1 for m in markets if m.description)
    print(f"with description: {with_desc}/{len(markets)}")


asyncio.run(main())

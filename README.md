# Flohmarkt Radar

Local single-page app that finds flea markets within a radius around you, using
[flohmarkt.at](https://www.flohmarkt.at/termine/) as the data source.

## Run

There is **one process**. The FastAPI backend also serves the frontend as static files,
so there is no separate frontend build or dev server.

```powershell
cd C:\Users\ngl\IdeaProjects\flohmarkt-radar
.\run.ps1                 # creates .venv, installs deps, starts server, opens the browser
.\run.ps1 -Port 9000      # different port
```

Open <http://127.0.0.1:8077/>. Stop with `Ctrl+C`.

### Manual start

```powershell
cd C:\Users\ngl\IdeaProjects\flohmarkt-radar
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

`--index-url https://pypi.org/simple` is needed because the machine's default pip index
is a private Azure Artifacts feed that prompts for credentials.

Add `--reload` while developing the backend. Frontend changes in `static/` need only a
browser hard-refresh (`Ctrl+F5`), no restart.

Then hit **📍 Mein Standort**, type an address, or right-click the map to set the origin.
Defaults: 7 km radius, next 7 days, category "Flohmarkt".

## Why a backend is needed

flohmarkt.at sends no CORS headers, so the browser cannot fetch it directly. The
FastAPI backend scrapes, geocodes and does the radius maths; the frontend is plain
HTML/JS with Leaflet.

## How it works

1. **PLZ prefilter** — flohmarkt.at has no radius search, only a PLZ-prefix filter.
   `app/geo.py` uses the offline GeoNames dataset (`data/AT.txt`, 19k rows) to find all
   Austrian postal codes within `radius + 15 km` and collapses them into a handful of
   2–3 digit prefixes.
2. **Scrape** — `app/scraper.py` calls
   `GET /termine/suche.php?kat=&bundesland=&plz=<prefixes>&zeitraum=<from>_bis_<to>&start=<n>`,
   25 rows per page. Plain server-rendered HTML, no JS, no auth, no cookies. Each row
   yields date, time, title, PLZ, city and street; organizer, description, phone, email,
   homepage and photo are inlined in the same page, so no detail requests are needed.
3. **Geocode** — `app/geocoder.py` resolves the street address via Photon (OSM), with
   Nominatim as fallback (throttled to 1 req/s per its policy). Results are cached
   permanently in SQLite, so only new events cost a request. Hits outside Austria or in a
   clearly different postal region are rejected; the PLZ centroid is the fallback.
4. **Radius** — haversine filter against the origin. Events still on a PLZ centroid
   (`geo_precision = "plz"`) get a 12 km buffer and are marked `~` in the UI, because
   GeoNames maps all Vienna districts to the city centre. Background refinement corrects
   them and the frontend reloads automatically.

## Caching

- Scrape results: SQLite, 6 h TTL. **↻ Neu laden** bypasses it.
- Geocoding: SQLite, permanent (addresses don't move).
- First search for a region takes ~15 s; repeats are ~20 ms.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/search?lat&lon&radius_km&days&category&q&refresh` | markets within radius, sorted by date then distance |
| `GET /api/suggest?q&limit` | address autocomplete (Austria only) |
| `GET /api/geocode?q=` | free-text place lookup for the search box |
| `GET /api/categories` | category id → label |
| `GET /api/stats` | cache sizes and pending background geocodes |

## Address autocomplete

The address box suggests completions after 3 characters (250 ms debounce, arrow keys +
Enter to pick). German street spellings are the hard part: Photon indexes
"Nußdorfer Straße" as two words, so `Nussdorferstrasse 73` matches nothing verbatim.
`query_variants()` therefore fires several rewrites in parallel — `ss` → `ß` and
compound splitting (`Nussdorferstrasse` → `Nussdorfer Straße`) — merges the hits, drops
non-Austrian and duplicate results, and only falls back to Nominatim if everything is
empty. Typical response: 45–400 ms. The same rewrites are applied when geocoding scraped
event addresses.

## Measured behaviour

- Vienna, 10 km, 21 days: 235 events scraped, 158 within radius, 132 street-level,
  16 place-level, 10 PLZ-only.
- Graz, 30 km, 45 days: 148 scraped, 91 within radius.

## Notes on being a good citizen

- `robots.txt` allows the search pages (only individual detail pages are disallowed).
- Requests are sequential per search, identified by a descriptive `User-Agent`, and
  caching keeps repeat load close to zero.
- Data belongs to flohmarkt.at and the event organizers — this is a personal local tool,
  not a republishing service.

## Layout

```
app/geo.py        offline PLZ index, haversine, prefix selection
app/scraper.py    flohmarkt.at fetching + HTML parsing
app/geocoder.py   Photon/Nominatim geocoding, address normalization
app/cache.py      SQLite cache
app/main.py       FastAPI endpoints
static/           SPA (Leaflet map + result list)
data/AT.txt       GeoNames AT postal codes (CC BY 4.0)
```

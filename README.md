# Flohmarkt Radar

Finds flea markets within a radius around you, using
[flohmarkt.at](https://www.flohmarkt.at/termine/) as the data source.

**Live: <https://nateglueck.github.io/flohmarkt-radar/>**

## How it is put together

flohmarkt.at has no radius search and sends no CORS headers, so a browser cannot query
it directly. Instead a scheduled GitHub Actions job does all the expensive work once a
day and commits nothing but a small cache; the published site is pure static files.

```
GitHub Actions (daily)                     GitHub Pages (static)
---------------------                      ---------------------
scrape flohmarkt.at      --+
geocode new addresses      +-> site/data/  --> browser: haversine filter,
shard events by grid cell --+                  map, list, autocomplete
```

Because the browser does the radius maths itself, changing the radius or the date range
is instant and costs no requests.

## Run locally

```powershell
cd C:\Users\ngl\IdeaProjects\flohmarkt-radar
.\run.ps1                 # creates .venv, installs deps, starts server, opens the browser
.\run.ps1 -Port 9000      # different port
```

That serves `site/` at <http://127.0.0.1:8077/>, exactly as GitHub Pages will. Stop with
`Ctrl+C`. Frontend changes need only a hard refresh (`Ctrl+F5`).

`site/data/` is generated, not committed, so on a fresh clone build it first:

```powershell
.\.venv\Scripts\python.exe -m app.build --days 180
```

### Manual setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

`--index-url https://pypi.org/simple` is needed because this machine's default pip index
is a private Azure Artifacts feed that prompts for credentials.

## Building the dataset

```powershell
# Full run: scrape + geocode + write site/data/
.\.venv\Scripts\python.exe -m app.build --days 180

# Reuse scrape passes from the last 24 h (fast iteration, no load on the source)
.\.venv\Scripts\python.exe -m app.build --days 180 --scrape-max-age 86400
```

`app/build.py`:

1. **Scrape** - one pass over all of Austria, then one pass per category to tag each
   event. Roughly 1000 requests, ~7 minutes, sequential.
2. **Geocode** - resolves each distinct street address via Photon (OSM), with Nominatim
   as fallback. Only *new* addresses cost a request: 2574 events currently collapse to
   621 distinct addresses.
3. **Shard** - events are written into a 0.25 deg x 0.5 deg grid under
   `site/data/markets/`, each cell tagged with its bounding box in `meta.json`. The
   browser fetches only the cells intersecting the search circle: a 7 km Vienna search
   pulls ~130 KB gzip instead of the full 650 KB.

### The geocode cache is the whole trick

Geocoding is the only slow, rate-limited part, and addresses never move. The results are
committed to git as **`data/geocache.json`** (sorted JSON, git-friendly), restored at the
start of every build and written back at the end. `data/cache.sqlite` is a local working
file and stays untracked - it holds scraped HTML and would bloat the repo.

The daily CI run therefore geocodes only the handful of genuinely new addresses.

## Address autocomplete

Two tiers, in order:

1. **Offline street index** - `site/data/streets/<letter>.json`, built from the Austrian
   address register (BEV, CC BY 4.0) by `app/build_address_index.py`. Sharded by the
   first letter of the normalised street name, so typing "Nussdorfer" downloads only the
   `n` shard. Normalisation strips case, umlauts, `ss`/`sz` and spaces, which is what
   makes `Nussdorferstrasse` match `Nussdorfer Strasse` without any query rewriting.
2. **Photon** - queried only when the index yields fewer than three hits, or before the
   index exists at all. Photon sends `Access-Control-Allow-Origin: *`, so the browser
   calls it directly; no backend and no API key.

The index is rebuilt twice a year by the `Refresh address index` workflow, because BEV
republishes on 1 April and 1 October. Until it has run once, tier 2 handles everything.

> `data.bev.gv.at` is unreachable from the current corporate network, so that workflow
> can only be run on GitHub, not locally.

## Deployment

| Workflow | Trigger | Does |
| --- | --- | --- |
| `.github/workflows/publish.yml` | daily 03:17 UTC, push to `main`, manual | build dataset, commit `data/geocache.json`, deploy `site/` to Pages |
| `.github/workflows/address-index.yml` | 5 April / 5 October, manual | rebuild the BEV street index and commit it |

One-time setup in the repository: **Settings -> Pages -> Source: GitHub Actions**.
Actions minutes are free only on public repositories.

Scheduled workflows are disabled after 60 days without repository activity, but the daily
geocode-cache commit counts as activity, so the schedule sustains itself.

## Layout

```
app/build.py               builds site/data/ (scrape + geocode + shard)
app/build_address_index.py builds site/data/streets/ from the BEV address register
app/scraper.py             flohmarkt.at fetching + HTML parsing
app/geocoder.py            Photon/Nominatim geocoding, address normalisation
app/geo.py                 offline PLZ index, haversine
app/cache.py               SQLite cache + geocache.json import/export
app/main.py                local dev server (serves site/, plus legacy /api routes)
site/                      everything that gets published
data/AT.txt                GeoNames AT postal codes (CC BY 4.0)
data/geocache.json         committed geocode results
```

## Data sources and attribution

- Event data: [flohmarkt.at](https://www.flohmarkt.at/termine/). `robots.txt` allows the
  search pages; requests are sequential and carry a descriptive `User-Agent`. Every event
  links back to the source.
- Geocoding and map tiles: OpenStreetMap contributors, ODbL.
- Addresses: (c) Oesterreichisches Adressregister, CC BY 4.0 - the attribution line in
  the page footer is a licence condition, not decoration.

## Measured behaviour

- 2574 events Austria-wide over 180 days, 621 distinct addresses.
- Geocoding precision: 2225 street-level, 248 place-level, 96 PLZ-only. Events still on a
  PLZ centroid get a 12 km buffer and are marked `~`, because GeoNames maps every Vienna
  district to the city centre.
- Vienna, 7 km, 7 days, category Flohmarkt: 4 grid cells (131 KB gzip), 756 candidates,
  28 hits.
- Cold bootstrap of the geocode cache: 408 addresses in 7.5 minutes.

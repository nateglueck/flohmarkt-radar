"""Build the offline street index used by the address autocomplete.

Source is the Austrian address register (Adressregister) published by BEV under
CC BY 4.0. Coordinates are stored in three Gauss-Krueger strips, so every row is
reprojected to WGS84 before street-level centroids are averaged.

The result is sharded by the first letter of the normalised street name, which
matches how the browser looks entries up: a user typing "Nussdorfer" only
downloads the "n" shard.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date
from itertools import chain
from pathlib import Path
from typing import Iterator

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "site" / "data" / "streets"
META_FILE = ROOT / "site" / "data" / "address_meta.json"
DOWNLOAD_DIR = ROOT / "data"

SOURCE_URL = (
    "https://data.bev.gv.at/download/Adressregister/"
    "Adresse_Relationale_Tabellen_Stichtagsdaten.zip"
)
ATTRIBUTION = "© Österreichisches Adressregister, Stichtagsdaten vom {stichtag}"

_UMLAUTS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "s"})


_CITY_SUFFIX_RE = re.compile(r"\s*,.*$")


def clean_city(name: str) -> str:
    """BEV stores districts inline, e.g. "Wien,Alsergrund" or "Graz,06.Bez.:Jakomini".

    The postal code already disambiguates, so keep only the municipality.
    """
    return _CITY_SUFFIX_RE.sub("", name).strip()


def normalize(text: str) -> str:
    """Must mirror normalizeStreet() in site/app.js."""
    lowered = text.lower().replace("ß", "ss")
    return re.sub(r"[^a-z0-9]", "", lowered.translate(_UMLAUTS))


def download(url: str, target: Path) -> Path:
    if target.exists():
        print(f"Using cached {target.name} ({target.stat().st_size / 1e6:.0f} MB)")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} …")
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_bytes(1 << 20):
                fh.write(chunk)
    print(f"  {target.stat().st_size / 1e6:.0f} MB")
    return target


def _member(archive: zipfile.ZipFile, name: str) -> str:
    """Locate a CSV inside the archive regardless of casing or folder nesting."""
    wanted = name.lower()
    for info in archive.namelist():
        if Path(info).name.lower() == wanted:
            return info
    raise SystemExit(f"{name} not found in archive; members: {archive.namelist()[:20]}")


def read_csv(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with archive.open(_member(archive, name)) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter=";")


def pick(fieldnames: list[str], *candidates: str) -> str:
    lookup = {f.lower(): f for f in fieldnames}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    raise SystemExit(f"none of {candidates} in columns {fieldnames}")


def load_lookup(archive: zipfile.ZipFile, name: str, key: str, value: str) -> dict[str, str]:
    rows = read_csv(archive, name)
    first = next(rows, None)
    if first is None:
        return {}
    fields = list(first.keys())
    key_col, value_col = pick(fields, key), pick(fields, value)
    out = {first[key_col]: first[value_col]}
    for row in rows:
        out[row[key_col]] = row[value_col]
    return out


def build(archive_path: Path, out_dir: Path, stichtag: str) -> None:
    from pyproj import Transformer

    transformers: dict[str, Transformer] = {}

    def to_wgs84(easting: float, northing: float, epsg: str) -> tuple[float, float]:
        if epsg not in transformers:
            transformers[epsg] = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        lon, lat = transformers[epsg].transform(easting, northing)
        return lat, lon

    with zipfile.ZipFile(archive_path) as archive:
        print("Reading STRASSE …")
        streets = load_lookup(archive, "STRASSE.csv", "SKZ", "STRASSENNAME")
        print(f"  {len(streets)} streets")

        print("Reading GEMEINDE …")
        municipalities = load_lookup(archive, "GEMEINDE.csv", "GKZ", "GEMEINDENAME")
        try:
            places = load_lookup(archive, "ORTSCHAFT.csv", "OKZ", "ORTSNAME")
        except SystemExit:
            places = {}

        print("Reading ADRESSE …")
        rows = read_csv(archive, "ADRESSE.csv")
        first = next(rows, None)
        if first is None:
            raise SystemExit("ADRESSE.csv is empty")
        fields = list(first.keys())
        col = {
            "skz": pick(fields, "SKZ"),
            "plz": pick(fields, "PLZ"),
            "gkz": pick(fields, "GKZ"),
            "rw": pick(fields, "RW"),
            "hw": pick(fields, "HW"),
            "epsg": pick(fields, "EPSG"),
        }
        try:
            col["okz"] = pick(fields, "OKZ")
        except SystemExit:
            col["okz"] = ""

        sums: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        names: dict[tuple[str, str], str] = {}
        total = skipped = 0

        # chain, not list: ADRESSE.csv has ~2.5M rows and materialising them
        # would cost several GB.
        for row in chain([first], rows):
            total += 1
            skz, plz = row[col["skz"]], (row[col["plz"]] or "").strip()
            street = streets.get(skz)
            if not street or not plz.isdigit():
                skipped += 1
                continue
            try:
                lat, lon = to_wgs84(float(row[col["rw"]]), float(row[col["hw"]]), row[col["epsg"]])
            except (ValueError, TypeError):
                skipped += 1
                continue

            key = (skz, plz)
            bucket = sums[key]
            bucket[0] += lat
            bucket[1] += lon
            bucket[2] += 1
            if key not in names:
                city = places.get(row[col["okz"]]) if col["okz"] else None
                names[key] = clean_city(city or municipalities.get(row[col["gkz"]], ""))

        print(f"  {total} address rows, {skipped} skipped, {len(sums)} street/PLZ pairs")

    shards: dict[str, list[list[object]]] = defaultdict(list)
    for (skz, plz), (lat_sum, lon_sum, count) in sums.items():
        street = streets[skz]
        letter = (normalize(street) or "_")[0]
        shards[letter].append(
            [street, plz, names[(skz, plz)], round(lat_sum / count, 6), round(lon_sum / count, 6)]
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        stale.unlink()

    total_bytes = 0
    for letter, entries in sorted(shards.items()):
        entries.sort(key=lambda e: (normalize(str(e[0])), str(e[1])))
        path = out_dir / f"{letter}.json"
        path.write_text(
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        total_bytes += path.stat().st_size

    META_FILE.write_text(
        json.dumps(
            {
                "stichtag": stichtag,
                "source": SOURCE_URL,
                "licence": "CC BY 4.0",
                "attribution": ATTRIBUTION.format(stichtag=stichtag),
                "entries": sum(len(v) for v in shards.values()),
                "shards": sorted(shards),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(shards)} shards, {total_bytes / 1e6:.1f} MB raw")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=SOURCE_URL)
    parser.add_argument("--zip", type=Path, default=DOWNLOAD_DIR / "adressregister.zip")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--stichtag", default=date.today().isoformat())
    args = parser.parse_args()

    archive = args.zip if args.zip.exists() else download(args.url, args.zip)
    build(archive, args.out, args.stichtag)
    print("Done. Remember: the attribution line is a licence condition.", file=sys.stderr)


if __name__ == "__main__":
    main()

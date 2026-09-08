from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cache.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_cache (
    address   TEXT PRIMARY KEY,
    lat       REAL,
    lon       REAL,
    precision TEXT NOT NULL,
    source    TEXT NOT NULL,
    cached_at REAL NOT NULL
);
"""


class Cache:
    """Small thread-safe SQLite store for scraped pages and geocoding results."""

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get_scrape(self, key: str, max_age_s: float) -> tuple[list[Any], float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM scrape_cache WHERE key = ?", (key,)
            ).fetchone()
        if not row or time.time() - row["fetched_at"] > max_age_s:
            return None
        return json.loads(row["payload"]), row["fetched_at"]

    def put_scrape(self, key: str, payload: list[Any]) -> float:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scrape_cache (key, payload, fetched_at) VALUES (?, ?, ?)",
                (key, json.dumps(payload, ensure_ascii=False, default=str), now),
            )
            self._conn.commit()
        return now

    def get_geocode(self, address: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT lat, lon, precision, source FROM geocode_cache WHERE address = ?",
                (address,),
            ).fetchone()

    def get_geocodes(self, addresses: list[str]) -> dict[str, sqlite3.Row]:
        if not addresses:
            return {}
        out: dict[str, sqlite3.Row] = {}
        with self._lock:
            for chunk_start in range(0, len(addresses), 400):
                chunk = addresses[chunk_start : chunk_start + 400]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT address, lat, lon, precision, source FROM geocode_cache "
                    f"WHERE address IN ({placeholders})",
                    chunk,
                ).fetchall()
                out.update({r["address"]: r for r in rows})
        return out

    def put_geocode(
        self,
        address: str,
        lat: float | None,
        lon: float | None,
        precision: str,
        source: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO geocode_cache "
                "(address, lat, lon, precision, source, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
                (address, lat, lon, precision, source, time.time()),
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            scrapes = self._conn.execute("SELECT COUNT(*) c FROM scrape_cache").fetchone()["c"]
            geo = self._conn.execute("SELECT COUNT(*) c FROM geocode_cache").fetchone()["c"]
            resolved = self._conn.execute(
                "SELECT COUNT(*) c FROM geocode_cache WHERE lat IS NOT NULL"
            ).fetchone()["c"]
        return {"scrape_entries": scrapes, "geocode_entries": geo, "geocode_resolved": resolved}

    def clear_scrapes(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scrape_cache")
            self._conn.commit()

    def export_geocodes(self, path: Path) -> int:
        """Dump the geocode cache to sorted JSON so git can version it cheaply.

        The SQLite file itself is a poor fit for version control: it is mostly
        scraped HTML payloads and binary deltas do not compress across commits.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, lat, lon, precision, source FROM geocode_cache ORDER BY address"
            ).fetchall()
        payload = {
            r["address"]: [r["lat"], r["lon"], r["precision"], r["source"]] for r in rows
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )
        return len(payload)

    def import_geocodes(self, path: Path) -> int:
        if not path.exists():
            return 0
        payload: dict[str, list[Any]] = json.loads(path.read_text(encoding="utf-8"))
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO geocode_cache "
                "(address, lat, lon, precision, source, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(addr, *values, now) for addr, values in payload.items()],
            )
            self._conn.commit()
        return len(payload)

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from datetime import date

import httpx
from bs4 import BeautifulSoup, Tag

from .models import Market

BASE_URL = "https://www.flohmarkt.at/termine/suche.php"
USER_AGENT = "flohmarkt-radar/1.0 (+https://github.com/nateglueck/flohmarkt-radar)"
PAGE_SIZE = 25

CATEGORIES: dict[int, str] = {
    1: "Flohmarkt",
    2: "Benefiz-, Vereins- & Pfarrflohmarkt",
    3: "Kinderflohmarkt & Kindersachen-Börse",
    4: "Hausflohmarkt & Wohnungsflohmarkt",
    5: "Sammlerbörse",
    6: "Oldtimerveranstaltung",
    7: "Kunst & Antiquitätenmesse",
    8: "Auktionen",
    9: "Ausstellung",
}

_ROW_ID_RE = re.compile(r"info_(\d+)_icon")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})(?:\.(\d{4}))?")
_PIC_RE = re.compile(r"pic_open\('([^']+)'")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _parse_dates(raw: str, fallback_year: int) -> tuple[date | None, date | None]:
    matches = _DATE_RE.findall(raw)
    if not matches:
        return None, None

    parsed: list[date] = []
    year_hint = next((int(m[2]) for m in reversed(matches) if m[2]), fallback_year)
    for day, month, year in matches:
        try:
            parsed.append(date(int(year) if year else year_hint, int(month), int(day)))
        except ValueError:
            continue

    if not parsed:
        return None, None
    return parsed[0], parsed[-1]


def _detail_fields(soup: BeautifulSoup, row_id: str) -> dict[str, str | None]:
    block = soup.find("div", id=f"in{row_id}fohidden")
    out: dict[str, str | None] = {
        "organizer": None,
        "description": None,
        "phone": None,
        "email": None,
        "homepage": None,
    }
    if not isinstance(block, Tag):
        return out

    html = block.decode_contents()
    parts = re.split(r"<b>(.*?)</b>", html)
    labels = {
        "Veranstalter:": "organizer",
        "Beschreibung:": "description",
        "Telefon:": "phone",
        "Homepage:": "homepage",
        "Email-Adresse:": "email",
    }
    for label, value in zip(parts[1::2], parts[2::2]):
        key = labels.get(_clean(BeautifulSoup(label, "lxml").get_text()))
        if not key:
            continue
        fragment = BeautifulSoup(value, "lxml")
        if key == "description":
            for br in fragment.find_all("br"):
                br.replace_with("\n")
            text = fragment.get_text().strip()
            out[key] = re.sub(r"\n{3,}", "\n\n", text) or None
        elif key == "homepage":
            link = fragment.find("a")
            href = link.get("href", "") if link else _clean(fragment.get_text())
            out[key] = href if href.startswith(("http://", "https://")) else None
        elif key == "email":
            link = fragment.find("a")
            href = link.get("href", "") if link else ""
            out[key] = href.removeprefix("mailto:") or _clean(fragment.get_text()) or None
        else:
            text = _clean(fragment.get_text())
            if key == "organizer":
                text = re.sub(r"\(Alle Termine dieses Veranstalters\)\s*$", "", text).strip()
            out[key] = text or None
    return out


def _image_url(soup: BeautifulSoup, row_id: str) -> str | None:
    row = soup.find("tr", id=f"in{row_id}fo")
    if not isinstance(row, Tag):
        return None
    match = _PIC_RE.search(row.decode_contents())
    return match.group(1) if match else None


def parse_results(html: str, fallback_year: int | None = None) -> list[Market]:
    year = fallback_year or date.today().year
    soup = BeautifulSoup(html, "lxml")
    markets: list[Market] = []

    for span in soup.find_all("span", id=_ROW_ID_RE):
        row_id = _ROW_ID_RE.match(span["id"]).group(1)
        tr = span.find_parent("tr")
        if not isinstance(tr, Tag):
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            continue

        date_raw = _clean(cells[0].get_text(" "))
        title = _clean(span.get_text(" "))
        plz = _clean(cells[3].get_text())
        city = _clean(cells[4].get_text())
        address = _clean(cells[5].get_text())
        date_from, date_to = _parse_dates(date_raw, year)

        key = f"{title}|{plz}|{address}|{date_raw}|{_clean(cells[1].get_text())}"
        markets.append(
            Market(
                id=hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
                title=title,
                date_from=date_from,
                date_to=date_to,
                date_raw=date_raw,
                time_raw=_clean(cells[1].get_text()),
                plz=plz,
                city=city,
                address=address,
                image_url=_image_url(soup, row_id),
                **_detail_fields(soup, row_id),
            )
        )
    return markets


def build_url(
    *,
    plz: str,
    date_from: date,
    date_to: date,
    category: int | None = None,
    query: str = "",
    start: int = 0,
) -> tuple[str, dict[str, str]]:
    params = {
        "kat": str(category) if category else "",
        "bundesland": "",
        "plz": plz,
        "regional": "",
        "zeitraum": f"{date_from.isoformat()}_bis_{date_to.isoformat()}",
        "turnus": "",
        "q": query,
        "sammelgebiet": "",
    }
    if start:
        params["start"] = str(start)
    return BASE_URL, params


async def fetch_page(
    client: httpx.AsyncClient,
    *,
    plz: str,
    date_from: date,
    date_to: date,
    category: int | None = None,
    query: str = "",
    start: int = 0,
    attempts: int = 4,
) -> str:
    """Fetch one result page, retrying transient network and 5xx failures.

    A full build makes roughly a thousand sequential requests, so a single
    dropped connection must not abort the run.
    """
    url, params = build_url(
        plz=plz,
        date_from=date_from,
        date_to=date_to,
        category=category,
        query=query,
        start=start,
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"server error {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            last_error = error
            if attempt == attempts - 1:
                break
            await asyncio.sleep(2.0 * 2**attempt)

    raise RuntimeError(f"giving up on {url} start={start} after {attempts} attempts") from last_error


async def scrape(
    client: httpx.AsyncClient,
    *,
    plz: str,
    date_from: date,
    date_to: date,
    category: int | None = None,
    query: str = "",
    max_pages: int = 80,
    page_delay_s: float = 0.0,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Market]:
    """Fetch every result page for one PLZ filter, sequentially and politely."""
    collected: list[Market] = []
    seen: set[str] = set()

    for page in range(max_pages):
        if page and page_delay_s:
            await asyncio.sleep(page_delay_s)
        html = await fetch_page(
            client,
            plz=plz,
            date_from=date_from,
            date_to=date_to,
            category=category,
            query=query,
            start=page * PAGE_SIZE,
        )
        batch = parse_results(html, fallback_year=date_from.year)
        new = [m for m in batch if m.id not in seen]
        seen.update(m.id for m in new)
        collected.extend(new)
        if on_page:
            on_page(page, len(collected))
        if len(batch) < PAGE_SIZE:
            break

    return collected

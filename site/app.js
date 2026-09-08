/* Flohmarkt Radar - static client.
 * All filtering happens in the browser against prebuilt shards in data/markets/.
 */

const el = (id) => document.getElementById(id);
const EARTH_RADIUS_KM = 6371.0088;
const DEFAULT_CATEGORY = "1"; // Flohmarkt

const map = L.map("map").setView([48.2082, 16.3738], 10);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "© OpenStreetMap",
}).addTo(map);

const markerLayer = L.layerGroup().addTo(map);
let originMarker = null;
let radiusCircle = null;
let origin = null;
let meta = null;
const loadedShards = new Map();

function setStatus(text, isError = false) {
  el("status").textContent = text;
  el("status").classList.toggle("error", isError);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const p1 = lat1 * rad;
  const p2 = lat2 * rad;
  const dp = p2 - p1;
  const dl = (lon2 - lon1) * rad;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(a));
}

function fmtDate(m) {
  if (!m.date_from) return m.date_raw || "";
  const opts = { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" };
  const from = new Date(m.date_from).toLocaleDateString("de-AT", opts);
  if (m.date_to && m.date_to !== m.date_from) {
    return `${from} – ${new Date(m.date_to).toLocaleDateString("de-AT", opts)}`;
  }
  return from;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// --- data loading -----------------------------------------------------------

/** Degrees of longitude per km at a given latitude, used to size the shard box. */
function bboxIntersects(bbox, lat, lon, radiusKm) {
  const dLat = radiusKm / 111.32;
  const dLon = radiusKm / (111.32 * Math.max(Math.cos(lat * (Math.PI / 180)), 0.01));
  const [minLat, minLon, maxLat, maxLon] = bbox;
  return (
    lat + dLat >= minLat && lat - dLat <= maxLat && lon + dLon >= minLon && lon - dLon <= maxLon
  );
}

async function loadShard(id) {
  if (!loadedShards.has(id)) {
    loadedShards.set(
      id,
      fetch(`data/markets/${id}.json`)
        .then((r) => (r.ok ? r.json() : []))
        .catch(() => [])
    );
  }
  return loadedShards.get(id);
}

async function relevantMarkets(lat, lon, radiusKm) {
  const padded = radiusKm + (meta?.approx_buffer_km ?? 0);
  const ids = (meta?.shards ?? [])
    .filter((s) => bboxIntersects(s.bbox, lat, lon, padded))
    .map((s) => s.id);
  const batches = await Promise.all(ids.map(loadShard));
  return batches.flat();
}

// --- filtering --------------------------------------------------------------

function overlapsWindow(m, todayIso, endIso) {
  const from = m.date_from || m.date_to;
  const to = m.date_to || m.date_from;
  if (!from) return true;
  return from <= endIso && to >= todayIso;
}

function matchesKeyword(m, needle) {
  if (!needle) return true;
  const haystack = [m.title, m.description, m.organizer, m.city, m.address]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

async function search() {
  if (!origin) {
    setStatus("Zuerst Standort bestimmen oder Adresse eingeben.", true);
    return;
  }
  const radiusKm = Number(el("radius").value);
  const days = Number(el("days").value);
  const category = el("category").value;
  const needle = el("q").value.trim().toLowerCase();
  const buffer = meta?.approx_buffer_km ?? 0;

  setStatus("Suche läuft …");
  const candidates = await relevantMarkets(origin.lat, origin.lon, radiusKm);

  const today = new Date();
  const todayIso = today.toISOString().slice(0, 10);
  const endIso = new Date(today.getTime() + days * 86400000).toISOString().slice(0, 10);

  const hits = [];
  for (const m of candidates) {
    if (category && String(m.category ?? "") !== category) continue;
    if (!overlapsWindow(m, todayIso, endIso)) continue;
    if (!matchesKeyword(m, needle)) continue;
    const distance = haversineKm(origin.lat, origin.lon, m.lat, m.lon);
    const limit = radiusKm + (m.geo_precision === "plz" ? buffer : 0);
    if (distance > limit) continue;
    hits.push({ ...m, distance_km: Math.round(distance * 10) / 10 });
  }

  hits.sort((a, b) => {
    const da = (a.date_from || "9999-12-31") < todayIso ? todayIso : a.date_from || "9999-12-31";
    const db = (b.date_from || "9999-12-31") < todayIso ? todayIso : b.date_from || "9999-12-31";
    return da === db ? a.distance_km - b.distance_km : da < db ? -1 : 1;
  });

  render(hits);
  drawOrigin();
  setStatus(
    `${hits.length} Termine im Umkreis von ${radiusKm} km ` +
      `(${candidates.length} Termine in der Region geprüft).`
  );
}

// --- rendering --------------------------------------------------------------

function drawOrigin() {
  if (!origin) return;
  const radius = Number(el("radius").value) * 1000;
  if (originMarker) map.removeLayer(originMarker);
  if (radiusCircle) map.removeLayer(radiusCircle);
  originMarker = L.circleMarker([origin.lat, origin.lon], {
    radius: 7, color: "#ff8a3d", fillColor: "#ff8a3d", fillOpacity: 1,
  }).addTo(map).bindPopup("Dein Standort");
  radiusCircle = L.circle([origin.lat, origin.lon], {
    radius, color: "#ff8a3d", weight: 1, fillOpacity: 0.05,
  }).addTo(map);
  map.fitBounds(radiusCircle.getBounds(), { padding: [20, 20] });
}

function render(hits) {
  markerLayer.clearLayers();
  const list = el("list");
  list.innerHTML = "";

  if (!hits.length) {
    list.innerHTML = '<div class="card">Keine Termine im gewählten Umkreis und Zeitraum.</div>';
    return;
  }

  for (const m of hits) {
    const approx = m.geo_precision === "plz";
    const marker = L.marker([m.lat, m.lon]).addTo(markerLayer);
    marker.bindPopup(
      `<b>${escapeHtml(m.title)}</b><br>${fmtDate(m)}<br>${escapeHtml(m.time_raw)}<br>` +
        `${escapeHtml(m.address)}<br>${escapeHtml(m.plz)} ${escapeHtml(m.city)}` +
        `<br><i>${m.distance_km} km${approx ? " (ungefähr)" : ""}</i>`
    );

    const destination = encodeURIComponent(`${m.address}, ${m.plz} ${m.city}`);
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `
      <h3>${escapeHtml(m.title)}</h3>
      <div class="meta">
        <span>${fmtDate(m)}</span>
        <span>${escapeHtml(m.time_raw)}</span>
        <span class="dist ${approx ? "approx" : ""}">${m.distance_km} km${approx ? " ~" : ""}</span>
      </div>
      <div class="addr">${escapeHtml(m.address)}, ${escapeHtml(m.plz)} ${escapeHtml(m.city)}</div>
      ${m.description ? `<div class="desc">${escapeHtml(m.description)}</div>` : ""}
      <div class="links">
        ${m.organizer ? `<span>${escapeHtml(m.organizer)}</span>` : ""}
        ${m.phone ? `<a href="tel:${escapeHtml(m.phone)}">${escapeHtml(m.phone)}</a>` : ""}
        ${m.email ? `<a href="mailto:${escapeHtml(m.email)}">E-Mail</a>` : ""}
        ${m.homepage ? `<a href="${escapeHtml(m.homepage)}" target="_blank" rel="noopener">Website</a>` : ""}
        <a href="https://www.google.com/maps/dir/?api=1&destination=${destination}" target="_blank" rel="noopener">Route</a>
      </div>`;
    card.addEventListener("click", () => {
      document.querySelectorAll(".card.active").forEach((c) => c.classList.remove("active"));
      card.classList.add("active");
      card.classList.toggle("open");
      map.setView([m.lat, m.lon], Math.max(map.getZoom(), 14));
      marker.openPopup();
    });
    list.appendChild(card);
  }
}

// --- address autocomplete ---------------------------------------------------
// Primary source is an offline street index built from the Austrian address
// register; Photon is queried only when the index has nothing useful.

const PHOTON_URL = "https://photon.komoot.io/api/";
const streetShards = new Map();

function normalizeStreet(text) {
  return text
    .toLowerCase()
    .replace(/ß/g, "ss")
    .replace(/ä/g, "a").replace(/ö/g, "o").replace(/ü/g, "u")
    .replace(/[^a-z0-9]/g, "");
}

function splitHouseNumber(text) {
  const match = text.trim().match(/^(.*?)[\s,]+(\d+\s*[a-zA-Z]?)\s*$/);
  return match ? { street: match[1], number: match[2].replace(/\s+/g, "") } : { street: text, number: "" };
}

async function loadStreetShard(letter) {
  if (!streetShards.has(letter)) {
    streetShards.set(
      letter,
      fetch(`data/streets/${letter}.json`)
        .then((r) => (r.ok ? r.json() : null))
        .then((rows) =>
          rows ? rows.map((r) => ({ street: r[0], plz: r[1], city: r[2], lat: r[3], lon: r[4], key: normalizeStreet(r[0]) })) : []
        )
        .catch(() => [])
    );
  }
  return streetShards.get(letter);
}

async function localSuggest(text, limit) {
  const { street, number } = splitHouseNumber(text);
  const key = normalizeStreet(street);
  if (key.length < 3) return [];

  const rows = await loadStreetShard(key[0]);
  if (!rows.length) return [];

  const prefix = [];
  const contains = [];
  for (const row of rows) {
    if (row.key.startsWith(key)) prefix.push(row);
    else if (row.key.includes(key)) contains.push(row);
    if (prefix.length > 400) break;
  }
  const ranked = [...prefix, ...contains].sort(
    (a, b) => a.key.length - b.key.length || a.plz.localeCompare(b.plz)
  );

  return ranked.slice(0, limit).map((row) => ({
    label: `${row.street}${number ? " " + number : ""}, ${row.plz} ${row.city}`,
    lat: row.lat,
    lon: row.lon,
  }));
}

function photonLabel(props) {
  const street = [props.street, props.housenumber].filter(Boolean).join(" ");
  const place = [props.postcode, props.city || props.county].filter(Boolean).join(" ");
  return [props.name, street || null, place || null].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(", ");
}

async function photonSuggest(text, limit) {
  try {
    const url = `${PHOTON_URL}?q=${encodeURIComponent(text)}&limit=${limit}&lang=de&bbox=9.4,46.3,17.2,49.1`;
    const res = await fetch(url);
    if (!res.ok) return [];
    const features = (await res.json()).features || [];
    return features
      .filter((f) => f.properties?.countrycode === "AT")
      .map((f) => ({
        label: photonLabel(f.properties),
        lat: f.geometry.coordinates[1],
        lon: f.geometry.coordinates[0],
      }))
      .filter((hit) => hit.label);
  } catch {
    return [];
  }
}

async function fetchSuggestions(text, limit = 6) {
  const local = await localSuggest(text, limit);
  if (local.length >= 3) return local;
  const remote = await photonSuggest(text, limit);
  const seen = new Set(local.map((h) => h.label));
  return [...local, ...remote.filter((h) => !seen.has(h.label))].slice(0, limit);
}

const placeInput = el("place");
const suggestBox = el("suggestions");
let suggestions = [];
let activeIndex = -1;
let suggestTimer = null;
let suggestSeq = 0;

function closeSuggestions() {
  suggestBox.hidden = true;
  suggestBox.innerHTML = "";
  placeInput.setAttribute("aria-expanded", "false");
  suggestions = [];
  activeIndex = -1;
}

function highlight(index) {
  activeIndex = index;
  [...suggestBox.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === index)));
  if (index >= 0) suggestBox.children[index]?.scrollIntoView({ block: "nearest" });
}

function chooseSuggestion(index) {
  const hit = suggestions[index];
  if (!hit) return;
  placeInput.value = hit.label;
  origin = { lat: hit.lat, lon: hit.lon };
  closeSuggestions();
  search();
}

function renderSuggestions(items) {
  suggestions = items;
  suggestBox.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "Keine Treffer";
    suggestBox.appendChild(li);
  } else {
    items.forEach((hit, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.textContent = hit.label;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        chooseSuggestion(i);
      });
      suggestBox.appendChild(li);
    });
  }
  suggestBox.hidden = false;
  placeInput.setAttribute("aria-expanded", "true");
  highlight(-1);
}

async function runSuggest(text) {
  const seq = ++suggestSeq;
  const items = await fetchSuggestions(text);
  if (seq === suggestSeq) renderSuggestions(items);
}

async function findPlace() {
  const value = placeInput.value.trim();
  if (value.length < 2) return;
  setStatus("Adresse wird gesucht …");
  const hits = await fetchSuggestions(value, 1);
  if (!hits.length) {
    setStatus("Adresse nicht gefunden.", true);
    return;
  }
  placeInput.value = hits[0].label;
  origin = { lat: hits[0].lat, lon: hits[0].lon };
  search();
}

placeInput.addEventListener("input", () => {
  clearTimeout(suggestTimer);
  const text = placeInput.value.trim();
  if (text.length < 3) {
    closeSuggestions();
    return;
  }
  suggestTimer = setTimeout(() => runSuggest(text), 200);
});

placeInput.addEventListener("keydown", (e) => {
  const open = !suggestBox.hidden && suggestions.length > 0;
  if (e.key === "ArrowDown" && open) {
    e.preventDefault();
    highlight((activeIndex + 1) % suggestions.length);
  } else if (e.key === "ArrowUp" && open) {
    e.preventDefault();
    highlight((activeIndex - 1 + suggestions.length) % suggestions.length);
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (open && activeIndex >= 0) chooseSuggestion(activeIndex);
    else {
      closeSuggestions();
      findPlace();
    }
  } else if (e.key === "Escape") {
    closeSuggestions();
  }
});

placeInput.addEventListener("blur", () => setTimeout(closeSuggestions, 150));

// --- wiring -----------------------------------------------------------------

el("radius").addEventListener("input", () => {
  el("radiusOut").textContent = `${el("radius").value} km`;
  drawOrigin();
});
el("radius").addEventListener("change", () => search());
el("days").addEventListener("change", () => search());
el("category").addEventListener("change", () => search());
el("q").addEventListener("keydown", (e) => e.key === "Enter" && search());
el("findPlace").addEventListener("click", () => {
  closeSuggestions();
  findPlace();
});

el("locate").addEventListener("click", () => {
  if (!navigator.geolocation) {
    setStatus("Browser unterstützt keine Standortabfrage.", true);
    return;
  }
  setStatus("Standort wird ermittelt …");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      origin = { lat: pos.coords.latitude, lon: pos.coords.longitude };
      search();
    },
    (err) => setStatus(`Standort nicht verfügbar: ${err.message}`, true),
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

map.on("contextmenu", (e) => {
  origin = { lat: e.latlng.lat, lon: e.latlng.lng };
  search();
});

(async () => {
  try {
    meta = await (await fetch("data/meta.json")).json();
  } catch {
    setStatus("Termindaten konnten nicht geladen werden.", true);
    return;
  }

  for (const [id, name] of Object.entries(meta.categories || {})) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = name;
    el("category").appendChild(opt);
  }
  el("category").value = DEFAULT_CATEGORY;

  const stamp = new Date(meta.generated_at).toLocaleString("de-AT");
  el("generated").textContent = `${meta.total_located} Termine, Stand ${stamp}`;
  if (meta.address_index) {
    el("addressCredit").textContent = `Adressen: © Österreichisches Adressregister, Stichtagsdaten vom ${meta.address_index.stichtag} (CC BY 4.0)`;
  }

  setStatus("Standort bestimmen, Adresse eingeben oder mit Rechtsklick auf die Karte einen Punkt wählen.");
})();

const el = (id) => document.getElementById(id);
const map = L.map("map").setView([48.2082, 16.3738], 10);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "© OpenStreetMap",
}).addTo(map);

const markerLayer = L.layerGroup().addTo(map);
let originMarker = null;
let radiusCircle = null;
let origin = null;
let markers = new Map();
let pendingPoll = null;

function setStatus(text, isError = false) {
  el("status").textContent = text;
  el("status").classList.toggle("error", isError);
}

function fmtDate(m) {
  if (!m.date_from) return m.date_raw;
  const opts = { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" };
  const from = new Date(m.date_from).toLocaleDateString("de-AT", opts);
  if (m.date_to && m.date_to !== m.date_from) {
    const to = new Date(m.date_to).toLocaleDateString("de-AT", opts);
    return `${from} – ${to}`;
  }
  return from;
}

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

function render(data) {
  markerLayer.clearLayers();
  markers = new Map();
  const list = el("list");
  list.innerHTML = "";

  if (!data.markets.length) {
    list.innerHTML = '<div class="card">Keine Termine im gewählten Umkreis und Zeitraum.</div>';
    return;
  }

  for (const m of data.markets) {
    const approx = m.geo_precision === "plz";
    const marker = L.marker([m.lat, m.lon]).addTo(markerLayer);
    marker.bindPopup(
      `<b>${m.title}</b><br>${fmtDate(m)}<br>${m.time_raw}<br>${m.address}<br>${m.plz} ${m.city}` +
      `<br><i>${m.distance_km} km${approx ? " (ungefähr)" : ""}</i>`
    );
    markers.set(m.id, marker);

    const card = document.createElement("article");
    card.className = "card";
    card.dataset.id = m.id;
    card.innerHTML = `
      <h3>${m.title}</h3>
      <div class="meta">
        <span>${fmtDate(m)}</span>
        <span>${m.time_raw}</span>
        <span class="dist ${approx ? "approx" : ""}">${m.distance_km} km${approx ? " ~" : ""}</span>
      </div>
      <div class="addr">${m.address}, ${m.plz} ${m.city}</div>
      ${m.description ? `<div class="desc">${m.description}</div>` : ""}
      <div class="links">
        ${m.organizer ? `<span>${m.organizer}</span>` : ""}
        ${m.phone ? `<a href="tel:${m.phone}">${m.phone}</a>` : ""}
        ${m.email ? `<a href="mailto:${m.email}">E-Mail</a>` : ""}
        ${m.homepage ? `<a href="${m.homepage}" target="_blank" rel="noopener">Website</a>` : ""}
        <a href="https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(m.address + ", " + m.plz + " " + m.city)}" target="_blank" rel="noopener">Route</a>
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

async function search({ refresh = false } = {}) {
  if (!origin) {
    setStatus("Zuerst Standort bestimmen oder Adresse eingeben.", true);
    return;
  }
  const params = new URLSearchParams({
    lat: origin.lat,
    lon: origin.lon,
    radius_km: el("radius").value,
    days: el("days").value,
    refresh: String(refresh),
  });
  if (el("category").value) params.set("category", el("category").value);
  if (el("q").value.trim()) params.set("q", el("q").value.trim());

  setStatus(refresh ? "Lade frische Daten von flohmarkt.at …" : "Suche läuft …");
  try {
    const res = await fetch(`/api/search?${params}`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    render(data);
    drawOrigin();
    setStatus(
      `${data.markets.length} Termine im Umkreis von ${data.radius_km} km ` +
      `(${data.total_scraped} Termine in der Region geprüft).`
    );
    pollPending();
  } catch (err) {
    setStatus(`Fehler: ${err.message}`, true);
  }
}

async function pollPending() {
  clearTimeout(pendingPoll);
  const res = await fetch("/api/stats");
  const stats = await res.json();
  if (stats.pending_geocodes > 0) {
    setStatus(
      `${el("list").childElementCount} Termine – verfeinere ${stats.pending_geocodes} Adressen im Hintergrund …`
    );
    pendingPoll = setTimeout(async () => {
      const again = await (await fetch("/api/stats")).json();
      if (again.pending_geocodes === 0) search();
      else pollPending();
    }, 4000);
  }
}

el("radius").addEventListener("input", () => {
  el("radiusOut").textContent = `${el("radius").value} km`;
  drawOrigin();
});
el("radius").addEventListener("change", () => search());
el("days").addEventListener("change", () => search());
el("category").addEventListener("change", () => search());
el("q").addEventListener("keydown", (e) => e.key === "Enter" && search());
el("refresh").addEventListener("click", () => search({ refresh: true }));

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

async function findPlace() {
  const value = el("place").value.trim();
  if (value.length < 2) return;
  setStatus("Adresse wird gesucht …");
  try {
    const res = await fetch(`/api/geocode?q=${encodeURIComponent(value)}`);
    if (!res.ok) throw new Error("Adresse nicht gefunden");
    const data = await res.json();
    origin = { lat: data.lat, lon: data.lon };
    el("place").value = data.label || value;
    search();
  } catch (err) {
    setStatus(err.message, true);
  }
}

// --- address autocomplete ---------------------------------------------------
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
  [...suggestBox.children].forEach((li, i) =>
    li.setAttribute("aria-selected", String(i === index))
  );
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
      li.role = "option";
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

async function fetchSuggestions(text) {
  const seq = ++suggestSeq;
  try {
    const res = await fetch(`/api/suggest?q=${encodeURIComponent(text)}`);
    if (!res.ok) throw new Error("suggest failed");
    const items = await res.json();
    if (seq === suggestSeq) renderSuggestions(items);
  } catch {
    if (seq === suggestSeq) closeSuggestions();
  }
}

placeInput.addEventListener("input", () => {
  clearTimeout(suggestTimer);
  const text = placeInput.value.trim();
  if (text.length < 3) {
    closeSuggestions();
    return;
  }
  suggestTimer = setTimeout(() => fetchSuggestions(text), 250);
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

el("findPlace").addEventListener("click", () => {
  closeSuggestions();
  findPlace();
});

map.on("contextmenu", (e) => {
  origin = { lat: e.latlng.lat, lon: e.latlng.lng };
  search();
});

const DEFAULT_CATEGORY = "1"; // Flohmarkt

(async () => {
  const cats = await (await fetch("/api/categories")).json();
  for (const [id, name] of Object.entries(cats)) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = name;
    el("category").appendChild(opt);
  }
  el("category").value = DEFAULT_CATEGORY;
  setStatus("Standort bestimmen, Adresse eingeben oder mit Rechtsklick auf die Karte einen Punkt wählen.");
})();

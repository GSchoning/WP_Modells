// GABORA landing page: mock login + aquifer selector.
// NOTE: This is a UI mockup. The credentials are checked client-side
// only; there is no real authentication. Do not rely on this for any
// access control.

const MOCK_USER = "Regulator";
const MOCK_PASS = "GABORA2027";
const SESSION_KEY = "gabora_session";

// Australia-wide bounds in EPSG:4326 — used as the initial fit if the
// /api/aquifers payload is missing or the load fails. Once the data is
// available we re-fit to the union of its bbox.
const FALLBACK_BOUNDS = [[138.0, -29.0], [154.0, -10.0]];

function $(id) { return document.getElementById(id); }

const STATE = {
  features: [],          // raw GeoJSON features
  byLabel: new Map(),    // label -> feature
  map: null,
  activeLabel: null,
};

function showSelector(username) {
  $("login-pane").hidden = true;
  $("selector-pane").hidden = false;
  $("user-chip").textContent = username;
  $("user-chip").hidden = false;
  $("logout-btn").hidden = false;
  // Fullscreen picker layout once logged in (handled in landing.css).
  document.body.classList.add("logged-in");
  if (!STATE.map) loadAquifers();
  // Map sizes itself off the container's clientHeight; nudge it after
  // the layout swap so the height is recomputed for fullscreen.
  if (STATE.map) setTimeout(() => STATE.map.resize(), 0);
}

function showLogin() {
  $("login-pane").hidden = false;
  $("selector-pane").hidden = true;
  $("user-chip").hidden = true;
  $("logout-btn").hidden = true;
  $("username").value = "";
  $("password").value = "";
  $("login-error").hidden = true;
  document.body.classList.remove("logged-in");
}

async function loadAquifers() {
  let geojson;
  try {
    const r = await fetch("/api/aquifers");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    geojson = await r.json();
  } catch (e) {
    console.warn("could not load /api/aquifers:", e);
    renderFallback();
    return;
  }
  STATE.features = geojson.features || [];
  for (const f of STATE.features) {
    STATE.byLabel.set(f.properties.label, f);
  }
  buildMap(geojson);
  renderList();
}

function renderFallback() {
  // Backend missing or shapefile not parseable — show a simple message
  // and a link directly to the Precipice module so the demo still works.
  const map = $("aquifer-map");
  map.innerHTML =
    `<div style="display:flex;align-items:center;justify-content:center;height:100%;
                  color:var(--ink-2);padding:2rem;text-align:center">
       Aquifer map unavailable.<br/>
       <a href="precipice.html" style="color:var(--accent);margin-top:0.6rem;
          display:inline-block;text-decoration:none;border:1px solid var(--line);
          padding:0.4rem 0.7rem;border-radius:6px">
         Open the Precipice module →
       </a>
     </div>`;
  $("aquifer-list").innerHTML =
    `<div style="padding:0.7rem;color:var(--ink-3);font-size:0.8rem">no aquifer list available</div>`;
}

function buildMap(geojson) {
  const style = GABORA.makeSatStyle({ roads: false, places: true });

  const map = new maplibregl.Map({
    container: "aquifer-map",
    style,
    bounds: bboxOfFeatures(geojson.features) || FALLBACK_BOUNDS,
    fitBoundsOptions: { padding: 30 },
  });
  STATE.map = map;

  map.on("load", () => {
    map.addSource("aquifers", { type: "geojson", data: geojson });

    map.addLayer({
      id: "aquifers-fill", type: "fill", source: "aquifers",
      paint: {
        "fill-color": [
          "case",
          ["boolean", ["get", "ready"], false], "#22c55e",
          "#94a3b8",
        ],
        "fill-opacity": [
          "case",
          ["boolean", ["get", "ready"], false], 0.55,
          0.28,
        ],
      },
    }, "places");
    map.addLayer({
      id: "aquifers-line", type: "line", source: "aquifers",
      paint: {
        "line-color": [
          "case",
          ["boolean", ["get", "ready"], false], "#15803d",
          "#475569",
        ],
        "line-width": 1.0,
        "line-opacity": 0.85,
      },
    }, "places");
    // Hover highlight uses a separate filter-driven layer.
    map.addLayer({
      id: "aquifers-hover", type: "line", source: "aquifers",
      filter: ["==", ["get", "label"], ""],
      paint: { "line-color": "#38bdf8", "line-width": 2.4, "line-opacity": 1 },
    }, "places");

    map.on("mousemove", "aquifers-fill", (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      map.getCanvas().style.cursor = "pointer";
      map.setFilter("aquifers-hover", ["==", ["get", "label"], f.properties.label]);
    });
    map.on("mouseleave", "aquifers-fill", () => {
      map.getCanvas().style.cursor = "";
      map.setFilter("aquifers-hover", ["==", ["get", "label"], ""]);
    });
    map.on("click", "aquifers-fill", (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      const p = f.properties || {};
      // Click = popup with a "Open" link. The list-item click navigates
      // directly; the map popup gives a confirmation step + context.
      const isReady = p.ready === true || p.ready === "true";
      const cta = isReady
        ? `<a href="${p.href}" style="color:#86efac;font-weight:600;text-decoration:none">Open Precipice module →</a>`
        : `<a href="${p.href}" style="color:#cbd5e1;text-decoration:none">View status →</a>`;
      const html =
        `<div><strong>${GABORA.escapeHtml(p.label)}</strong></div>` +
        `<div style="color:#cbd5e1;font-size:0.72rem;margin-top:2px">
           ${GABORA.escapeHtml(p.unit || "")}${p.unit && p.basin ? " · " : ""}${GABORA.escapeHtml(p.basin || "")}
         </div>` +
        `<div style="margin-top:0.45rem">${cta}</div>`;
      new maplibregl.Popup({ closeButton: true })
        .setLngLat(e.lngLat).setHTML(html).addTo(map);
      highlightInList(p.label);
    });
  });
}

function bboxOfFeatures(features) {
  if (!features?.length) return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const visit = (coords) => {
    if (typeof coords[0] === "number") {
      minX = Math.min(minX, coords[0]); maxX = Math.max(maxX, coords[0]);
      minY = Math.min(minY, coords[1]); maxY = Math.max(maxY, coords[1]);
      return;
    }
    for (const c of coords) visit(c);
  };
  for (const f of features) visit(f.geometry.coordinates);
  if (!Number.isFinite(minX)) return null;
  return [[minX, minY], [maxX, maxY]];
}

function renderList(filterText) {
  const q = (filterText || "").trim().toLowerCase();
  const list = $("aquifer-list");
  // Group by basin (BASIN column).
  const grouped = new Map();
  for (const f of STATE.features) {
    const p = f.properties;
    if (q && !(`${p.label} ${p.basin} ${p.unit}`).toLowerCase().includes(q)) continue;
    const key = p.basin || "Other";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(p);
  }

  if (grouped.size === 0) {
    list.innerHTML = `<div style="padding:0.7rem;color:var(--ink-3);font-size:0.8rem">no matches</div>`;
    return;
  }

  // Surface "ready" basins first; the rest alphabetical.
  const basinKeys = [...grouped.keys()].sort((a, b) => {
    const aReady = grouped.get(a).some(p => p.ready);
    const bReady = grouped.get(b).some(p => p.ready);
    if (aReady !== bReady) return aReady ? -1 : 1;
    return a.localeCompare(b);
  });

  let html = "";
  for (const basin of basinKeys) {
    const items = grouped.get(basin).slice().sort((a, b) => a.label.localeCompare(b.label));
    html += `<div class="basin-group"><h4>${basin || "Other"}</h4>`;
    for (const p of items) {
      const cls = p.ready ? "ready" : "soon";
      const status = p.ready ? "ready" : "soon";
      html += `<div class="aquifer-item ${cls}" data-label="${escapeAttr(p.label)}" data-href="${escapeAttr(p.href)}">
        <span class="dot ${cls}"></span>
        <span class="label-text">${GABORA.escapeHtml(p.label)}</span>
        <span class="badge-mini ${cls}">${status}</span>
      </div>`;
    }
    html += `</div>`;
  }
  list.innerHTML = html;

  list.querySelectorAll(".aquifer-item").forEach((el) => {
    el.addEventListener("mouseenter", () => {
      const lbl = el.dataset.label;
      if (STATE.map?.getLayer("aquifers-hover")) {
        STATE.map.setFilter("aquifers-hover", ["==", ["get", "label"], lbl]);
      }
    });
    el.addEventListener("mouseleave", () => {
      if (STATE.map?.getLayer("aquifers-hover")) {
        STATE.map.setFilter("aquifers-hover", ["==", ["get", "label"], ""]);
      }
    });
    el.addEventListener("click", () => {
      const lbl = el.dataset.label;
      flyToLabel(lbl);
      highlightInList(lbl);
    });
    el.addEventListener("dblclick", () => {
      window.location.href = el.dataset.href;
    });
  });
}

function flyToLabel(label) {
  const f = STATE.byLabel.get(label);
  if (!f || !STATE.map) return;
  const bbox = bboxOfFeatures([f]);
  if (bbox) {
    STATE.map.fitBounds(bbox, { padding: 60, maxZoom: 8, duration: 700 });
  }
  // Show the popup so the regulator can click "Open …".
  const center = polyCentroid(f);
  if (center) {
    const p = f.properties || {};
    const isReady = p.ready === true || p.ready === "true";
    const cta = isReady
      ? `<a href="${p.href}" style="color:#86efac;font-weight:600;text-decoration:none">Open Precipice module →</a>`
      : `<a href="${p.href}" style="color:#cbd5e1;text-decoration:none">View status →</a>`;
    new maplibregl.Popup({ closeButton: true })
      .setLngLat(center)
      .setHTML(
        `<div><strong>${GABORA.escapeHtml(p.label)}</strong></div>` +
        `<div style="color:#cbd5e1;font-size:0.72rem;margin-top:2px">${GABORA.escapeHtml(p.unit || "")}${p.unit && p.basin ? " · " : ""}${GABORA.escapeHtml(p.basin || "")}</div>` +
        `<div style="margin-top:0.45rem">${cta}</div>`)
      .addTo(STATE.map);
  }
}

function polyCentroid(feature) {
  // Cheap centroid: average vertex of the first ring of the first polygon.
  // Good enough for popup placement on the landing page.
  const g = feature.geometry;
  if (!g) return null;
  let ring;
  if (g.type === "Polygon") ring = g.coordinates[0];
  else if (g.type === "MultiPolygon") ring = g.coordinates[0][0];
  if (!ring || !ring.length) return null;
  let x = 0, y = 0;
  for (const [lng, lat] of ring) { x += lng; y += lat; }
  return [x / ring.length, y / ring.length];
}

function highlightInList(label) {
  STATE.activeLabel = label;
  document.querySelectorAll(".aquifer-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.label === label);
  });
  const active = document.querySelector(".aquifer-item.active");
  if (active) active.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function init() {
  // Restore session if present.
  const saved = sessionStorage.getItem(SESSION_KEY);
  if (saved) {
    showSelector(saved);
  } else {
    showLogin();
  }

  $("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const u = $("username").value.trim();
    const p = $("password").value;
    if (u === MOCK_USER && p === MOCK_PASS) {
      sessionStorage.setItem(SESSION_KEY, u);
      showSelector(u);
    } else {
      const err = $("login-error");
      err.textContent = "Invalid username or password.";
      err.hidden = false;
    }
  });

  $("logout-btn").addEventListener("click", () => {
    sessionStorage.removeItem(SESSION_KEY);
    showLogin();
  });

  $("aquifer-search").addEventListener("input", (e) => {
    renderList(e.target.value);
  });
}

init();

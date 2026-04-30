/* Frontend for the Precipice Sandstone water-licence impact API.
 *
 * Single-page MapLibre app: shows formation extent, outcrop, existing
 * bores, and springs, lets the user click to place a proposed bore,
 * POSTs to /api/scenarios, and renders results.
 */

const $ = (id) => document.getElementById(id);

const STATE = {
  projectCRS: null,
  proposedLngLat: null,
  proposedXY: null,
  cachedTransform: null,    // {forward(lon, lat) -> [x, y]}
  springsLayer: null,
};

function setStatus(msg, level = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = level;
}

async function projForward(lng, lat) {
  // Convert (lng, lat) to project CRS (m). The backend exposes everything
  // in EPSG:4326 for the map; we send the proposed bore back in the
  // project CRS so the model uses it directly. We use proj4 via a lazy
  // load and cache the transform per session.
  if (!STATE.cachedTransform) {
    if (!window.proj4) {
      await loadScript("https://unpkg.com/proj4@2.10.0/dist/proj4.js");
    }
    // Fetch project CRS WKT from backend (proj4 supports EPSG codes too).
    const code = STATE.projectCRS;
    proj4.defs(code, await fetchEpsgWkt(code));
    STATE.cachedTransform = proj4("EPSG:4326", code);
  }
  const [x, y] = STATE.cachedTransform.forward([lng, lat]);
  return [x, y];
}

async function fetchEpsgWkt(code) {
  // epsg.io has a free endpoint. proj4 understands these strings.
  const num = code.split(":")[1];
  const r = await fetch(`https://epsg.io/${num}.proj4`);
  return await r.text();
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = reject;
    document.head.appendChild(s);
  });
}

function fmt(v, p = 2) {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(p);
}

async function init() {
  setStatus("loading map data…");
  let mapData;
  try {
    mapData = await (await fetch("/api/map-data")).json();
  } catch (e) {
    setStatus("backend unreachable", "error");
    return;
  }
  STATE.projectCRS = mapData.crs;

  const map = new maplibregl.Map({
    container: "map",
    style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    bounds: mapData.bbox_4326,
    fitBoundsOptions: { padding: 30 },
  });

  map.on("load", async () => {
    if (mapData.formation_extent) {
      map.addSource("formation", { type: "geojson", data: mapData.formation_extent });
      map.addLayer({
        id: "formation-fill", type: "fill", source: "formation",
        paint: { "fill-color": "#cbd2d9", "fill-opacity": 0.25 },
      });
      map.addLayer({
        id: "formation-line", type: "line", source: "formation",
        paint: { "line-color": "#52606d", "line-width": 1 },
      });
    }
    if (mapData.outcrop) {
      map.addSource("outcrop", { type: "geojson", data: mapData.outcrop });
      map.addLayer({
        id: "outcrop-fill", type: "fill", source: "outcrop",
        paint: { "fill-color": "#34d399", "fill-opacity": 0.35 },
      });
    }
    if (mapData.pumping_bores) {
      map.addSource("pumping", { type: "geojson", data: mapData.pumping_bores });
      map.addLayer({
        id: "pumping-circles", type: "circle", source: "pumping",
        paint: {
          "circle-radius": 2.5, "circle-color": "#ef4444",
          "circle-opacity": 0.7, "circle-stroke-color": "#7f1d1d",
          "circle-stroke-width": 0.4,
        },
      });
    }
    if (mapData.springs) {
      map.addSource("springs", {
        type: "geojson",
        data: { ...mapData.springs, features: mapData.springs.features.map(f => ({...f, properties: {...f.properties, s_total: 0}})) },
      });
      map.addLayer({
        id: "springs-circles", type: "circle", source: "springs",
        paint: {
          "circle-radius": 5,
          "circle-color": [
            "case",
            [">", ["coalesce", ["get", "s_total"], 0], 5], "#7f1d1d",
            [">", ["coalesce", ["get", "s_total"], 0], 1], "#dc2626",
            [">", ["coalesce", ["get", "s_total"], 0], 0.1], "#f59e0b",
            "#2563eb",
          ],
          "circle-stroke-color": "#fff", "circle-stroke-width": 1,
        },
      });
      STATE.springsLayer = "springs-circles";

      map.on("click", "springs-circles", (e) => {
        const f = e.features[0];
        const props = f.properties || {};
        const idKey = ["spring_id","SpringID","Spring_ID","ID","OBJECTID","FID","id"]
          .find(k => props[k] != null);
        const id = idKey ? props[idKey] : "(spring)";
        new maplibregl.Popup()
          .setLngLat(e.lngLat)
          .setHTML(`<strong>${id}</strong><br/>s_total = ${fmt(Number(props.s_total) || 0)} m`)
          .addTo(map);
      });
    }
    map.addSource("proposed", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "proposed-circle", type: "circle", source: "proposed",
      paint: {
        "circle-radius": 9, "circle-color": "#f59e0b",
        "circle-stroke-color": "#1f2933", "circle-stroke-width": 2,
      },
    });

    map.on("click", async (e) => {
      // Don't intercept clicks on existing layers (springs/bores).
      const features = map.queryRenderedFeatures(e.point, {
        layers: ["springs-circles", "pumping-circles"].filter(l => map.getLayer(l)),
      });
      if (features.length) return;
      placeProposed(map, e.lngLat.lng, e.lngLat.lat);
    });

    setStatus(`ready — ${mapData.springs?.features?.length || 0} springs, click to place a bore`, "ok");
    refreshHealth();
  });

  $("scenario-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runScenario(map);
  });
}

async function refreshHealth() {
  try {
    const h = await (await fetch("/api/healthz")).json();
    if (!h.baseline_cached) setStatus("baseline still loading…");
  } catch {}
}

async function placeProposed(map, lng, lat) {
  setStatus("converting click to project CRS…");
  let xy;
  try {
    xy = await projForward(lng, lat);
  } catch (err) {
    console.error(err);
    setStatus("CRS conversion failed", "error");
    return;
  }
  STATE.proposedLngLat = [lng, lat];
  STATE.proposedXY = xy;
  $("x").value = xy[0].toFixed(0);
  $("y").value = xy[1].toFixed(0);
  map.getSource("proposed").setData({
    type: "FeatureCollection",
    features: [{ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lng, lat] } }],
  });
  setStatus(`proposed bore at (${xy[0].toFixed(0)}, ${xy[1].toFixed(0)})`, "ok");
}

async function runScenario(map) {
  const x = parseFloat($("x").value);
  const y = parseFloat($("y").value);
  const rate = parseFloat($("rate").value);
  const bore_id = $("bore_id").value || "PROPOSED_001";
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(rate)) {
    setStatus("fill in x, y, rate", "error");
    return;
  }
  $("run-btn").disabled = true;
  $("run-btn").textContent = "Running… (~5 min)";
  setStatus("running scenario C…");
  const t0 = performance.now();
  let resp;
  try {
    resp = await fetch("/api/scenarios", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposed_bore: { bore_id, x, y, rate_ML_per_year: rate } }),
    });
  } catch (err) {
    setStatus("network error", "error");
    $("run-btn").disabled = false;
    $("run-btn").textContent = "Run scenario";
    return;
  }
  if (!resp.ok) {
    const msg = await resp.text();
    setStatus(`scenario failed: ${msg}`, "error");
    $("run-btn").disabled = false;
    $("run-btn").textContent = "Run scenario";
    return;
  }
  const result = await resp.json();
  const dt = ((performance.now() - t0) / 1000).toFixed(1);
  setStatus(`done in ${dt}s (server ${result.runtime_seconds.toFixed(1)}s)`, "ok");
  $("run-btn").disabled = false;
  $("run-btn").textContent = "Run scenario";
  renderResults(result);
  recolorSprings(map, result);
}

function renderResults(result) {
  $("results-empty").hidden = true;
  $("results-meta").hidden = false;
  $("results-tables").hidden = false;
  $("results-meta").innerHTML = `
    <div><strong>${result.proposed_bore.bore_id}</strong>
      @ (${result.proposed_bore.x.toFixed(0)}, ${result.proposed_bore.y.toFixed(0)}),
      ${result.proposed_bore.rate_ML_per_year} ML/yr</div>
    <div class="muted">runtime: ${result.runtime_seconds.toFixed(1)}s · output years: ${result.output_years.join(", ")}</div>
  `;
  const lastYear = Math.max(...result.output_years);
  const top = result.top_n_total;
  let html = `<h3 style="font-size:0.95rem;margin:0.4rem 0 0.3rem">Top 10 most-impacted at t = ${lastYear} yr</h3>`;
  html += "<table><thead><tr><th>spring</th><th>s_appr.</th><th>s_add.</th><th>s_total</th></tr></thead><tbody>";
  for (const s of top) {
    html += `<tr><td>${s.spring_id}</td>
      <td class="num">${fmt(s.s_approved_m)}</td>
      <td class="num">${fmt(s.s_additional_m)}</td>
      <td class="num"><strong>${fmt(s.s_total_m)}</strong></td></tr>`;
  }
  html += "</tbody></table>";
  $("results-tables").innerHTML = html;
}

function recolorSprings(map, result) {
  if (!STATE.springsLayer || !map.getSource("springs")) return;
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  const totalById = {};
  for (const s of yearBlock.springs) totalById[s.spring_id] = s.s_total_m;

  const src = map.getSource("springs");
  const data = src._data;
  const idKeys = ["spring_id","SpringID","Spring_ID","ID","OBJECTID","FID","id"];
  for (const f of data.features) {
    const k = idKeys.find(k => f.properties[k] != null);
    const id = k ? String(f.properties[k]) : null;
    f.properties.s_total = id != null && id in totalById ? totalById[id] : 0;
  }
  src.setData(data);
}

window.addEventListener("DOMContentLoaded", init);

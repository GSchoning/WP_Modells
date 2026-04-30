/* Frontend for the Precipice Sandstone water-licence impact API.
 *
 * Single-page MapLibre app: shows formation extent, outcrop, existing
 * bores, and **spring complex centroids**. Click to place a proposed
 * bore, POST /api/scenarios, render results coloured by drawdown and
 * a regulatory threshold (default 0.4 m).
 */

const $ = (id) => document.getElementById(id);

const STATE = {
  projectCRS: null,
  threshold: 0.4,
  cachedTransform: null,
  complexCount: 0,
};

function setStatus(msg, level = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = level;
}

async function projForward(lng, lat) {
  if (!STATE.cachedTransform) {
    if (!window.proj4) {
      await loadScript("https://unpkg.com/proj4@2.10.0/dist/proj4.js");
    }
    const code = STATE.projectCRS;
    proj4.defs(code, await fetchEpsgWkt(code));
    STATE.cachedTransform = proj4("EPSG:4326", code);
  }
  const [x, y] = STATE.cachedTransform.forward([lng, lat]);
  return [x, y];
}

async function fetchEpsgWkt(code) {
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
  STATE.threshold = mapData.regulatory_threshold_m ?? 0.4;
  STATE.complexCount = mapData.spring_complexes?.features?.length ?? 0;
  $("threshold-display").textContent = STATE.threshold.toFixed(2);

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
        paint: { "fill-color": "#cbd2d9", "fill-opacity": 0.2 },
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
        paint: { "fill-color": "#34d399", "fill-opacity": 0.3 },
      });
    }
    if (mapData.pumping_bores) {
      map.addSource("pumping", { type: "geojson", data: mapData.pumping_bores });
      map.addLayer({
        id: "pumping-circles", type: "circle", source: "pumping",
        paint: {
          "circle-radius": 2.5, "circle-color": "#ef4444",
          "circle-opacity": 0.6, "circle-stroke-color": "#7f1d1d",
          "circle-stroke-width": 0.4,
        },
      });
    }
    if (mapData.spring_complexes) {
      map.addSource("complexes", { type: "geojson", data: mapData.spring_complexes });
      map.addLayer({
        id: "complex-circles", type: "circle", source: "complexes",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["coalesce", ["get", "n_springs"], 1],
            1, 4,  10, 7,  50, 11,
          ],
          "circle-color": [
            "case",
            ["==", ["get", "exceeds_threshold"], true], "#dc2626",
            [">", ["coalesce", ["get", "s_total"], 0], STATE.threshold * 0.5], "#f59e0b",
            [">", ["coalesce", ["get", "s_total"], 0], 0.05], "#fde68a",
            "#2563eb",
          ],
          "circle-stroke-color": "#fff", "circle-stroke-width": 1.2,
          "circle-opacity": 0.95,
        },
      });

      map.on("click", "complex-circles", (e) => {
        const f = e.features[0];
        const p = f.properties || {};
        const exceed = p.exceeds_threshold === "true" || p.exceeds_threshold === true;
        const flag = exceed ? `<div style="color:#dc2626;font-weight:600">⚠ exceeds ${STATE.threshold} m threshold</div>` : "";
        new maplibregl.Popup()
          .setLngLat(e.lngLat)
          .setHTML(`<strong>${p.complex_id}</strong><br/>
            ${p.n_springs} member spring${p.n_springs == 1 ? "" : "s"}<br/>
            s_total = ${fmt(Number(p.s_total) || 0)} m
            ${flag}`)
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
      const features = map.queryRenderedFeatures(e.point, {
        layers: ["complex-circles", "pumping-circles"].filter(l => map.getLayer(l)),
      });
      if (features.length) return;
      placeProposed(map, e.lngLat.lng, e.lngLat.lat);
    });

    setStatus(`ready — ${STATE.complexCount} spring complexes, click to place a bore`, "ok");
  });

  $("scenario-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runScenario(map);
  });
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
  const exceed = result.n_exceedances_any_year;
  const verdict = exceed > 0
    ? `⚠ ${exceed} complex${exceed === 1 ? "" : "es"} exceed ${result.regulatory_threshold_m} m`
    : `✓ no complex exceeds ${result.regulatory_threshold_m} m`;
  setStatus(`done in ${dt}s · ${verdict}`, exceed > 0 ? "error" : "ok");
  $("run-btn").disabled = false;
  $("run-btn").textContent = "Run scenario";
  renderResults(result);
  recolorComplexes(map, result);
}

function renderResults(result) {
  $("results-empty").hidden = true;
  $("results-meta").hidden = false;
  $("results-tables").hidden = false;

  const exceed = result.n_exceedances_any_year;
  const verdictHtml = exceed > 0
    ? `<div class="verdict bad">⚠ ${exceed} complex${exceed === 1 ? "" : "es"} exceed${exceed === 1 ? "s" : ""} the ${result.regulatory_threshold_m} m threshold</div>`
    : `<div class="verdict ok">✓ no complex exceeds the ${result.regulatory_threshold_m} m threshold</div>`;

  let metaHtml = verdictHtml + `
    <div><strong>${result.proposed_bore.bore_id}</strong>
      @ (${result.proposed_bore.x.toFixed(0)}, ${result.proposed_bore.y.toFixed(0)}),
      ${result.proposed_bore.rate_ML_per_year} ML/yr</div>
    <div class="muted">runtime: ${result.runtime_seconds.toFixed(1)}s · output years: ${result.output_years.join(", ")}</div>
  `;
  if (result.theis) {
    metaHtml += `<div class="muted">Theis local T = ${result.theis.T_m2_per_day.toFixed(2)} m²/d, S = ${result.theis.S_dimensionless.toExponential(2)} (well cell ${result.theis.well_cell.join(",")})</div>`;
  }
  $("results-meta").innerHTML = metaHtml;

  const lastYear = Math.max(...result.output_years);
  const top = result.top_n_total;
  const hasTheis = top.some(c => c.s_additional_theis_m != null);
  let html = `<h3 style="font-size:0.95rem;margin:0.4rem 0 0.3rem">Top 10 most-impacted complexes at t = ${lastYear} yr</h3>`;
  if (hasTheis) {
    html += "<table><thead><tr><th>complex</th><th>#</th><th>r (km)</th><th>s_appr.</th><th>s_add.</th><th>Theis</th><th>s_total</th></tr></thead><tbody>";
  } else {
    html += "<table><thead><tr><th>complex</th><th>#</th><th>s_appr.</th><th>s_add.</th><th>s_total</th></tr></thead><tbody>";
  }
  for (const c of top) {
    const r_km = c.r_to_proposed_m != null ? (c.r_to_proposed_m / 1000).toFixed(1) : "—";
    const exceed = c.exceeds_threshold ? ' class="bad-row"' : "";
    if (hasTheis) {
      html += `<tr${exceed}><td>${c.complex_id}</td>
        <td class="num">${c.n_springs}</td>
        <td class="num">${r_km}</td>
        <td class="num">${fmt(c.s_approved_m)}</td>
        <td class="num">${fmt(c.s_additional_m)}</td>
        <td class="num">${fmt(c.s_additional_theis_m)}</td>
        <td class="num"><strong>${fmt(c.s_total_m)}</strong></td></tr>`;
    } else {
      html += `<tr${exceed}><td>${c.complex_id}</td>
        <td class="num">${c.n_springs}</td>
        <td class="num">${fmt(c.s_approved_m)}</td>
        <td class="num">${fmt(c.s_additional_m)}</td>
        <td class="num"><strong>${fmt(c.s_total_m)}</strong></td></tr>`;
    }
  }
  html += "</tbody></table>";
  $("results-tables").innerHTML = html;
}

function recolorComplexes(map, result) {
  if (!map.getSource("complexes")) return;
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  const byId = {};
  for (const c of yearBlock.complexes) byId[c.complex_id] = c;

  const src = map.getSource("complexes");
  const data = src._data;
  for (const f of data.features) {
    const id = f.properties.complex_id;
    if (id in byId) {
      f.properties.s_total = byId[id].s_total_m;
      f.properties.exceeds_threshold = byId[id].exceeds_threshold;
    }
  }
  src.setData(data);
}

window.addEventListener("DOMContentLoaded", init);

/* Frontend for the Precipice Sandstone water-licence impact API.
 *
 * Single-page MapLibre app: shows formation extent, outcrop, existing
 * bores, and spring complex centroids. Click to place a proposed bore,
 * POST /api/scenarios, render results as a stacked bar chart by
 * complex with a regulatory threshold line, plus an Approve/Reject
 * recommendation.
 */

const $ = (id) => document.getElementById(id);

const STATE = {
  projectCRS: null,
  threshold: 0.4,
  cachedTransform: null,
  complexCount: 0,
  map: null,
  lastResult: null,
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
    s.src = src; s.onload = resolve; s.onerror = reject;
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

  // Esri World Imagery as a raster basemap. No API key required;
  // attribution is mandatory and shown by MapLibre's default control.
  const satelliteStyle = {
    version: 8,
    sources: {
      sat: {
        type: "raster",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        tileSize: 256,
        attribution: "Imagery © Esri, Maxar, Earthstar Geographics, USDA, USGS, IGN",
      },
    },
    layers: [{ id: "sat", type: "raster", source: "sat" }],
  };
  const map = new maplibregl.Map({
    container: "map",
    style: satelliteStyle,
    bounds: mapData.bbox_4326,
    fitBoundsOptions: { padding: 30 },
  });
  STATE.map = map;

  map.on("load", () => buildLayers(map, mapData));

  $("scenario-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runScenario(map);
  });

  window.addEventListener("resize", () => {
    if (STATE.lastResult) renderBarChart(STATE.lastResult);
  });
}

function buildLayers(map, mapData) {
  if (mapData.formation_extent) {
    map.addSource("formation", { type: "geojson", data: mapData.formation_extent });
    map.addLayer({ id: "formation-fill", type: "fill", source: "formation",
      paint: { "fill-color": "#cbd2d9", "fill-opacity": 0.2 } });
    map.addLayer({ id: "formation-line", type: "line", source: "formation",
      paint: { "line-color": "#52606d", "line-width": 1 } });
  }
  if (mapData.outcrop) {
    map.addSource("outcrop", { type: "geojson", data: mapData.outcrop });
    map.addLayer({ id: "outcrop-fill", type: "fill", source: "outcrop",
      paint: { "fill-color": "#34d399", "fill-opacity": 0.3 } });
  }
  if (mapData.pumping_bores) {
    map.addSource("pumping", { type: "geojson", data: mapData.pumping_bores });
    map.addLayer({ id: "pumping-circles", type: "circle", source: "pumping",
      paint: {
        "circle-radius": 2.5, "circle-color": "#ef4444",
        "circle-opacity": 0.6, "circle-stroke-color": "#7f1d1d",
        "circle-stroke-width": 0.4,
      } });
  }
  if (mapData.spring_complexes) {
    map.addSource("complexes", { type: "geojson", data: mapData.spring_complexes });
    map.addLayer({ id: "complex-circles", type: "circle", source: "complexes",
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
      } });

    map.on("click", "complex-circles", (e) => {
      const f = e.features[0];
      const p = f.properties || {};
      const exceed = p.exceeds_threshold === "true" || p.exceeds_threshold === true;
      const flag = exceed ? `<div style="color:#dc2626;font-weight:600">⚠ exceeds ${STATE.threshold} m</div>` : "";
      new maplibregl.Popup()
        .setLngLat(e.lngLat)
        .setHTML(`<strong>${p.complex_id}</strong><br/>
          ${p.n_springs} spring${p.n_springs == 1 ? "" : "s"}<br/>
          s_total = ${fmt(Number(p.s_total) || 0)} m
          ${flag}`)
        .addTo(map);
    });
  }
  map.addSource("proposed", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "proposed-circle", type: "circle", source: "proposed",
    paint: {
      "circle-radius": 9, "circle-color": "#f59e0b",
      "circle-stroke-color": "#1f2933", "circle-stroke-width": 2,
    } });

  map.on("click", (e) => {
    const features = map.queryRenderedFeatures(e.point, {
      layers: ["complex-circles", "pumping-circles"].filter(l => map.getLayer(l)),
    });
    if (features.length) return;
    placeProposed(map, e.lngLat.lng, e.lngLat.lat);
  });

  setStatus(`ready — ${STATE.complexCount} spring complexes, click to place a bore`, "ok");
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
  STATE.lastResult = result;
  setStatus(`done in ${dt}s`, result.n_exceedances_any_year > 0 ? "error" : "ok");
  $("run-btn").disabled = false;
  $("run-btn").textContent = "Run scenario";

  renderDecision(result);
  renderBarChart(result);
  renderTable(result);
  recolorComplexes(map, result);
}

function renderDecision(result) {
  const triggered = result.n_triggered_any_year ?? 0;
  const already = result.n_already_exceeded_any_year ?? 0;
  const thresh = result.regulatory_threshold_m;
  const badge = $("decision-badge");
  const detail = $("decision-detail");
  const meta = $("decision-meta");

  if (triggered > 0) {
    badge.className = "reject";
    badge.textContent = "REJECT";
    detail.textContent = `Proposed bore tips ${triggered} spring complex${triggered === 1 ? "" : "es"} over the ${thresh} m drawdown trigger threshold.`;
  } else {
    badge.className = "approve";
    badge.textContent = "APPROVE";
    detail.textContent = `No spring complex is tipped over the ${thresh} m threshold by the proposed bore.`;
  }

  let mh = "";
  if (already > 0) {
    mh += `<div class="advisory">⚠ Advisory: ${already} complex${already === 1 ? "" : "es"} ${already === 1 ? "is" : "are"} already predicted to exceed ${thresh} m from existing licences alone (not attributable to this proposal).</div>`;
  }
  mh += `<div><strong>${result.proposed_bore.bore_id}</strong> · ${result.proposed_bore.rate_ML_per_year} ML/yr</div>`;
  mh += `<div>(${result.proposed_bore.x.toFixed(0)}, ${result.proposed_bore.y.toFixed(0)}) · ${result.runtime_seconds.toFixed(1)}s</div>`;
  if (result.theis) {
    mh += `<div>Theis local T = ${result.theis.T_m2_per_day.toFixed(2)} m²/d, S = ${result.theis.S_dimensionless.toExponential(1)}</div>`;
  }
  meta.innerHTML = mh;
}

function renderBarChart(result) {
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  // Sort by s_total descending; keep all but cap at 30 for readability.
  const allComplexes = [...yearBlock.complexes].sort((a,b) => b.s_total_m - a.s_total_m);
  const complexes = allComplexes.slice(0, 30);

  const svg = $("bars");
  svg.innerHTML = "";

  const W = svg.clientWidth || svg.parentElement.clientWidth;
  const H = 280;
  const margin = { top: 22, right: 24, bottom: 90, left: 50 };
  const innerW = Math.max(50, W - margin.left - margin.right);
  const innerH = Math.max(40, H - margin.top - margin.bottom);

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", H);

  const maxTotal = Math.max(STATE.threshold * 1.5, ...complexes.map(c => c.s_total_m), 0.1);
  const yScale = v => margin.top + (innerH - (v / maxTotal) * innerH);
  const slot = innerW / Math.max(1, complexes.length);
  const barW = Math.min(28, Math.max(6, slot - 4));

  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  };

  // Y-axis ticks
  const nTicks = 4;
  for (let i = 0; i <= nTicks; i++) {
    const v = (maxTotal * i) / nTicks;
    const y = yScale(v);
    svg.appendChild(make("line", {
      x1: margin.left, x2: margin.left + innerW, y1: y, y2: y,
      stroke: "#e4e7eb", "stroke-width": 1,
    }));
    const t = make("text", {
      x: margin.left - 6, y: y + 3, "text-anchor": "end",
      "font-size": 10, fill: "#52606d",
    });
    t.textContent = v.toFixed(2);
    svg.appendChild(t);
  }
  // Y-axis label
  const yl = make("text", {
    x: 14, y: margin.top + innerH / 2,
    "text-anchor": "middle", "font-size": 10, fill: "#52606d",
    transform: `rotate(-90 14 ${margin.top + innerH / 2})`,
  });
  yl.textContent = "drawdown (m)";
  svg.appendChild(yl);

  // Threshold line
  const threshY = yScale(STATE.threshold);
  svg.appendChild(make("line", {
    x1: margin.left, x2: margin.left + innerW,
    y1: threshY, y2: threshY,
    stroke: "#dc2626", "stroke-width": 1.5, "stroke-dasharray": "4,3",
  }));
  const tl = make("text", {
    x: margin.left + innerW - 4, y: threshY - 4, "text-anchor": "end",
    "font-size": 10, fill: "#dc2626", "font-weight": 600,
  });
  tl.textContent = `threshold ${STATE.threshold} m`;
  svg.appendChild(tl);

  // Bars. Three categories drive the colours:
  //   triggered_by_proposed : the proposed bore tips it over (decision-relevant)
  //   already_exceeded       : was already over from existing licences
  //   ok                     : under threshold
  complexes.forEach((c, i) => {
    const x = margin.left + i * slot + (slot - barW) / 2;
    const triggered = c.triggered_by_proposed;
    const already = c.already_exceeded;
    const yApp = yScale(c.s_approved_m);
    const yTotal = yScale(c.s_total_m);
    const baseY = yScale(0);

    // Existing (Scenario A): muted purple if already over, slate otherwise.
    if (c.s_approved_m > 0) {
      const fill = already ? "#7c3aed" : "#475569";
      const r = make("rect", {
        x, y: yApp, width: barW, height: Math.max(0, baseY - yApp), fill,
      });
      r.appendChild(make_title(
        `${c.complex_id} · existing: ${fmt(c.s_approved_m)} m${already ? " (already exceeds)" : ""}`
      ));
      svg.appendChild(r);
    }
    // Additional (Scenario C): bright red iff this segment is what tips
    // the bar over; amber otherwise (including when bar was already over).
    if (c.s_additional_m > 0) {
      const fill = triggered ? "#dc2626" : "#f59e0b";
      const r = make("rect", {
        x, y: yTotal, width: barW, height: Math.max(0, yApp - yTotal), fill,
      });
      const tag = triggered ? " (TRIGGERS)" : (already ? " (on top of existing exceedance)" : "");
      r.appendChild(make_title(
        `${c.complex_id} · proposed: +${fmt(c.s_additional_m)} m, total: ${fmt(c.s_total_m)} m${tag}`
      ));
      svg.appendChild(r);
    }
    // Label
    const labelX = x + barW / 2;
    const labelY = margin.top + innerH + 8;
    const labelColor = triggered ? "#991b1b" : already ? "#5b21b6" : "#1f2933";
    const t = make("text", {
      x: labelX, y: labelY,
      "text-anchor": "end", "font-size": 9.5, fill: labelColor,
      transform: `rotate(-50 ${labelX} ${labelY})`,
    });
    t.textContent = c.complex_id.length > 22 ? c.complex_id.slice(0, 21) + "…" : c.complex_id;
    svg.appendChild(t);
  });

  // Caption
  const caption = make("text", {
    x: margin.left + innerW / 2, y: H - 6,
    "text-anchor": "middle", "font-size": 10, fill: "#52606d",
  });
  caption.textContent =
    `top ${complexes.length} of ${allComplexes.length} complexes at t = ${lastYear} yr · ` +
    `slate = existing, amber = proposed, red = triggered by proposal, purple = already exceeded`;
  svg.appendChild(caption);

  function make_title(text) {
    const t = document.createElementNS(ns, "title");
    t.textContent = text;
    return t;
  }
}

function renderTable(result) {
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  const all = [...yearBlock.complexes].sort((a, b) => b.s_total_m - a.s_total_m);
  const hasTheis = all.some(c => c.s_additional_theis_m != null);

  let html = "<table><thead><tr>";
  html += "<th>complex</th><th>#</th>";
  if (hasTheis) html += "<th>r (km)</th>";
  html += "<th>s_approved</th><th>s_additional</th>";
  if (hasTheis) html += "<th>Theis s_add.</th>";
  html += "<th>s_total</th></tr></thead><tbody>";

  for (const c of all) {
    const r_km = c.r_to_proposed_m != null ? (c.r_to_proposed_m / 1000).toFixed(1) : "—";
    const rowClass = c.triggered_by_proposed ? "triggered-row"
                    : c.already_exceeded ? "already-row" : "";
    const cls = rowClass ? ` class="${rowClass}"` : "";
    html += `<tr${cls} data-id="${c.complex_id}"><td>${c.complex_id}</td><td class="num">${c.n_springs}</td>`;
    if (hasTheis) html += `<td class="num">${r_km}</td>`;
    html += `<td class="num">${fmt(c.s_approved_m)}</td>`;
    html += `<td class="num">${fmt(c.s_additional_m)}</td>`;
    if (hasTheis) html += `<td class="num">${fmt(c.s_additional_theis_m)}</td>`;
    html += `<td class="num"><strong>${fmt(c.s_total_m)}</strong></td></tr>`;
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

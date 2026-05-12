/* Frontend for the Precipice Sandstone water-licence impact API.
 *
 * Single-page MapLibre app: shows formation extent, outcrop, existing
 * bores, and spring complex centroids. Three scenario flavours:
 *   - single: click map to place one new bore.
 *   - multi:  click map to add bores; per-row rate inputs.
 *   - trade:  pick an existing bore by ID, click map for destination.
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
  complexLngLat: {},      // complex_id -> [lng, lat] for fly-to
  selectedComplexId: null,
  scenarioType: "single", // "single" | "multi" | "trade"
  multiWells: [],         // [{x, y, lng, lat, rate_ML_per_year}]
  tradeFrom: null,        // {bore_id, x, y, lng, lat, rate_ML_per_year}
  tradeTo: null,          // {x, y, lng, lat}
  existingBores: [],
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

  // Scenario-type radios swap which form pane is visible and re-target
  // map clicks. State is preserved per mode.
  document.querySelectorAll('input[name="scenario-type"]').forEach((r) => {
    r.addEventListener("change", () => {
      STATE.scenarioType = r.value;
      $("mode-single").hidden = r.value !== "single";
      $("mode-multi").hidden  = r.value !== "multi";
      $("mode-trade").hidden  = r.value !== "trade";
      refreshScenarioMarkers(map);
      if (r.value === "trade" && STATE.existingBores.length === 0) {
        loadExistingBores();
      }
    });
  });

  // Trade-mode: when the user picks a from_bore_id, look it up and
  // mirror the selection onto the map.
  $("trade-from").addEventListener("input", () => {
    const id = $("trade-from").value.trim();
    const b = STATE.existingBores.find((x) => String(x.bore_id) === id);
    if (b) {
      STATE.tradeFrom = b;
      $("trade-from-info").textContent =
        `${b.bore_id}: ${b.rate_ML_per_year.toFixed(0)} ML/yr at (${b.x.toFixed(0)}, ${b.y.toFixed(0)})`;
    } else {
      STATE.tradeFrom = null;
      $("trade-from-info").textContent = "";
    }
    refreshScenarioMarkers(map);
  });
  $("trade-to-x").addEventListener("input", () => syncTradeToFromInputs(map));
  $("trade-to-y").addEventListener("input", () => syncTradeToFromInputs(map));

  window.addEventListener("resize", () => {
    if (STATE.lastResult) renderBarChart(STATE.lastResult);
  });

  setupSplitter(map);
  renderMultiWellsList();
}

function setupSplitter(map) {
  const splitter = $("splitter");
  if (!splitter) return;
  let dragging = false;
  let startY = 0;
  let startLowerH = 0;

  const onMove = (clientY) => {
    if (!dragging) return;
    const dy = clientY - startY;
    const min = 140;
    const max = window.innerHeight - 200;
    const newH = Math.max(min, Math.min(max, startLowerH - dy));
    $("app").style.gridTemplateRows = `auto minmax(140px, 1fr) 10px ${newH}px`;
    if (map) map.resize();
    if (STATE.lastResult) renderBarChart(STATE.lastResult);
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("resizing");
  };

  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    startY = e.clientY;
    startLowerH = $("lower").getBoundingClientRect().height;
    document.body.classList.add("resizing");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => onMove(e.clientY));
  window.addEventListener("mouseup", stop);
  window.addEventListener("mouseleave", stop);

  splitter.addEventListener("touchstart", (e) => {
    if (!e.touches[0]) return;
    dragging = true;
    startY = e.touches[0].clientY;
    startLowerH = $("lower").getBoundingClientRect().height;
    document.body.classList.add("resizing");
  }, { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (e.touches[0]) onMove(e.touches[0].clientY);
  }, { passive: true });
  window.addEventListener("touchend", stop);
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
    for (const f of mapData.spring_complexes.features) {
      STATE.complexLngLat[f.properties.complex_id] = f.geometry.coordinates;
    }
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
      "circle-radius": 9,
      // gold for new extractions, dark slate for the "from" bore in a
      // trade (the source being decommissioned by the transfer).
      "circle-color": [
        "case",
        ["==", ["get", "kind"], "from"], "#475569",
        "#f59e0b",
      ],
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
  // Dispatch the map click based on the active scenario mode.
  let xy;
  try {
    xy = await projForward(lng, lat);
  } catch (err) {
    console.error(err);
    setStatus("CRS conversion failed", "error");
    return;
  }
  const [x, y] = xy;
  if (STATE.scenarioType === "single") {
    $("x").value = x.toFixed(0);
    $("y").value = y.toFixed(0);
    setStatus(`proposed bore at (${x.toFixed(0)}, ${y.toFixed(0)})`, "ok");
  } else if (STATE.scenarioType === "multi") {
    const rate = parseFloat($("multi-default-rate").value) || 1000;
    STATE.multiWells.push({ x, y, lng, lat, rate_ML_per_year: rate });
    renderMultiWellsList();
    setStatus(`added bore #${STATE.multiWells.length} at (${x.toFixed(0)}, ${y.toFixed(0)})`, "ok");
  } else if (STATE.scenarioType === "trade") {
    STATE.tradeTo = { x, y, lng, lat };
    $("trade-to-x").value = x.toFixed(0);
    $("trade-to-y").value = y.toFixed(0);
    setStatus(`trade destination at (${x.toFixed(0)}, ${y.toFixed(0)})`, "ok");
  }
  refreshScenarioMarkers(map);
}

function renderMultiWellsList() {
  const list = $("multi-wells-list");
  if (!STATE.multiWells.length) {
    list.innerHTML = `<div class="multi-empty">No bores yet — click map to add</div>`;
    return;
  }
  let html = "";
  STATE.multiWells.forEach((w, i) => {
    html += `<div class="multi-well-row" data-i="${i}">
      <div class="well-coords">#${i + 1} · (${w.x.toFixed(0)}, ${w.y.toFixed(0)})</div>
      <input class="well-rate-input" type="number" min="0" step="any" value="${w.rate_ML_per_year}" data-i="${i}" />
      <button type="button" class="remove-btn" data-i="${i}" title="Remove">&times;</button>
    </div>`;
  });
  list.innerHTML = html;
  list.querySelectorAll(".well-rate-input").forEach((inp) => {
    inp.addEventListener("input", (e) => {
      const i = Number(e.target.dataset.i);
      const v = parseFloat(e.target.value);
      if (Number.isFinite(v)) STATE.multiWells[i].rate_ML_per_year = v;
    });
  });
  list.querySelectorAll(".remove-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.i);
      STATE.multiWells.splice(i, 1);
      renderMultiWellsList();
      refreshScenarioMarkers(STATE.map);
    });
  });
}

async function syncTradeToFromInputs(map) {
  const x = parseFloat($("trade-to-x").value);
  const y = parseFloat($("trade-to-y").value);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    STATE.tradeTo = null;
    refreshScenarioMarkers(map);
    return;
  }
  if (!window.proj4) await loadScript("https://unpkg.com/proj4@2.10.0/dist/proj4.js");
  if (!proj4.defs(STATE.projectCRS)) {
    proj4.defs(STATE.projectCRS, await fetchEpsgWkt(STATE.projectCRS));
  }
  const [lng, lat] = proj4(STATE.projectCRS, "EPSG:4326").forward([x, y]);
  STATE.tradeTo = { x, y, lng, lat };
  refreshScenarioMarkers(map);
}

async function loadExistingBores() {
  try {
    const r = await fetch("/api/existing-bores");
    if (!r.ok) return;
    const d = await r.json();
    STATE.existingBores = d.bores || [];
    const dl = $("existing-bores-list");
    dl.innerHTML = "";
    for (const b of STATE.existingBores) {
      const opt = document.createElement("option");
      opt.value = b.bore_id;
      opt.label = `${b.rate_ML_per_year.toFixed(0)} ML/yr`;
      dl.appendChild(opt);
    }
  } catch (err) {
    console.warn("failed to load existing bores", err);
  }
}

function refreshScenarioMarkers(map) {
  // The "proposed" map source holds whatever markers the current scenario
  // mode wants displayed: gold star for new extractions, slate for the
  // trade source (which is being removed).
  if (!map || !map.getSource) return;
  const src = map.getSource("proposed");
  if (!src) return;
  const features = [];
  if (STATE.scenarioType === "single") {
    const x = parseFloat($("x").value);
    const y = parseFloat($("y").value);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      const lngLat = projInverseCached(x, y);
      if (lngLat) {
        features.push({ type: "Feature", properties: { kind: "new" },
          geometry: { type: "Point", coordinates: lngLat } });
      }
    }
  } else if (STATE.scenarioType === "multi") {
    for (const w of STATE.multiWells) {
      features.push({ type: "Feature", properties: { kind: "new" },
        geometry: { type: "Point", coordinates: [w.lng, w.lat] } });
    }
  } else if (STATE.scenarioType === "trade") {
    if (STATE.tradeFrom) {
      features.push({ type: "Feature", properties: { kind: "from" },
        geometry: { type: "Point", coordinates: [STATE.tradeFrom.lng, STATE.tradeFrom.lat] } });
    }
    if (STATE.tradeTo) {
      features.push({ type: "Feature", properties: { kind: "new" },
        geometry: { type: "Point", coordinates: [STATE.tradeTo.lng, STATE.tradeTo.lat] } });
    }
  }
  src.setData({ type: "FeatureCollection", features });
}

function projInverseCached(x, y) {
  try {
    if (!window.proj4 || !STATE.projectCRS) return null;
    if (!proj4.defs(STATE.projectCRS)) return null;
    return proj4(STATE.projectCRS, "EPSG:4326").forward([x, y]);
  } catch { return null; }
}

async function runScenario(map) {
  // Build the request body based on the active scenario mode.
  const rechargeMult = parseFloat($("recharge_mult").value);
  const mult = Number.isFinite(rechargeMult) && rechargeMult >= 0 ? rechargeMult : 1.0;
  let body;
  if (STATE.scenarioType === "single") {
    const x = parseFloat($("x").value);
    const y = parseFloat($("y").value);
    const rate = parseFloat($("rate").value);
    const bore_id = $("bore_id").value || "PROPOSED_001";
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(rate) || rate <= 0) {
      setStatus("fill in x, y, rate (rate > 0)", "error");
      return;
    }
    body = {
      scenario_type: "single",
      proposed_bore: { bore_id, x, y, rate_ML_per_year: rate },
      recharge_multiplier: mult,
    };
  } else if (STATE.scenarioType === "multi") {
    if (STATE.multiWells.length === 0) {
      setStatus("add at least one bore (click map)", "error");
      return;
    }
    if (!STATE.multiWells.every((w) => w.rate_ML_per_year > 0)) {
      setStatus("all rates must be > 0", "error");
      return;
    }
    body = {
      scenario_type: "multi",
      new_wells: STATE.multiWells.map((w, i) => ({
        label: `BORE_${i + 1}`, x: w.x, y: w.y, rate_ML_per_year: w.rate_ML_per_year,
      })),
      recharge_multiplier: mult,
    };
  } else if (STATE.scenarioType === "trade") {
    if (!STATE.tradeFrom) {
      setStatus("pick an existing bore to trade from", "error");
      return;
    }
    if (!STATE.tradeTo || !Number.isFinite(STATE.tradeTo.x) || !Number.isFinite(STATE.tradeTo.y)) {
      setStatus("set a trade destination (click map)", "error");
      return;
    }
    body = {
      scenario_type: "trade",
      from_bore_id: STATE.tradeFrom.bore_id,
      to_x: STATE.tradeTo.x,
      to_y: STATE.tradeTo.y,
      recharge_multiplier: mult,
    };
  } else {
    setStatus("unknown scenario type", "error");
    return;
  }

  $("run-btn").disabled = true;
  $("run-btn").textContent = mult !== 1.0 ? "Running… (~10 min, re-baselining)" : "Running… (~5 min)";
  setStatus("running scenario C…");
  const t0 = performance.now();
  let resp;
  try {
    resp = await fetch("/api/scenarios", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
  renderSummaryStats(result);
  renderBarChart(result);
  renderTable(result);
  recolorComplexes(map, result);
}

function renderSummaryStats(result) {
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  const all = yearBlock.complexes;
  const triggered = all.filter(c => c.triggered_by_proposed).length;
  const already = all.filter(c => c.already_exceeded).length;
  const ok = all.length - triggered - already;
  $("stat-ok").textContent = ok;
  $("stat-triggered").textContent = triggered;
  $("stat-already").textContent = already;
  $("stat-total").textContent = all.length;
  $("summary-stats").classList.remove("empty");
}

function selectComplex(id) {
  STATE.selectedComplexId = id;
  const lngLat = STATE.complexLngLat[id];
  if (lngLat && STATE.map) {
    STATE.map.flyTo({ center: lngLat, zoom: Math.max(STATE.map.getZoom(), 9), speed: 1.4 });
    let popupHtml = `<strong>${id}</strong>`;
    if (STATE.lastResult) {
      const lastYear = Math.max(...STATE.lastResult.output_years);
      const yr = STATE.lastResult.by_year.find(y => y.time_years === lastYear);
      const c = yr?.complexes.find(x => x.complex_id === id);
      if (c) {
        const flag = c.triggered_by_proposed
          ? '<div style="color:#b91c1c;font-weight:600">⚠ triggered by proposal</div>'
          : c.already_exceeded
            ? '<div style="color:#6d28d9;font-weight:600">already exceeded</div>'
            : "";
        popupHtml += `<br/>${c.n_springs} spring${c.n_springs == 1 ? "" : "s"}` +
          `<br/>existing: ${fmt(c.s_approved_m)} m` +
          `<br/>proposed: +${fmt(c.s_additional_m)} m` +
          `<br/><strong>total: ${fmt(c.s_total_m)} m</strong>${flag}`;
      }
    }
    new maplibregl.Popup({ closeOnClick: true })
      .setLngLat(lngLat).setHTML(popupHtml).addTo(STATE.map);
  }
  const bars = document.querySelectorAll("#bars rect, #bars text");
  bars.forEach(el => {
    if (el.getAttribute("data-id") === id) el.classList.add("selected");
    else el.classList.remove("selected");
  });
  const rows = document.querySelectorAll("#results-tables tbody tr");
  rows.forEach(tr => {
    if (tr.getAttribute("data-id") === id) tr.classList.add("row-selected");
    else tr.classList.remove("row-selected");
  });
  loadAndRenderSeries(id);
}

async function loadAndRenderSeries(complexId) {
  let data;
  try {
    const resp = await fetch(`/api/spring-series?complex_id=${encodeURIComponent(complexId)}`);
    if (!resp.ok) return;
    data = await resp.json();
  } catch (err) {
    return;
  }
  renderSeriesChart(data);
}

function renderSeriesChart(data) {
  const pane = $("series-pane");
  const svg = $("series-chart");
  const title = $("series-title");
  const legend = $("series-legend");
  if (!data || !data.times_years || !data.times_years.length) {
    pane.hidden = true; return;
  }
  pane.hidden = false;
  const hasC = data.s_total_m != null;
  title.innerHTML = `Drawdown over time · <strong>${data.complex_id}</strong>` +
    (data.n_springs ? ` <span class="muted">(${data.n_springs} spring${data.n_springs === 1 ? "" : "s"})</span>` : "");

  const W = svg.clientWidth || svg.parentElement.clientWidth;
  const H = 180;
  const margin = { top: 14, right: 14, bottom: 28, left: 38 };
  const innerW = Math.max(40, W - margin.left - margin.right);
  const innerH = Math.max(30, H - margin.top - margin.bottom);

  const times = data.times_years;
  const tMin = times[0];
  const tMax = times[times.length - 1];
  const seriesA = data.s_approved_m;
  const seriesT = hasC ? data.s_total_m : seriesA;
  const threshold = data.threshold_m ?? 0.4;
  const peak = Math.max(threshold * 1.2, ...seriesT, ...seriesA, 0.05);

  const xScale = t => margin.left + ((t - tMin) / (tMax - tMin || 1)) * innerW;
  const yScale = v => margin.top + innerH - (Math.max(0, v) / peak) * innerH;

  svg.innerHTML = "";
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", H);
  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  };
  const path = (vals, color, dash, width) => {
    let d = "";
    for (let i = 0; i < times.length; i++) {
      d += (i === 0 ? "M" : "L") + xScale(times[i]) + "," + yScale(vals[i]) + " ";
    }
    return make("path", {
      d, fill: "none", stroke: color, "stroke-width": width || 1.6,
      "stroke-dasharray": dash || "",
    });
  };

  const nTicks = 4;
  for (let i = 0; i <= nTicks; i++) {
    const v = (peak * i) / nTicks;
    const y = yScale(v);
    svg.appendChild(make("line", {
      x1: margin.left, x2: margin.left + innerW, y1: y, y2: y,
      stroke: "#e4e7eb", "stroke-width": 1,
    }));
    const t = make("text", {
      x: margin.left - 5, y: y + 3, "text-anchor": "end",
      "font-size": 9, fill: "#52606d",
    });
    t.textContent = v.toFixed(2);
    svg.appendChild(t);
  }
  const xTicks = times.length <= 5 ? times : [times[0], 10, 25, 50, 100].filter(v => v <= tMax + 0.01);
  for (const xt of xTicks) {
    const x = xScale(xt);
    svg.appendChild(make("line", {
      x1: x, x2: x, y1: margin.top + innerH, y2: margin.top + innerH + 3,
      stroke: "#94a3b8", "stroke-width": 1,
    }));
    const t = make("text", {
      x, y: margin.top + innerH + 13, "text-anchor": "middle",
      "font-size": 9, fill: "#52606d",
    });
    t.textContent = `${xt}`;
    svg.appendChild(t);
  }
  const xLab = make("text", {
    x: margin.left + innerW / 2, y: H - 2, "text-anchor": "middle",
    "font-size": 9, fill: "#52606d",
  });
  xLab.textContent = "years";
  svg.appendChild(xLab);

  const tY = yScale(threshold);
  svg.appendChild(make("line", {
    x1: margin.left, x2: margin.left + innerW, y1: tY, y2: tY,
    stroke: "#dc2626", "stroke-width": 1.3, "stroke-dasharray": "4,3",
  }));
  const tLab = make("text", {
    x: margin.left + innerW - 3, y: tY - 3, "text-anchor": "end",
    "font-size": 9, fill: "#dc2626", "font-weight": 600,
  });
  tLab.textContent = `${threshold} m`;
  svg.appendChild(tLab);

  svg.appendChild(path(seriesA, "#475569", "", 1.6));
  let totColor = "#f59e0b";
  if (hasC) {
    const peakT = Math.max(...seriesT);
    const peakA = Math.max(...seriesA);
    if (peakT >= threshold && peakA < threshold) totColor = "#dc2626";
    else if (peakA >= threshold) totColor = "#7c3aed";
    svg.appendChild(path(seriesT, totColor, "", 2.0));
  }

  let legendHtml = `<span><span class="swatch-line" style="background:#475569"></span>existing (A)</span>`;
  if (hasC) {
    legendHtml += `<span><span class="swatch-line" style="background:${totColor}"></span>total (A + C)</span>`;
  }
  legendHtml += `<span><span class="swatch-line" style="background:#dc2626"></span>threshold</span>`;
  legend.innerHTML = legendHtml;
}

function renderDecision(result) {
  // Decision is anchored to the last output year (the regulatory horizon).
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  const triggered = yearBlock.complexes.filter(c => c.triggered_by_proposed).length;
  const already = yearBlock.complexes.filter(c => c.already_exceeded).length;
  const thresh = result.regulatory_threshold_m;
  const badge = $("decision-badge");
  const detail = $("decision-detail");
  const meta = $("decision-meta");

  if (triggered > 0) {
    badge.className = "reject";
    badge.textContent = "REJECT";
    detail.textContent = `Proposed bore tips ${triggered} spring complex${triggered === 1 ? "" : "es"} over the ${thresh} m drawdown trigger threshold at ${lastYear} yr.`;
  } else {
    badge.className = "approve";
    badge.textContent = "APPROVE";
    detail.textContent = `No spring complex is tipped over the ${thresh} m threshold by the proposed bore at ${lastYear} yr.`;
  }

  let mh = "";
  if (already > 0) {
    mh += `<div class="advisory">⚠ Advisory: ${already} complex${already === 1 ? "" : "es"} ${already === 1 ? "is" : "are"} already predicted to exceed ${thresh} m from existing licences alone (not attributable to this proposal).</div>`;
  }
  // The response may describe a single bore, several new bores (multi), or
  // a trade change set. Render a one-line summary of whichever shape we got.
  const wellsRun = result.wells_run || [];
  const stype = result.scenario_type || "single";
  if (stype === "trade" && wellsRun.length >= 2) {
    const toW = wellsRun.find((w) => w.rate_ML_per_year > 0);
    const fromW = wellsRun.find((w) => w.rate_ML_per_year < 0);
    mh += `<div><strong>Trade</strong> · ${Math.abs(toW.rate_ML_per_year).toFixed(0)} ML/yr</div>`;
    if (fromW) mh += `<div>from (${fromW.x.toFixed(0)}, ${fromW.y.toFixed(0)})</div>`;
    if (toW)   mh += `<div>to (${toW.x.toFixed(0)}, ${toW.y.toFixed(0)})</div>`;
    mh += `<div class="muted">runtime ${result.runtime_seconds.toFixed(1)}s</div>`;
  } else if (stype === "multi" && wellsRun.length > 1) {
    const total = wellsRun.reduce((s, w) => s + Math.max(0, w.rate_ML_per_year), 0);
    mh += `<div><strong>${wellsRun.length} bores</strong> · total ${total.toFixed(0)} ML/yr</div>`;
    mh += `<div class="muted">runtime ${result.runtime_seconds.toFixed(1)}s</div>`;
  } else if (result.proposed_bore) {
    const pb = result.proposed_bore;
    mh += `<div><strong>${pb.bore_id}</strong> · ${pb.rate_ML_per_year} ML/yr</div>`;
    mh += `<div>(${pb.x.toFixed(0)}, ${pb.y.toFixed(0)}) · ${result.runtime_seconds.toFixed(1)}s</div>`;
  }
  if (result.theis) {
    mh += `<div>Theis local T = ${result.theis.T_m2_per_day.toFixed(2)} m²/d, S = ${result.theis.S_dimensionless.toExponential(1)}</div>`;
  }
  mh += `<div><a href="scenario.html" target="_blank" rel="noopener" class="detail-link">View drawdown maps →</a></div>`;
  meta.innerHTML = mh;
}

function renderBarChart(result) {
  const lastYear = Math.max(...result.output_years);
  const yearBlock = result.by_year.find(y => y.time_years === lastYear);
  const ZERO_CUTOFF_M = 0.005;
  const allComplexes = [...yearBlock.complexes]
    .filter(c => c.s_total_m >= ZERO_CUTOFF_M)
    .sort((a, b) => b.s_total_m - a.s_total_m);
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
  const yl = make("text", {
    x: 14, y: margin.top + innerH / 2,
    "text-anchor": "middle", "font-size": 10, fill: "#52606d",
    transform: `rotate(-90 14 ${margin.top + innerH / 2})`,
  });
  yl.textContent = "drawdown (m)";
  svg.appendChild(yl);

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

  complexes.forEach((c, i) => {
    const x = margin.left + i * slot + (slot - barW) / 2;
    const triggered = c.triggered_by_proposed;
    const already = c.already_exceeded;
    const yApp = yScale(c.s_approved_m);
    const yTotal = yScale(c.s_total_m);
    const baseY = yScale(0);

    if (c.s_approved_m > 0) {
      const fill = already ? "#7c3aed" : "#475569";
      const r = make("rect", {
        x, y: yApp, width: barW, height: Math.max(0, baseY - yApp), fill,
        "data-id": c.complex_id,
      });
      r.appendChild(make_title(
        `${c.complex_id} · existing: ${fmt(c.s_approved_m)} m${already ? " (already exceeds)" : ""}`
      ));
      r.addEventListener("click", () => selectComplex(c.complex_id));
      svg.appendChild(r);
    }
    if (c.s_additional_m > 0) {
      const fill = triggered ? "#dc2626" : "#f59e0b";
      const r = make("rect", {
        x, y: yTotal, width: barW, height: Math.max(0, yApp - yTotal), fill,
        "data-id": c.complex_id,
      });
      const tag = triggered ? " (TRIGGERS)" : (already ? " (on top of existing exceedance)" : "");
      r.appendChild(make_title(
        `${c.complex_id} · proposed: +${fmt(c.s_additional_m)} m, total: ${fmt(c.s_total_m)} m${tag}`
      ));
      r.addEventListener("click", () => selectComplex(c.complex_id));
      svg.appendChild(r);
    }
    const labelX = x + barW / 2;
    const labelY = margin.top + innerH + 8;
    const labelColor = triggered ? "#991b1b" : already ? "#5b21b6" : "#1f2933";
    const t = make("text", {
      x: labelX, y: labelY,
      "text-anchor": "end", "font-size": 9.5, fill: labelColor,
      transform: `rotate(-50 ${labelX} ${labelY})`,
      "data-id": c.complex_id,
    });
    t.textContent = c.complex_id.length > 22 ? c.complex_id.slice(0, 21) + "…" : c.complex_id;
    t.addEventListener("click", () => selectComplex(c.complex_id));
    t.style.cursor = "pointer";
    svg.appendChild(t);
  });

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
  html += "<th>complex</th>";
  html += "<th class=\"num\">existing (m)</th>";
  html += "<th class=\"num\">proposed (m)</th>";
  if (hasTheis) html += "<th class=\"num\" title=\"Theis analytical estimate of proposed-bore drawdown\">Theis (m)</th>";
  html += "<th class=\"num\">total (m)</th>";
  html += "</tr></thead><tbody>";

  for (const c of all) {
    const rowClass = c.triggered_by_proposed ? "triggered-row"
                    : c.already_exceeded ? "already-row" : "";
    const cls = rowClass ? ` class="${rowClass}"` : "";
    html += `<tr${cls} data-id="${c.complex_id}">`;
    html += `<td>${c.complex_id}</td>`;
    html += `<td class="num">${fmt(c.s_approved_m)}</td>`;
    html += `<td class="num">${fmt(c.s_additional_m)}</td>`;
    if (hasTheis) html += `<td class="num">${fmt(c.s_additional_theis_m)}</td>`;
    html += `<td class="num"><strong>${fmt(c.s_total_m)}</strong></td>`;
    html += `</tr>`;
  }
  html += "</tbody></table>";
  $("results-tables").innerHTML = html;
  $("results-tables").querySelectorAll("tbody tr").forEach(tr => {
    tr.addEventListener("click", () => selectComplex(tr.getAttribute("data-id")));
  });
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

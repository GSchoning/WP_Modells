/* Frontend for the Precipice Sandstone water-licence impact API.
 *
 * Single-page MapLibre app: shows formation extent, outcrop, existing
 * bores, and spring complex centroids. Three scenario flavours:
 *   - single: click map to place one new bore.
 *   - multi:  click map to add bores; per-row rate inputs.
 *   - trade:  pick an existing bore by ID, click map for one or more
 *             destinations; rates must sum to ≤ source rate.
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
  lastJobId: null,        // job id of the last completed scenario run
  complexLngLat: {},      // complex_id -> [lng, lat] for fly-to
  selectedComplexId: null,
  scenarioType: "single", // "single" | "multi" | "trade"
  multiWells: [],         // [{x, y, lng, lat, rate_ML_per_year}]
  tradeFrom: null,        // {bore_id, x, y, lng, lat, rate_ML_per_year}
  tradeDestinations: [], // [{x, y, lng, lat, rate_ML_per_year}]
  existingBores: [],
};

function setStatus(msg, level = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = level;
}

// Pumping-bore marker radius as a function of extraction rate (m³/day).
// Area grows ~ with rate: stops are on a compressed (sqrt-like) scale so the
// median stock-and-domestic bore is a small dot and large licensed bores read
// clearly; rates above the top stop clamp to its radius (no monster dots).
const BORE_RADIUS_BY_RATE = [
  "interpolate", ["linear"], ["coalesce", ["get", "rate_m3_per_day"], 0],
  0, 2,  5, 3,  25, 5,  100, 8,  400, 12,
];

function borePopupHtml(p) {
  const m3d = Number(p.rate_m3_per_day) || 0;
  const mlyr = m3d * 365.25 / 1000;
  const id = p.bore_id != null ? GABORA.escapeHtml(String(p.bore_id)) : "(bore)";
  const lic = (p.licensed === true || p.licensed === "true")
    ? `<br/><span style="color:#3987e5;font-size:0.8em">licensed (entitlement)</span>`
    : `<br/><span style="color:#64748b;font-size:0.8em">S&amp;D / other take</span>`;
  return `<strong>${id}</strong><br/>extraction: ${mlyr.toFixed(1)} ML/yr` +
    `<br/><span style="color:#64748b;font-size:0.8em">${m3d.toFixed(1)} m³/day</span>${lic}`;
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

  const map = new maplibregl.Map({
    container: "map",
    style: GABORA.makeSatStyle({ roads: true, places: true }),
    bounds: mapData.bbox_4326,
    fitBoundsOptions: { padding: 30 },
  });
  STATE.map = map;

  map.on("load", () => {
    buildLayers(map, mapData);
    // Restore the previous scenario, if any, so navigating to the drawdown
    // maps and back doesn't reset the recommendation, bar chart, table, or
    // map markers. The result payload lives in sessionStorage.
    restoreSessionState(map);
  });

  $("scenario-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runScenario(map);
  });

  initModelSettings();

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
  // mirror the selection onto the map. Changing the source bore clears
  // the destinations list — they were rated against the previous source.
  $("trade-from").addEventListener("input", () => {
    const id = $("trade-from").value.trim();
    const b = STATE.existingBores.find((x) => String(x.bore_id) === id);
    const prevId = STATE.tradeFrom?.bore_id;
    if (b) {
      STATE.tradeFrom = b;
      $("trade-from-info").textContent =
        `${b.bore_id}: ${b.rate_ML_per_year.toFixed(0)} ML/yr at (${b.x.toFixed(0)}, ${b.y.toFixed(0)})`;
    } else {
      STATE.tradeFrom = null;
      $("trade-from-info").textContent = "";
    }
    if (b?.bore_id !== prevId) STATE.tradeDestinations = [];
    renderTradeDestinationsList();
  });

  window.addEventListener("resize", () => {
    if (STATE.lastResult) renderBarChart(STATE.lastResult);
    if (STATE.lastSeries) renderSeriesChart(STATE.lastSeries);
  });

  setupSplitter(map);
  renderMultiWellsList();
  renderTradeDestinationsList();

  // Decision panel + history wiring
  const approveBtn = document.getElementById("approve-btn");
  const rejectBtn  = document.getElementById("reject-btn");
  if (approveBtn) approveBtn.addEventListener("click", () => recordDecision("approve"));
  if (rejectBtn)  rejectBtn.addEventListener("click", () => recordDecision("reject"));

  const histBtn   = document.getElementById("history-btn");
  const histClose = document.getElementById("history-close");
  const histScrim = document.getElementById("history-scrim");
  if (histBtn)   histBtn.addEventListener("click", openHistoryPanel);
  if (histClose) histClose.addEventListener("click", closeHistoryPanel);
  if (histScrim) histScrim.addEventListener("click", closeHistoryPanel);
  const clearBtn = document.getElementById("clear-decisions-btn");
  if (clearBtn) clearBtn.addEventListener("click", clearAllDecisions);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("history-panel").hidden) {
      closeHistoryPanel();
    }
  });
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
        // Radius encodes extraction rate (area ~ rate): stops are chosen so
        // the median S&D bore stays a small dot and large licensed bores
        // stand out, with the biggest clamped at the top stop.
        "circle-radius": BORE_RADIUS_BY_RATE,
        // Licensed (entitlement) bores in blue, S&D/other take in grey —
        // matches the licensed/S&D split used in the bar chart.
        "circle-color": ["case", ["==", ["get", "licensed"], true], "#3987e5", "#94a3b8"],
        "circle-opacity": 0.75, "circle-stroke-color": "#1e293b",
        "circle-stroke-width": 0.4,
      } });
    map.on("click", "pumping-circles", (e) => {
      const p = e.features[0].properties || {};
      new maplibregl.Popup().setLngLat(e.lngLat)
        .setHTML(borePopupHtml(p)).addTo(map);
    });
    map.on("mouseenter", "pumping-circles", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "pumping-circles", () => { map.getCanvas().style.cursor = ""; });
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
        // Green = below threshold, red = triggered/exceeding.
        "circle-color": [
          "case",
          ["==", ["get", "exceeds_threshold"], true], "#dc2626",
          "#16a34a",
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
        .setHTML(`<strong>${GABORA.escapeHtml(p.complex_id)}</strong><br/>
          ${Number(p.n_springs) || 1} spring${p.n_springs == 1 ? "" : "s"}<br/>
          s_total = ${fmt(Number(p.s_total) || 0)} m
          ${flag}`)
        .addTo(map);
    });
  }
  map.addSource("proposed", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "proposed-circle", type: "circle", source: "proposed",
    paint: {
      "circle-radius": 9,
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

  // Pull road and town-label overlays back to the top so they stay
  // legible above the formation / outcrop fills and the recoloured
  // spring complexes.
  if (map.getLayer("roads"))  map.moveLayer("roads");
  if (map.getLayer("places")) map.moveLayer("places");

  setStatus(`ready — ${STATE.complexCount} spring complexes, click to place a bore`, "ok");
}

async function placeProposed(map, lng, lat) {
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
    if (!STATE.tradeFrom) {
      setStatus("pick the source bore first", "error");
      return;
    }
    // Default the new destination's rate to whatever's left of the source
    // licence so the first add takes the full rate by default.
    const sourceRate = STATE.tradeFrom.rate_ML_per_year;
    const used = STATE.tradeDestinations.reduce((s, d) => s + d.rate_ML_per_year, 0);
    const remaining = Math.max(0, sourceRate - used);
    const rate = remaining > 0.01 ? Number(remaining.toFixed(2)) : 0;
    STATE.tradeDestinations.push({ x, y, lng, lat, rate_ML_per_year: rate });
    renderTradeDestinationsList();
    setStatus(`added destination #${STATE.tradeDestinations.length} at (${x.toFixed(0)}, ${y.toFixed(0)})`, "ok");
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

function renderTradeDestinationsList() {
  const list = $("trade-destinations-list");
  if (!list) return;
  if (!STATE.tradeDestinations.length) {
    list.innerHTML = `<div class="multi-empty">No destinations yet — click map to add</div>`;
  } else {
    let html = "";
    STATE.tradeDestinations.forEach((d, i) => {
      html += `<div class="multi-well-row" data-i="${i}">
        <div class="well-coords">#${i + 1} · (${d.x.toFixed(0)}, ${d.y.toFixed(0)})</div>
        <input class="trade-rate-input" type="number" min="0" step="any" value="${d.rate_ML_per_year}" data-i="${i}" />
        <button type="button" class="remove-btn" data-i="${i}" title="Remove">&times;</button>
      </div>`;
    });
    list.innerHTML = html;
    list.querySelectorAll(".trade-rate-input").forEach((inp) => {
      inp.addEventListener("input", (e) => {
        const i = Number(e.target.dataset.i);
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v)) {
          STATE.tradeDestinations[i].rate_ML_per_year = v;
          renderTradeBalance();
        }
      });
    });
    list.querySelectorAll(".remove-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const i = Number(btn.dataset.i);
        STATE.tradeDestinations.splice(i, 1);
        renderTradeDestinationsList();
        refreshScenarioMarkers(STATE.map);
      });
    });
  }
  renderTradeBalance();
  refreshScenarioMarkers(STATE.map);
}

function renderTradeBalance() {
  const el = $("trade-balance");
  if (!el) return;
  if (!STATE.tradeFrom) {
    el.className = "trade-balance empty";
    el.textContent = "Pick a source bore to see its rate";
    return;
  }
  const sourceRate = STATE.tradeFrom.rate_ML_per_year;
  const used = STATE.tradeDestinations.reduce((s, d) => s + (d.rate_ML_per_year || 0), 0);
  const delta = sourceRate - used;
  let cls = "ok";
  if (used === 0) cls = "empty";
  else if (Math.abs(delta) <= sourceRate * 0.001) cls = "ok";
  else if (delta > 0) cls = "under";   // partial trade — some rate stays at source
  else cls = "over";                    // illegal — destinations exceed source
  el.className = `trade-balance ${cls}`;
  const sourceStr = sourceRate.toFixed(0);
  const usedStr = used.toFixed(0);
  let note;
  if (cls === "over")       note = `over by ${(-delta).toFixed(0)} ML/yr`;
  else if (cls === "under") note = `${delta.toFixed(0)} ML/yr stays at source`;
  else if (cls === "ok")    note = "fully transferred";
  else                       note = "no destinations yet";
  el.innerHTML = `<span>source ${sourceStr} ML/yr · destinations ${usedStr}</span><strong>${note}</strong>`;
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
    for (const d of STATE.tradeDestinations) {
      features.push({ type: "Feature", properties: { kind: "new" },
        geometry: { type: "Point", coordinates: [d.lng, d.lat] } });
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
    };
  } else if (STATE.scenarioType === "trade") {
    if (!STATE.tradeFrom) {
      setStatus("pick an existing bore to trade from", "error");
      return;
    }
    if (!STATE.tradeDestinations.length) {
      setStatus("add at least one trade destination (click map)", "error");
      return;
    }
    if (!STATE.tradeDestinations.every((d) => d.rate_ML_per_year > 0)) {
      setStatus("every destination needs a rate > 0", "error");
      return;
    }
    const sourceRate = STATE.tradeFrom.rate_ML_per_year;
    const used = STATE.tradeDestinations.reduce((s, d) => s + d.rate_ML_per_year, 0);
    if (used > sourceRate * 1.001) {
      setStatus(`destinations sum to ${used.toFixed(0)} ML/yr but source only carries ${sourceRate.toFixed(0)}`, "error");
      return;
    }
    body = {
      scenario_type: "trade",
      from_bore_id: STATE.tradeFrom.bore_id,
      to_wells: STATE.tradeDestinations.map((d) => ({
        x: d.x, y: d.y, rate_ML_per_year: d.rate_ML_per_year,
      })),
    };
  } else {
    setStatus("unknown scenario type", "error");
    return;
  }

  $("run-btn").disabled = true;
  $("run-btn").textContent = "Submitting…";
  setStatus("submitting scenario…");
  const t0 = performance.now();

  const fail = (msg) => {
    setStatus(msg, "error");
    $("run-btn").disabled = false;
    $("run-btn").textContent = "Run scenario";
  };

  // Submit as a background job, then poll. The server serialises MF6 runs,
  // so a busy model shows as "queued" rather than a hung request.
  let job;
  try {
    const resp = await fetch("/api/scenarios", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) return fail(`scenario rejected: ${await resp.text()}`);
    job = await resp.json();
  } catch (err) {
    return fail("network error");
  }

  const POLL_MS = 4000;
  let result = null;
  while (true) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    let status;
    try {
      const resp = await fetch(`/api/scenarios/jobs/${encodeURIComponent(job.job_id)}`);
      if (!resp.ok) return fail(`job lookup failed: HTTP ${resp.status}`);
      status = await resp.json();
    } catch (err) {
      continue;                       // transient network blip — keep polling
    }
    const mins = ((performance.now() - t0) / 60000).toFixed(1);
    if (status.status === "queued") {
      $("run-btn").textContent = `Queued… (${mins} min)`;
      setStatus("queued behind another scenario run");
    } else if (status.status === "running") {
      $("run-btn").textContent = `Running… (${mins} min)`;
      setStatus(`running: ${status.progress || "MODFLOW 6"}`);
    } else if (status.status === "error") {
      return fail(`scenario failed: ${status.error}`);
    } else if (status.status === "done") {
      result = status.result;
      break;
    }
  }

  const dt = ((performance.now() - t0) / 1000).toFixed(1);
  STATE.lastResult = result;
  STATE.lastJobId = result.job_id || job.job_id;
  setStatus(`done in ${dt}s`, result.n_exceedances_any_year > 0 ? "error" : "ok");
  $("run-btn").disabled = false;
  $("run-btn").textContent = "Run scenario";

  renderDecision(result);
  renderSummaryStats(result);
  renderBarChart(result);
  renderTable(result);
  recolorComplexes(map, result);

  saveSessionState();
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
    let popupHtml = `<strong>${GABORA.escapeHtml(id)}</strong>`;
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
        const licLine = (c.s_licensed_m != null && c.s_licensed_m > 0)
          ? `<br/>&nbsp;&nbsp;of which licensed take: ${fmt(c.s_licensed_m)} m`
          : "";
        popupHtml += `<br/>${c.n_springs} spring${c.n_springs == 1 ? "" : "s"}` +
          `<br/>existing: ${fmt(c.s_approved_m)} m` + licLine +
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
    const jobParam = STATE.lastJobId ? `&job=${encodeURIComponent(STATE.lastJobId)}` : "";
    const resp = await fetch(`/api/spring-series?complex_id=${encodeURIComponent(complexId)}${jobParam}`);
    if (!resp.ok) return;
    data = await resp.json();
  } catch (err) {
    return;
  }
  STATE.lastSeries = data;
  renderSeriesChart(data);
}

function renderSeriesChart(data) {
  const pane = $("series-pane");
  const svg = $("series-chart");
  const title = $("series-title");
  const legend = $("series-legend");
  if (!data || !data.times_years || !data.times_years.length) {
    pane.hidden = true;
    const tip = document.getElementById("series-tooltip");
    if (tip) tip.style.display = "none";
    return;
  }
  pane.hidden = false;
  const hasC = data.s_total_m != null;

  // First time the total series crosses the threshold — linearly
  // interpolated between the bracketing timesteps for a readable year.
  const thresholdForCross = data.threshold_m ?? 0.4;
  const crossSeries = hasC ? data.s_total_m : data.s_approved_m;
  let crossingText = "";
  const iCross = crossSeries.findIndex((v) => v >= thresholdForCross);
  if (iCross === 0) {
    crossingText = `<span class="crossing over">exceeds ${thresholdForCross.toFixed(2)} m from the first timestep</span>`;
  } else if (iCross > 0) {
    const t0c = data.times_years[iCross - 1], t1c = data.times_years[iCross];
    const v0 = crossSeries[iCross - 1], v1 = crossSeries[iCross];
    const tX = t0c + (thresholdForCross - v0) / (v1 - v0 || 1) * (t1c - t0c);
    crossingText = `<span class="crossing over">crosses ${thresholdForCross.toFixed(2)} m at ~${tX < 3 ? tX.toFixed(1) : Math.round(tX)} yr</span>`;
  } else {
    crossingText = `<span class="crossing under">stays under ${thresholdForCross.toFixed(2)} m</span>`;
  }

  title.innerHTML = `Drawdown over time · <strong>${GABORA.escapeHtml(data.complex_id)}</strong>` +
    (data.n_springs ? ` <span class="muted">(${data.n_springs} spring${data.n_springs === 1 ? "" : "s"})</span>` : "") +
    ` · ${crossingText}`;

  const W = svg.clientWidth || svg.parentElement.clientWidth;
  // Height comes from the CSS-laid-out SVG so it matches the table's
  // height in the lower-split row. Floor at 140 px so the chart is still
  // legible if the lower pane has been shrunk hard via the splitter.
  const H = Math.max(140, svg.clientHeight || 200);
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
  // viewBox sets the internal coordinate space; the SVG element's pixel
  // size comes from CSS (flex sizing inside #series-pane) so the chart
  // matches the table's height in the lower-split row.
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.removeAttribute("width");
  svg.removeAttribute("height");
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

  // --- Interactivity: crosshair + tooltip ----------------------------------
  const cross = make("g", {});
  cross.style.pointerEvents = "none";
  cross.style.display = "none";
  const crossLine = make("line", {
    x1: 0, x2: 0, y1: margin.top, y2: margin.top + innerH,
    stroke: "#0f172a", "stroke-width": 1, "stroke-dasharray": "3,3",
    opacity: 0.55,
  });
  cross.appendChild(crossLine);
  const dotA = make("circle", { r: 3.5, fill: "#475569", stroke: "#fff", "stroke-width": 1.2, cx: -10, cy: -10 });
  cross.appendChild(dotA);
  let dotT = null;
  if (hasC) {
    dotT = make("circle", { r: 3.5, fill: totColor, stroke: "#fff", "stroke-width": 1.2, cx: -10, cy: -10 });
    cross.appendChild(dotT);
  }
  svg.appendChild(cross);

  // Invisible rect that captures mouse events over the plot area.
  const overlay = make("rect", {
    x: margin.left, y: margin.top, width: innerW, height: innerH,
    fill: "transparent",
  });
  overlay.style.cursor = "crosshair";
  svg.appendChild(overlay);

  // Tooltip is an HTML element so we can use crisp un-stretched text.
  // Reuse a single tooltip across renders.
  let tip = document.getElementById("series-tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "series-tooltip";
    pane.appendChild(tip);
  }
  tip.style.display = "none";

  const fmtYear = (t) =>
    t < 1 ? `${(t * 365).toFixed(0)} days` :
    t < 5 ? `${t.toFixed(2)} yr` :
            `${Math.round(t)} yr`;

  overlay.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    if (!r.width) return;
    // viewBox-x at the cursor (W is the viewBox width).
    const xv = (e.clientX - r.left) * (W / r.width);
    const tFrac = Math.max(0, Math.min(1, (xv - margin.left) / innerW));
    const tHover = tMin + tFrac * (tMax - tMin);
    // Nearest sample.
    let idx = 0, best = Infinity;
    for (let i = 0; i < times.length; i++) {
      const d = Math.abs(times[i] - tHover);
      if (d < best) { best = d; idx = i; }
    }
    const cx = xScale(times[idx]);
    crossLine.setAttribute("x1", cx);
    crossLine.setAttribute("x2", cx);
    dotA.setAttribute("cx", cx);
    dotA.setAttribute("cy", yScale(seriesA[idx]));
    if (dotT) {
      dotT.setAttribute("cx", cx);
      dotT.setAttribute("cy", yScale(seriesT[idx]));
    }
    cross.style.display = "";

    // Tooltip position, in pane-relative pixels. Flip to the left of the
    // crosshair if it would otherwise overflow the right edge of the pane.
    const paneRect = pane.getBoundingClientRect();
    const cxScreenX = r.left + (cx / W) * r.width;
    const tipW = 160;
    let tx = cxScreenX - paneRect.left + 12;
    if (tx + tipW > paneRect.width) tx = cxScreenX - paneRect.left - tipW - 8;
    if (tx < 4) tx = 4;
    const ty = Math.max(4, e.clientY - paneRect.top - 18);
    tip.style.left = `${tx}px`;
    tip.style.top  = `${ty}px`;
    let html = `<div class="tip-year">${fmtYear(times[idx])}</div>`;
    html += `<div class="tip-row"><span class="swatch-line" style="background:#475569"></span>existing: <strong>${seriesA[idx].toFixed(3)} m</strong></div>`;
    if (hasC) {
      html += `<div class="tip-row"><span class="swatch-line" style="background:${totColor}"></span>total: <strong>${seriesT[idx].toFixed(3)} m</strong></div>`;
      const delta = seriesT[idx] - seriesA[idx];
      html += `<div class="tip-row tip-delta">proposal adds: <strong>${delta.toFixed(3)} m</strong></div>`;
    }
    const exceed = (hasC ? seriesT[idx] : seriesA[idx]) >= threshold;
    html += `<div class="tip-thresh ${exceed ? "over" : ""}">threshold ${threshold.toFixed(2)} m${exceed ? " — exceeded" : ""}</div>`;
    tip.innerHTML = html;
    tip.style.display = "";
  });
  overlay.addEventListener("mouseleave", () => {
    cross.style.display = "none";
    tip.style.display = "none";
  });
}

function renderDecision(result) {
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
  const wellsRun = result.wells_run || [];
  const stype = result.scenario_type || "single";
  if (stype === "trade" && wellsRun.length >= 2) {
    const dests = wellsRun.filter((w) => w.rate_ML_per_year > 0);
    const fromW = wellsRun.find((w) => w.rate_ML_per_year < 0);
    const totalOut = dests.reduce((s, d) => s + d.rate_ML_per_year, 0);
    mh += `<div><strong>Trade</strong> · ${totalOut.toFixed(0)} ML/yr across ${dests.length} destination${dests.length === 1 ? "" : "s"}</div>`;
    if (fromW) mh += `<div>from (${fromW.x.toFixed(0)}, ${fromW.y.toFixed(0)})</div>`;
    for (const d of dests) {
      mh += `<div>to (${d.x.toFixed(0)}, ${d.y.toFixed(0)}) · ${d.rate_ML_per_year.toFixed(0)} ML/yr</div>`;
    }
    mh += `<div class="muted">runtime ${result.runtime_seconds.toFixed(1)}s</div>`;
  } else if (stype === "multi" && wellsRun.length > 1) {
    const total = wellsRun.reduce((s, w) => s + Math.max(0, w.rate_ML_per_year), 0);
    mh += `<div><strong>${wellsRun.length} bores</strong> · total ${total.toFixed(0)} ML/yr</div>`;
    mh += `<div class="muted">runtime ${result.runtime_seconds.toFixed(1)}s</div>`;
  } else if (result.proposed_bore) {
    const pb = result.proposed_bore;
    mh += `<div><strong>${GABORA.escapeHtml(pb.bore_id)}</strong> · ${pb.rate_ML_per_year} ML/yr</div>`;
    mh += `<div>(${pb.x.toFixed(0)}, ${pb.y.toFixed(0)}) · ${result.runtime_seconds.toFixed(1)}s</div>`;
  }
  if (result.theis) {
    mh += `<div>Theis (formation-avg) T = ${result.theis.T_m2_per_day.toFixed(2)} m²/d, S = ${result.theis.S_dimensionless.toExponential(1)}</div>`;
  }

  // QA flags from the solver — surfaced so a questionable run can't
  // silently look authoritative.
  if (result.qa) {
    // Boundary drawdown stays in the QA payload/provenance but is not
    // surfaced as a banner (removed per user request 2026-08-10): with
    // the existing take in the baseline, most runs trip it through the
    // A-side drawdown rather than anything proposal-specific.
    if (result.qa.mass_balance_warning) {
      mh += `<div class="advisory">⚠ Mass balance: MF6 budget discrepancy ${result.qa.max_pct_discrepancy.toFixed(2)}% exceeds 1% — solver convergence is questionable for this run.</div>`;
    }
    if (result.qa.drain_warning) {
      mh += `<div class="advisory">⚠ Rejected-recharge linearisation: ${result.qa.n_drain_reversals} outcrop drain cell${result.qa.n_drain_reversals === 1 ? "" : "s"} drew below drain level — drawdown near those cells is under-predicted. Treat near-outcrop impacts as a lower bound.</div>`;
    }
  }
  // Provenance line: which config/data/binary produced this number.
  if (result.provenance) {
    const p = result.provenance;
    mh += `<div class="muted" style="font-size:0.68rem;margin-top:0.3rem" title="config ${GABORA.escapeHtml(p.config_sha256)} · properties ${GABORA.escapeHtml(p.properties_sha256)} · water use ${GABORA.escapeHtml(p.water_use_sha256)}">` +
      `run ${GABORA.escapeHtml(result.job_id || "")} · baseline ${GABORA.escapeHtml(p.baseline_cache_key || "")} · ${GABORA.escapeHtml(p.mf6_version || "")}</div>`;
  }
  const jobParam = result.job_id ? `?job=${encodeURIComponent(result.job_id)}` : "";
  // Same-tab navigation: the dashboard state is persisted in
  // sessionStorage and restored on return, so opening a second window
  // (which then multiplies on every "back") is never needed.
  mh += `<div><a href="${GABORA.withAquifer(`scenario.html${jobParam}`)}" class="detail-link">View drawdown maps →</a></div>`;
  meta.innerHTML = mh;

  // Show the approve/reject controls now that a scenario is on screen.
  const actions = $("decision-actions");
  if (actions) {
    actions.hidden = false;
    $("approve-btn").disabled = false;
    $("reject-btn").disabled = false;
    const recorded = $("decision-recorded");
    if (recorded) { recorded.hidden = true; recorded.textContent = ""; }
  }
}

// --- Decision history --------------------------------------------------

function regulatorName() {
  return sessionStorage.getItem("gabora_session") || "unknown";
}

function buildScenarioSnapshot(result) {
  const wellsRun = (result.wells_run || []).map((w) => ({
    label: w.label || "well",
    x: Number(w.x), y: Number(w.y),
    rate_ML_per_year: Number(w.rate_ML_per_year),
  }));
  let bore_label = null;
  if (result.proposed_bore?.bore_id) bore_label = result.proposed_bore.bore_id;
  let from_bore_id = null;
  if (result.scenario_type === "trade") {
    const fromW = (result.wells_run || []).find((w) => Number(w.rate_ML_per_year) < 0);
    if (fromW && typeof fromW.label === "string") {
      const m = fromW.label.match(/^from\s+(.+)$/);
      if (m) from_bore_id = m[1];
    }
  }
  return {
    scenario_type: result.scenario_type || "single",
    wells_run: wellsRun,
    from_bore_id,
    bore_label,
  };
}

function buildScenarioSummary(result) {
  return {
    n_exceedances_any_year: result.n_exceedances_any_year || 0,
    n_triggered_any_year: result.n_triggered_any_year || 0,
    n_already_exceeded_any_year: result.n_already_exceeded_any_year || 0,
    regulatory_threshold_m: result.regulatory_threshold_m,
    output_years: result.output_years || [],
    runtime_seconds: result.runtime_seconds ?? null,
  };
}

/* ---- Legislative-baseline rebuild feedback -------------------------------
 * Approvals, reversals, rollbacks and clears re-baseline the existing take
 * in the background. Surface that: banner + status, Run locked out, poll
 * /api/model-settings until the rebuild finishes. */
let REBUILD_POLLING = false;

async function pollLegislativeRebuild() {
  if (REBUILD_POLLING) return;
  REBUILD_POLLING = true;
  const banner = $("legislative-banner");
  if (banner) banner.hidden = false;
  $("run-btn").disabled = true;
  for (const id of ["storage-mode", "ic-source"]) {
    const el = $(id);
    if (el) el.disabled = true;
  }
  setStatus("updating the legislative scenario…", "busy");
  let s = null;
  for (;;) {
    await new Promise((res) => setTimeout(res, 4000));
    try {
      s = await (await fetch("/api/model-settings")).json();
    } catch (e) { continue; }
    if (!s.rebuilding) break;
  }
  if (banner) banner.hidden = true;
  $("run-btn").disabled = false;
  for (const id of ["storage-mode", "ic-source"]) {
    const el = $(id);
    if (el) el.disabled = false;
  }
  setStatus(
    s && s.rebuild_error
      ? `legislative update failed: ${s.rebuild_error}`
      : "legislative scenario updated — new runs use the revised baseline",
    s && s.rebuild_error ? "error" : "ok");
}

async function recordDecision(kind) {
  if (!STATE.lastResult) {
    setStatus("run a scenario before recording a decision", "error");
    return;
  }
  $("approve-btn").disabled = true;
  $("reject-btn").disabled = true;
  try {
    const body = {
      decision: kind,
      regulator: regulatorName(),
      note: "",
      scenario: buildScenarioSnapshot(STATE.lastResult),
      summary: buildScenarioSummary(STATE.lastResult),
    };
    const r = await fetch("/api/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const rec = await r.json();
    const recorded = $("decision-recorded");
    if (recorded) {
      recorded.hidden = false;
      recorded.textContent = `Recorded ${rec.decision} · ${rec.id}`;
      recorded.className = `decision-recorded ${kind}`;
    }
    setStatus(`decision recorded (${kind})`, "ok");
    // If the panel is open, refresh it.
    if (!$("history-panel").hidden) loadHistory();
    // Approvals join the legislative baseline — show the wait message
    // while the background re-baseline runs.
    if (kind === "approve") pollLegislativeRebuild();
  } catch (err) {
    console.error(err);
    setStatus(`failed to record decision: ${err.message}`, "error");
    $("approve-btn").disabled = false;
    $("reject-btn").disabled = false;
  }
}

async function loadHistory() {
  const list = $("history-list");
  list.innerHTML = `<div class="muted history-empty">loading…</div>`;
  try {
    const r = await fetch("/api/decisions");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderHistory(data);
  } catch (err) {
    list.innerHTML = `<div class="muted history-empty">failed to load: ${err.message}</div>`;
  }
}

function renderHistory(data) {
  const list = $("history-list");
  const decisions = data.decisions || [];
  $("history-count").textContent = decisions.length
    ? `${decisions.length} decision${decisions.length === 1 ? "" : "s"}`
    : "no decisions yet";
  $("history-head").textContent = data.active_head_id
    ? `head · ${data.active_head_id}`
    : "no active head";

  if (!decisions.length) {
    list.innerHTML = `<div class="muted history-empty">Approve a scenario to start the audit trail.</div>`;
    return;
  }
  let html = "";
  for (const d of decisions) {
    const isHead = d.id === data.active_head_id;
    const summary = d.summary || {};
    const scen = d.scenario || {};
    const wells = scen.wells_run || [];
    const totalAdd = wells.filter((w) => w.rate_ML_per_year > 0).reduce((s, w) => s + w.rate_ML_per_year, 0);
    let title = scen.bore_label || scen.from_bore_id || scen.scenario_type;
    if (scen.scenario_type === "trade" && scen.from_bore_id) {
      title = `Trade from ${scen.from_bore_id}`;
    } else if (scen.scenario_type === "multi") {
      title = `${wells.filter((w) => w.rate_ML_per_year > 0).length} bores`;
    } else if (scen.bore_label) {
      title = scen.bore_label;
    } else {
      title = scen.scenario_type;
    }
    const when = new Date(d.created_at).toLocaleString();
    const statusClass = d.status;
    const statusLabel = ({ active: "Active", rolled_back: "Rolled back", rejected: "Rejected", reversed: "Reversed" })[d.status] || d.status;
    const canRollback = d.decision === "approve" && d.status !== "active";
    const canReverse = d.decision === "approve" && d.status === "active";
    let rowBtns = "";
    if (canReverse) {
      rowBtns += `<button type="button" class="reverse-btn" data-id="${d.id}">Reverse</button>`;
    }
    if (canRollback) {
      rowBtns += `<button type="button" class="rollback-btn" data-id="${d.id}">Restore to here</button>`;
    }
    if (isHead) rowBtns += `<span class="head-chip">head</span>`;
    const rollbackBtn = rowBtns;
    html += `<div class="history-row ${statusClass}${isHead ? " head" : ""}">
      <div class="history-row-top">
        <span class="hist-id">${GABORA.escapeHtml(d.id)}</span>
        <span class="hist-status ${statusClass}">${statusLabel}</span>
      </div>
      <div class="hist-title">${GABORA.escapeHtml(title)}</div>
      <div class="hist-meta">
        ${totalAdd.toFixed(0)} ML/yr · ${summary.n_triggered_any_year || 0} triggered · ${summary.n_already_exceeded_any_year || 0} already-exceeded
      </div>
      <div class="hist-foot">
        <span class="muted">${when} · ${GABORA.escapeHtml(d.regulator)}</span>
        ${rollbackBtn}
      </div>
    </div>`;
  }
  list.innerHTML = html;
  list.querySelectorAll(".rollback-btn").forEach((btn) => {
    btn.addEventListener("click", () => rollbackTo(btn.dataset.id));
  });
  list.querySelectorAll(".reverse-btn").forEach((btn) => {
    btn.addEventListener("click", () => reverseDecision(btn.dataset.id));
  });
}

async function _decisionAction(url, confirmMsg, okMsg) {
  if (!confirm(confirmMsg)) return;
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ regulator: regulatorName() }),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail;
      throw new Error(detail || `HTTP ${r.status}`);
    }
    const data = await r.json();
    renderHistory(data);
    setStatus(okMsg, "ok");
    pollLegislativeRebuild();
  } catch (err) {
    setStatus(`action failed: ${err.message}`, "error");
  }
}

function rollbackTo(id) {
  _decisionAction(
    `/api/decisions/${encodeURIComponent(id)}/rollback`,
    `Restore the legislative state to ${id}? Any approvals after this point will be marked as rolled back and their take removed from the baseline.`,
    `legislative state restored to ${id}`);
}

function reverseDecision(id) {
  _decisionAction(
    `/api/decisions/${encodeURIComponent(id)}/reverse`,
    `Reverse ${id}? Its approved take is removed from the legislative baseline; other approvals stay active. The record remains in the audit trail.`,
    `${id} reversed`);
}

function clearAllDecisions() {
  _decisionAction(
    "/api/decisions/clear",
    "Clear ALL active approvals? The legislative baseline reverts to the raw water-use dataset. Records remain in the audit trail and can be restored individually.",
    "all approvals cleared");
}

function openHistoryPanel() {
  $("history-panel").hidden = false;
  $("history-scrim").hidden = false;
  loadHistory();
}
function closeHistoryPanel() {
  $("history-panel").hidden = true;
  $("history-scrim").hidden = true;
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
      // Split the existing (Scenario A) segment into licensed take (bottom,
      // dark) and S&D/other (top, light). s_licensed <= s_approved. When a
      // complex already exceeds the threshold, keep the purple flag colour
      // for the whole existing segment rather than splitting.
      const sLic = Math.min(c.s_licensed_m || 0, c.s_approved_m);
      const yLic = yScale(sLic);
      if (!already && sLic > 0) {
        const rL = make("rect", {
          x, y: yLic, width: barW, height: Math.max(0, baseY - yLic), fill: "#334155",
          "data-id": c.complex_id,
        });
        rL.appendChild(make_title(
          `${c.complex_id} · licensed take: ${fmt(sLic)} m of ${fmt(c.s_approved_m)} m existing`
        ));
        rL.addEventListener("click", () => selectComplex(c.complex_id));
        svg.appendChild(rL);
      }
      // Upper part: S&D/other existing (or the whole existing bar if already
      // exceeding, or no licensed split available).
      const yTop = (!already && sLic > 0) ? yLic : baseY;
      const fill = already ? "#7c3aed" : "#94a3b8";
      const r = make("rect", {
        x, y: yApp, width: barW, height: Math.max(0, yTop - yApp), fill,
        "data-id": c.complex_id,
      });
      r.appendChild(make_title(
        `${c.complex_id} · existing: ${fmt(c.s_approved_m)} m` +
        (!already && sLic > 0 ? ` (S&D/other ${fmt(c.s_approved_m - sLic)} m)` : "") +
        (already ? " (already exceeds)" : "")
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
    `dark slate = licensed take, light slate = S&D/other existing, ` +
    `amber = proposed, red = triggered by proposal, purple = already exceeded`;
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

  const hasLicensed = all.some(c => c.s_licensed_m != null && c.s_licensed_m > 0);
  const hasTheisCum = all.some(c => c.s_approved_theis_m != null);
  let html = "<table><thead><tr>";
  html += "<th>complex</th>";
  html += "<th class=\"num\">existing (m)</th>";
  if (hasTheisCum) html += "<th class=\"num\" title=\"Cumulative impact of ALL existing bores estimated by superposition of Theis solutions with the standard assessment parameters — the current-practice method, for comparison with the modelled existing column\">Theis existing (m)</th>";
  if (hasLicensed) html += "<th class=\"num\" title=\"Impact from licensed/entitlement take only (a subset of existing)\">licensed (m)</th>";
  html += "<th class=\"num\">proposed (m)</th>";
  if (hasTheis) html += "<th class=\"num\" title=\"Theis analytical estimate of proposed-bore drawdown, using formation-averaged T (geometric mean) and S (arithmetic mean) over active cells\">Theis (m)</th>";
  html += "<th class=\"num\">total (m)</th>";
  html += "</tr></thead><tbody>";

  for (const c of all) {
    const rowClass = c.triggered_by_proposed ? "triggered-row"
                    : c.already_exceeded ? "already-row" : "";
    const cls = rowClass ? ` class="${rowClass}"` : "";
    const safeId = GABORA.escapeHtml(c.complex_id);
    const meshMark = c.mesh_dependent
      ? ` <span class="mesh-flag" title="Within ~2 grid cells of a proposed bore — drawdown at this receptor is mesh-dependent and carries extra numerical uncertainty">†</span>`
      : "";
    html += `<tr${cls} data-id="${safeId}">`;
    html += `<td>${safeId}${meshMark}</td>`;
    html += `<td class="num">${fmt(c.s_approved_m)}</td>`;
    if (hasTheisCum) html += `<td class="num">${fmt(c.s_approved_theis_m)}</td>`;
    if (hasLicensed) html += `<td class="num">${fmt(c.s_licensed_m)}</td>`;
    html += `<td class="num">${fmt(c.s_additional_m)}</td>`;
    if (hasTheis) html += `<td class="num">${fmt(c.s_additional_theis_m)}</td>`;
    html += `<td class="num"><strong>${fmt(c.s_total_m)}</strong></td>`;
    html += `</tr>`;
  }
  html += "</tbody></table>";

  // Receptor-bore impacts (report-only — no threshold classification;
  // the bore trigger criterion is not yet confirmed). s_approved at an
  // extraction bore includes its own drawdown; the proposed column is
  // the decision-relevant number.
  const bores = yearBlock.bores || [];
  if (bores.length) {
    const hasLicB = bores.some(b => b.s_licensed_m != null && b.s_licensed_m > 0);
    html += `<h3 class="bores-heading">Receptor bores at ${lastYear} yr</h3>`;
    html += "<table><thead><tr><th>bore</th>";
    html += "<th class=\"num\">existing (m)</th>";
    if (hasLicB) html += "<th class=\"num\" title=\"Impact from licensed/entitlement take only\">licensed (m)</th>";
    html += "<th class=\"num\" title=\"Impact of the proposed change alone — no self-impact; the decision-relevant number\">proposed (m)</th>";
    html += "<th class=\"num\">total (m)</th></tr></thead><tbody>";
    for (const b of bores) {
      const meshMark = b.mesh_dependent
        ? ` <span class="mesh-flag" title="Within ~2 grid cells of a proposed bore — value is mesh-dependent">†</span>`
        : "";
      html += `<tr><td>${GABORA.escapeHtml(b.bore_id)}${meshMark}</td>`;
      html += `<td class="num">${fmt(b.s_approved_m)}</td>`;
      if (hasLicB) html += `<td class="num">${fmt(b.s_licensed_m)}</td>`;
      html += `<td class="num">${fmt(b.s_additional_m)}</td>`;
      html += `<td class="num"><strong>${fmt(b.s_total_m)}</strong></td></tr>`;
    }
    html += "</tbody></table>";
    html += `<div class="muted" style="margin-top:0.3rem">existing impact at an extraction bore includes its own drawdown; the proposed column carries no self-impact.</div>`;
  }

  $("results-tables").innerHTML =
    `<button type="button" id="csv-export-btn" class="csv-btn">Download CSV (all years)</button>` + html;
  $("results-tables").querySelectorAll("tbody tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => selectComplex(tr.getAttribute("data-id")));
  });
  $("csv-export-btn").addEventListener("click", () => exportResultCsv(result));
}

function exportResultCsv(result) {
  // Every complex × output year, all three reporting layers + flags —
  // the machine-readable version of what the regulator signs off on.
  const rows = [[
    "receptor_type", "receptor_id", "n_springs", "time_years",
    "s_approved_m", "s_approved_theis_m", "s_licensed_m", "s_additional_m", "s_total_m",
    "s_additional_theis_m",
    "exceeds_threshold", "already_exceeded", "triggered_by_proposed",
  ]];
  for (const yr of result.by_year) {
    for (const c of yr.complexes) {
      rows.push([
        "spring_complex", c.complex_id, c.n_springs, yr.time_years,
        c.s_approved_m, c.s_approved_theis_m ?? "", c.s_licensed_m ?? "",
        c.s_additional_m, c.s_total_m,
        c.s_additional_theis_m ?? "",
        c.exceeds_threshold, c.already_exceeded, c.triggered_by_proposed,
      ]);
    }
    for (const b of (yr.bores || [])) {
      rows.push([
        "receptor_bore", b.bore_id, "", yr.time_years,
        b.s_approved_m, "", b.s_licensed_m ?? "", b.s_additional_m, b.s_total_m,
        "", "", "", "",
      ]);
    }
  }
  const csv = rows.map((r) =>
    r.map((v) => {
      const s = String(v ?? "");
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(","),
  ).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const jobPart = result.job_id ? `_${result.job_id}` : "";
  a.download = `impact_assessment${jobPart}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

// --- Session persistence -------------------------------------------------
// The regulator can navigate to the drawdown-maps page and back; we want
// the recommendation, bar chart, table, and map markers to stay intact.
// `sessionStorage` is scoped to the tab and is cleared when the tab closes.

// Scoped per aquifer: the module pages share one tab (and thus one
// sessionStorage), so an unscoped key leaked the previous aquifer's
// scenario, markers and recommendation into the next one opened from
// the landing page.
const SCENARIO_STATE_KEY = `gabora_scenario_state:${GABORA.AQUIFER}`;

function saveSessionState() {
  if (!STATE.lastResult) return;
  const payload = {
    scenarioType: STATE.scenarioType,
    single: STATE.scenarioType === "single" ? {
      bore_id: $("bore_id").value,
      x: $("x").value,
      y: $("y").value,
      rate: $("rate").value,
    } : null,
    multiWells: STATE.multiWells || [],
    tradeFrom: STATE.tradeFrom ? { bore_id: STATE.tradeFrom.bore_id } : null,
    tradeDestinations: STATE.tradeDestinations || [],
    lastResult: STATE.lastResult,
    lastJobId: STATE.lastJobId || null,
    selectedComplexId: STATE.selectedComplexId || null,
  };
  try {
    sessionStorage.setItem(SCENARIO_STATE_KEY, JSON.stringify(payload));
  } catch (e) {
    // Quota / private mode — non-fatal.
    console.warn("could not persist scenario state:", e);
  }
}

async function restoreSessionState(map) {
  let saved;
  try {
    const raw = sessionStorage.getItem(SCENARIO_STATE_KEY);
    if (!raw) return;
    saved = JSON.parse(raw);
  } catch { return; }
  if (!saved || !saved.lastResult) return;

  // 1. Scenario type radio + visible pane.
  const wantType = saved.scenarioType || "single";
  STATE.scenarioType = wantType;
  const radio = document.querySelector(`input[name="scenario-type"][value="${wantType}"]`);
  if (radio) {
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // 2. Mode-specific input restoration.
  if (wantType === "single" && saved.single) {
    if (saved.single.bore_id != null) $("bore_id").value = saved.single.bore_id;
    if (saved.single.x != null)        $("x").value       = saved.single.x;
    if (saved.single.y != null)        $("y").value       = saved.single.y;
    if (saved.single.rate != null)     $("rate").value    = saved.single.rate;
  } else if (wantType === "multi") {
    STATE.multiWells = saved.multiWells || [];
    renderMultiWellsList();
  } else if (wantType === "trade") {
    if (STATE.existingBores.length === 0) {
      try { await loadExistingBores(); } catch { /* network blip */ }
    }
    if (saved.tradeFrom?.bore_id) {
      const tradeFromInput = $("trade-from");
      tradeFromInput.value = String(saved.tradeFrom.bore_id);
      // Dispatch 'input' so the existing handler resolves STATE.tradeFrom
      // from the existing-bores list and updates the info line.
      tradeFromInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
    STATE.tradeDestinations = saved.tradeDestinations || [];
    renderTradeDestinationsList();
  }
  refreshScenarioMarkers(map);

  // 3. Replay the result rendering.
  STATE.lastResult = saved.lastResult;
  STATE.lastJobId = saved.lastJobId || saved.lastResult.job_id || null;
  STATE.selectedComplexId = saved.selectedComplexId || null;
  renderDecision(STATE.lastResult);
  renderSummaryStats(STATE.lastResult);
  renderBarChart(STATE.lastResult);
  renderTable(STATE.lastResult);
  recolorComplexes(map, STATE.lastResult);
  setStatus("restored last scenario", "ok");
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

/* ---- Model settings: storage-formulation switch --------------------------
 * Baselines are cached per mode server-side, so flipping back to a mode
 * that has run before is near-instant; a first switch rebuilds the
 * baseline (two MF6 runs) while scenario runs queue behind it. */

function renderStorageInfo(s) {
  const info = $("storage-mode-info");
  if (s.rebuild_error) {
    info.className = "muted error";
    info.textContent = `switch failed: ${s.rebuild_error}`;
    return;
  }
  if (s.rebuilding) {
    info.className = "muted rebuilding";
    info.textContent =
      `Rebuilding the baseline (${s.storage_mode} storage, ` +
      `${s.ic_source === "parent_predev" ? "UWIR pre-development" : "steady-state"} ` +
      `heads)… minutes; scenarios wait for it.`;
    return;
  }
  info.className = "muted";
  const overrides = [];
  if (s.storage_mode !== s.config_default) overrides.push(`storage default: ${s.config_default}`);
  if (s.ic_source !== s.ic_config_default) overrides.push(`heads default: ${s.ic_config_default}`);
  info.textContent = overrides.length
    ? `Session override (${overrides.join(", ")}).`
    : "";
}

async function initModelSettings() {
  const selStor = $("storage-mode");
  const selIc = $("ic-source");
  if (!selStor) return;
  let s;
  try {
    s = await (await fetch("/api/model-settings")).json();
  } catch (e) {
    $("model-settings").hidden = true;
    return;
  }
  selStor.value = s.storage_mode;
  selIc.value = s.ic_source;
  if (!s.ic_parent_available) {
    selIc.querySelector('option[value="parent_predev"]').disabled = true;
  }
  renderStorageInfo(s);
  if (s.rebuilding) pollModelSettings();    // a switch was already in flight

  async function postChange(body, sel, prev) {
    try {
      const r = await fetch("/api/model-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      s = await r.json();
      renderStorageInfo(s);
      if (s.rebuilding) pollModelSettings();
      else setStatus(`model settings: ${s.storage_mode} / ${s.ic_source}`);
    } catch (err) {
      sel.value = prev;
      const info = $("storage-mode-info");
      info.className = "muted error";
      info.textContent = `switch failed: ${err.message}`;
    }
  }
  selStor.addEventListener("change", () =>
    postChange({ storage_mode: selStor.value }, selStor, s.storage_mode));
  selIc.addEventListener("change", () =>
    postChange({ ic_source: selIc.value }, selIc, s.ic_source));

  async function pollModelSettings() {
    selStor.disabled = true;
    selIc.disabled = true;
    $("run-btn").disabled = true;
    setStatus("rebuilding baseline…", "busy");
    for (;;) {
      await new Promise((res) => setTimeout(res, 4000));
      try {
        s = await (await fetch("/api/model-settings")).json();
      } catch (e) { continue; }
      renderStorageInfo(s);
      if (!s.rebuilding) break;
    }
    selStor.disabled = false;
    selIc.disabled = false;
    $("run-btn").disabled = false;
    selStor.value = s.storage_mode;
    selIc.value = s.ic_source;
    setStatus(s.rebuild_error
      ? "model-settings switch failed"
      : `baseline ready (${s.storage_mode} / ${s.ic_source})`,
      s.rebuild_error ? "error" : undefined);
  }
}

window.addEventListener("DOMContentLoaded", init);

/* Drawdown-maps page: side-by-side cumulative + proposed-only rasters
 * for the most recent Scenario C run.
 */

const $ = (id) => document.getElementById(id);

const SAT_STYLE = GABORA.makeSatStyle({ roads: true, places: true });

// Optional ?job=<id> pins this page to a specific scenario run, so two
// regulators viewing maps concurrently each see their own result.
const JOB_ID = new URLSearchParams(window.location.search).get("job");
const jobQS = () => (JOB_ID ? `&job=${encodeURIComponent(JOB_ID)}` : "");

const STATE = {
  info: null,
  mapData: null,             // /api/map-data payload (formation, outcrop, bores, springs)
  maps: {},                  // layer -> map
  opacity: 0.7,
};

async function init() {
  let info, mapData;
  try {
    [info, mapData] = await Promise.all([
      fetch(`/api/last-scenario/info${JOB_ID ? `?job=${encodeURIComponent(JOB_ID)}` : ""}`).then((r) => r.json()),
      fetch("/api/map-data").then((r) => r.json()),
    ]);
  } catch (e) {
    showEmpty("Backend unreachable.");
    return;
  }
  if (!info.available) {
    showEmpty("No scenario has been run yet. Go back and run one first.");
    return;
  }
  STATE.info = info;
  STATE.mapData = mapData;

  const meta = $("scenario-meta");
  const wells = info.wells || [];
  const proposed = wells.filter((w) => w.rate_ML_per_year > 0);
  const fromW = wells.find((w) => w.rate_ML_per_year < 0);
  if (proposed.length > 1) {
    const totalRate = proposed.reduce((s, w) => s + w.rate_ML_per_year, 0);
    meta.textContent = fromW
      ? `Trade · ${proposed.length} destinations · ${totalRate.toFixed(0)} ML/yr from ${fromW.label.replace(/^from\s+/, "")}`
      : `${proposed.length} bores · ${totalRate.toFixed(0)} ML/yr total`;
  } else {
    meta.textContent =
      `${info.bore.bore_id} · ${info.bore.rate_ML_per_year} ML/yr · ` +   // textContent — no escaping needed
      `(${info.bore.x.toFixed(0)}, ${info.bore.y.toFixed(0)})`;
  }

  // Year selector — default to the latest year
  const sel = $("year-select");
  for (const y of info.years) {
    const opt = document.createElement("option");
    opt.value = y; opt.textContent = `${Number(y).toFixed(0)} yr`;
    sel.appendChild(opt);
  }
  sel.value = info.years[info.years.length - 1];
  sel.addEventListener("change", () => {
    updateOverlay("cumulative", sel.value);
    updateOverlay("additional", sel.value);
    updateSpringImpacts(sel.value);
  });

  const opacity = $("opacity-slider");
  opacity.addEventListener("input", () => {
    STATE.opacity = Number(opacity.value) / 100;
    $("opacity-value").textContent = `${opacity.value}%`;
    for (const layer of ["cumulative", "additional"]) {
      const m = STATE.maps[layer];
      if (m && m.getLayer("dd")) m.setPaintProperty("dd", "raster-opacity", STATE.opacity);
    }
  });

  STATE.maps.cumulative = createMap("map-cumulative", "cumulative", sel.value);
  STATE.maps.additional = createMap("map-additional", "additional", sel.value);
}

function showEmpty(msg) {
  const maps = $("maps");
  maps.innerHTML = `<div id="empty-banner">${msg}</div>`;
}

function createMap(elementId, layer, year) {
  const map = new maplibregl.Map({
    container: elementId,
    style: SAT_STYLE,
    bounds: STATE.info.bbox_4326,
    fitBoundsOptions: { padding: 30 },
  });
  map.on("load", () => {
    map.addSource("dd", {
      type: "image",
      // MapLibre image sources bypass the patched window.fetch, so the
      // aquifer param must be baked into the URL explicitly.
      url: GABORA.withAquifer(`/api/last-scenario/drawdown.png?layer=${layer}&year=${year}${jobQS()}`),
      coordinates: STATE.info.image_corners_4326,
    });
    // Insert the drawdown raster BELOW the roads/places labels so towns
    // and major roads remain legible through the overlay.
    map.addLayer({
      id: "dd", type: "raster", source: "dd",
      paint: { "raster-opacity": STATE.opacity, "raster-fade-duration": 0 },
    }, "roads");

    addContextLayers(map);
    addWellMarkers(map);

    // Click-to-sample: query the underlying grid value at the clicked
    // point. Server reprojects EPSG:4326 to project CRS and looks up
    // the cell drawdown.
    map.on("click", async (e) => {
      const yr = $("year-select").value;
      const url = `/api/last-scenario/drawdown/sample?lng=${e.lngLat.lng}&lat=${e.lngLat.lat}&layer=${layer}&year=${yr}${jobQS()}`;
      let resp;
      try { resp = await fetch(url); } catch { return; }
      if (!resp.ok) return;
      const d = await resp.json();
      let html;
      if (!d.in_domain) {
        html = "<em>outside model domain</em>";
      } else {
        const headerLine = layer === "cumulative"
          ? "Cumulative drawdown"
          : "Proposed-only drawdown";
        const breakdown = layer === "cumulative" && d.s_approved_m != null
          ? `<div class="muted-pop">existing ${d.s_approved_m.toFixed(2)} m + proposed ${d.s_additional_m.toFixed(2)} m</div>`
          : "";
        html = `<div><strong>${headerLine}</strong></div>` +
               `<div style="font-size:1.1rem;margin:0.2rem 0">${d.drawdown_m.toFixed(2)} m</div>` +
               breakdown +
               `<div class="muted-pop">cell (${d.row}, ${d.col}) · ${yr} yr</div>`;
      }
      new maplibregl.Popup({ closeButton: true })
        .setLngLat(e.lngLat).setHTML(html).addTo(map);
    });
    map.getCanvas().style.cursor = "crosshair";
  });
  return map;
}

// Merge the scenario's per-complex results for `year` into the spring
// centroid geojson, so the layer paint expressions can classify them.
function springsWithImpacts(year) {
  const base = STATE.mapData.spring_complexes;
  const byYear = (STATE.info && STATE.info.complexes_by_year) || {};
  // keys are stringified floats ("100.0"); match numerically.
  const key = Object.keys(byYear).find((k) => Math.abs(Number(k) - Number(year)) < 1e-6);
  const rows = key ? byYear[key] : [];
  const lookup = new Map(rows.map((r) => [String(r.complex_id), r]));
  return {
    type: "FeatureCollection",
    features: (base.features || []).map((f) => {
      const r = lookup.get(String(f.properties?.complex_id));
      return {
        ...f,
        properties: {
          ...f.properties,
          s_total: r ? r.s_total : null,
          exceeds_threshold: r ? r.exceeds_threshold : false,
          already_exceeded: r ? r.already_exceeded : false,
          triggered_by_proposed: r ? r.triggered_by_proposed : false,
        },
      };
    }),
  };
}

function updateSpringImpacts(year) {
  const data = springsWithImpacts(year);
  for (const layer of ["cumulative", "additional"]) {
    const m = STATE.maps[layer];
    const src = m && m.getSource && m.getSource("springs");
    if (src) src.setData(data);
  }
}

function addContextLayers(map) {
  const md = STATE.mapData;
  if (!md) return;

  // Outcrop polygon — orange tint, drawn first so it sits below points.
  if (md.outcrop && md.outcrop.features && md.outcrop.features.length) {
    map.addSource("outcrop", { type: "geojson", data: md.outcrop });
    map.addLayer({
      id: "outcrop-fill", type: "fill", source: "outcrop",
      paint: { "fill-color": "#f59e0b", "fill-opacity": 0.18 },
    });
    map.addLayer({
      id: "outcrop-line", type: "line", source: "outcrop",
      paint: { "line-color": "#b45309", "line-width": 1.2, "line-opacity": 0.7 },
    });
  }

  // Spring complexes — coloured by scenario impact at the selected year,
  // exactly like the dashboard (red = exceeds threshold).
  if (md.spring_complexes && md.spring_complexes.features) {
    map.addSource("springs", { type: "geojson", data: springsWithImpacts($("year-select").value) });
    map.addLayer({
      id: "springs-pt", type: "circle", source: "springs",
      paint: {
        "circle-radius": [
          "interpolate", ["linear"],
          ["coalesce", ["get", "n_springs"], 1],
          1, 4, 10, 7, 50, 10,
        ],
        // Green = below threshold, red = triggered/exceeding (matches the
        // dashboard).
        "circle-color": [
          "case",
          ["==", ["get", "exceeds_threshold"], true], "#dc2626",
          "#16a34a",
        ],
        "circle-stroke-color": "#fff",
        "circle-stroke-width": 1.2,
        "circle-opacity": 0.95,
      },
    });
    map.on("click", "springs-pt", (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      const p = f.properties || {};
      const name = GABORA.escapeHtml(p.complex_id || p.name || "spring complex");
      const n = p.n_springs ? `<div class="muted-pop">${p.n_springs} member spring${p.n_springs === 1 ? "" : "s"}</div>` : "";
      const s = p.s_total != null
        ? `<div class="muted-pop">s_total ${Number(p.s_total).toFixed(2)} m @ ${$("year-select").value} yr</div>` : "";
      const flag = (p.exceeds_threshold === true || p.exceeds_threshold === "true")
        ? `<div style="color:#dc2626;font-weight:600">exceeds threshold</div>` : "";
      new maplibregl.Popup({ closeButton: true })
        .setLngLat(e.lngLat)
        .setHTML(`<div><strong>${name}</strong></div>${s}${flag}${n}`)
        .addTo(map);
    });
  }

  // Existing pumping bores — sized by extraction rate; licensed
  // (entitlement) bores in blue, S&D/other take in grey (as on the
  // dashboard).
  if (md.pumping_bores && md.pumping_bores.features) {
    map.addSource("pumping", { type: "geojson", data: md.pumping_bores });
    map.addLayer({
      id: "pumping-pt", type: "circle", source: "pumping",
      paint: {
        "circle-radius": [
          "interpolate", ["linear"], ["coalesce", ["get", "rate_m3_per_day"], 0],
          0, 2, 5, 3, 25, 5, 100, 8, 400, 12,
        ],
        "circle-color": ["case", ["==", ["get", "licensed"], true], "#3987e5", "#94a3b8"],
        "circle-stroke-color": "#1e293b",
        "circle-stroke-width": 0.4,
        "circle-opacity": 0.75,
      },
    });
    map.on("click", "pumping-pt", (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      const p = f.properties || {};
      const id = p.bore_id ? `<strong>${GABORA.escapeHtml(p.bore_id)}</strong>` : "existing bore";
      const rate = p.rate_m3_per_day != null
        ? `<div class="muted-pop">${(p.rate_m3_per_day * 365.25 / 1000).toFixed(0)} ML/yr</div>`
        : "";
      const lic = (p.licensed === true || p.licensed === "true")
        ? `<div class="muted-pop" style="color:#3987e5">licensed (entitlement)</div>`
        : `<div class="muted-pop">S&amp;D / other take</div>`;
      new maplibregl.Popup({ closeButton: true })
        .setLngLat(e.lngLat).setHTML(`<div>${id}</div>${rate}${lic}`)
        .addTo(map);
    });
  }

  // Pull roads + town labels above the context fills so they stay
  // readable on top of the outcrop polygon.
  GABORA.raiseReferenceLayers(map);
}

function addWellMarkers(map) {
  const wells = (STATE.info && STATE.info.wells) || [];
  if (!wells.length && STATE.info && STATE.info.bore) {
    // Legacy single-bore fallback.
    new maplibregl.Marker({ color: "#f59e0b" })
      .setLngLat([STATE.info.bore.lng, STATE.info.bore.lat])
      .setPopup(new maplibregl.Popup().setHTML(
        `<strong>${GABORA.escapeHtml(STATE.info.bore.bore_id)}</strong><br/>${STATE.info.bore.rate_ML_per_year} ML/yr`
      )).addTo(map);
    return;
  }
  for (const w of wells) {
    const isFrom = w.rate_ML_per_year < 0;
    // Orange = new proposed extraction, sky-blue = trade source (reduction).
    const colour = isFrom ? "#0284c7" : "#f59e0b";
    const role   = isFrom ? "trade source" : "proposed extraction";
    const label  = GABORA.escapeHtml(w.label || "bore");
    const rate   = `${Math.abs(w.rate_ML_per_year).toFixed(0)} ML/yr`;
    new maplibregl.Marker({ color: colour })
      .setLngLat([w.lng, w.lat])
      .setPopup(new maplibregl.Popup().setHTML(
        `<strong>${label}</strong><br/>${role} · ${rate}`
      )).addTo(map);
  }
}

function updateOverlay(layer, year) {
  const map = STATE.maps[layer];
  if (!map || !map.getSource("dd")) return;
  // Same as createMap: MapLibre image requests bypass the patched
  // window.fetch, so the aquifer param must be in the URL itself.
  map.getSource("dd").updateImage({
    url: GABORA.withAquifer(`/api/last-scenario/drawdown.png?layer=${layer}&year=${year}${jobQS()}`),
    coordinates: STATE.info.image_corners_4326,
  });
}

window.addEventListener("DOMContentLoaded", init);

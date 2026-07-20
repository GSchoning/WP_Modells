/* Model setup page: shows the modelled grid, boundaries, recharge zone,
 * pumping bores, and spring complexes on the satellite basemap.
 */

const $ = (id) => document.getElementById(id);

const SAT_STYLE = GABORA.makeSatStyle({ roads: true, places: true });

// Per-layer vector polygon configuration. Colours match the legend swatches
// in setup.css. Each layer renders the cell-square polygons fetched from
// /api/model-setup/<layer>.geojson.
const VECTOR_LAYERS = [
  { id: "active",  url: "/api/model-setup/active.geojson",  fill: "#9ca3af", stroke: "#475569", opacity: 0.25, strokeOpacity: 0.0 },
  { id: "outcrop", url: "/api/model-setup/outcrop.geojson", fill: "#10b981", stroke: "#047857", opacity: 0.55, strokeOpacity: 0.0 },
  // Rejected-recharge drains: full opacity where flowing at steady state
  // (linearised into GHBs for the transients), faint where dry.
  { id: "drains",  url: "/api/model-setup/drains.geojson",  fill: "#d97706", stroke: "#92400e",
    opacity: ["case", ["boolean", ["get", "flowing"], false], 0.85, 0.25], strokeOpacity: 0.4 },
  // Far-field boundaries, now split: GHB (uwir_ghb mode, western strip)
  // vs CHD (legacy modes / convergence fallback — normally empty).
  { id: "ghb",     url: "/api/model-setup/ghb.geojson",     fill: "#38bdf8", stroke: "#075985", opacity: 0.9, strokeOpacity: 0.7 },
  { id: "chd",     url: "/api/model-setup/chd.geojson",     fill: "#dc2626", stroke: "#7f1d1d", opacity: 0.85, strokeOpacity: 0.6 },
  { id: "noflow",  url: "/api/model-setup/noflow.geojson",  fill: "#1f2937", stroke: "#000000", opacity: 0.85, strokeOpacity: 0.6 },
];

async function init() {
  let info, mapData;
  try {
    [info, mapData] = await Promise.all([
      (await fetch("/api/model-setup/info")).json(),
      (await fetch("/api/map-data")).json(),
    ]);
  } catch (e) {
    $("setup-meta").textContent = "backend unreachable";
    return;
  }

  $("setup-meta").textContent =
    `${info.grid.nrow} × ${info.grid.ncol} grid, ${info.grid.dx_m.toFixed(0)} m cells · ` +
    `${info.grid.n_active_cells.toLocaleString()} active cells`;

  const b = info.boundaries;
  $("counts").innerHTML =
    `<div><span class="swatch active" style="vertical-align:middle"></span> ` +
    `${info.grid.n_active_cells.toLocaleString()} active cells</div>` +
    `<div><span class="swatch outcrop" style="vertical-align:middle"></span> ` +
    `${info.grid.n_outcrop_cells.toLocaleString()} outcrop / recharge cells</div>` +
    `<div><span class="swatch drains" style="vertical-align:middle"></span> ` +
    `${(b.n_drain_cells ?? 0).toLocaleString()} drain cells ` +
    `(${(b.n_drains_flowing ?? 0).toLocaleString()} flowing)</div>` +
    `<div><span class="swatch ghb" style="vertical-align:middle"></span> ` +
    `${(b.n_ghb_cells ?? 0).toLocaleString()} GHB far-field cells</div>` +
    `<div><span class="swatch chd" style="vertical-align:middle"></span> ` +
    `${b.n_chd_cells.toLocaleString()} CHD cells</div>` +
    `<div><span class="swatch noflow" style="vertical-align:middle"></span> ` +
    `${b.n_noflow_boundary_cells.toLocaleString()} no-flow boundary cells</div>` +
    `<div class="muted" style="margin-top:0.4rem">recharge multiplier: ${info.recharge_multiplier}</div>`;

  const map = new maplibregl.Map({
    container: "map",
    style: SAT_STYLE,
    bounds: info.bbox_4326,
    fitBoundsOptions: { padding: 30 },
  });

  map.on("load", async () => {
    // Vector layers — fetch once, add as GeoJSON sources for crisp
    // rendering at any zoom (vs. the previous PNG raster overlays).
    for (const { id, url, fill, stroke, opacity, strokeOpacity } of VECTOR_LAYERS) {
      let data;
      try {
        data = await (await fetch(url)).json();
      } catch (err) {
        console.warn("failed to load layer", id, err);
        continue;
      }
      map.addSource(`vec-${id}`, { type: "geojson", data });
      map.addLayer({
        id: `vec-${id}-fill`, type: "fill", source: `vec-${id}`,
        paint: { "fill-color": fill, "fill-opacity": opacity },
        layout: { visibility: $(`toggle-${id}`).checked ? "visible" : "none" },
      });
      map.addLayer({
        id: `vec-${id}-line`, type: "line", source: `vec-${id}`,
        paint: { "line-color": stroke, "line-width": 0.4, "line-opacity": strokeOpacity },
        layout: { visibility: $(`toggle-${id}`).checked ? "visible" : "none" },
      });
    }

    // Pumping bores (vector layer from /map-data). Marker radius encodes
    // extraction rate (area ~ rate) on a compressed scale so small S&D bores
    // stay dots and large licensed bores stand out; big rates clamp at the
    // top stop.
    if (mapData.pumping_bores) {
      map.addSource("pumping", { type: "geojson", data: mapData.pumping_bores });
      map.addLayer({
        id: "pumping", type: "circle", source: "pumping",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["coalesce", ["get", "rate_m3_per_day"], 0],
            0, 2,  5, 3,  25, 5,  100, 8,  400, 12,
          ],
          "circle-color": "#ef4444",
          "circle-opacity": 0.85, "circle-stroke-color": "#7f1d1d",
          "circle-stroke-width": 0.5,
        },
        layout: { visibility: $("toggle-bores").checked ? "visible" : "none" },
      });
      map.on("click", "pumping", (e) => {
        const p = e.features[0].properties || {};
        const m3d = Number(p.rate_m3_per_day) || 0;
        const mlyr = m3d * 365.25 / 1000;
        const id = p.bore_id != null ? GABORA.escapeHtml(String(p.bore_id)) : "(bore)";
        new maplibregl.Popup().setLngLat(e.lngLat)
          .setHTML(`<strong>${id}</strong><br/>extraction: ${mlyr.toFixed(1)} ML/yr`
                   + `<br/><span style="color:#64748b;font-size:0.8em">${m3d.toFixed(1)} m³/day</span>`)
          .addTo(map);
      });
      map.on("mouseenter", "pumping", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "pumping", () => { map.getCanvas().style.cursor = ""; });
    }
    // Spring complex centroids.
    if (mapData.spring_complexes) {
      map.addSource("complexes", { type: "geojson", data: mapData.spring_complexes });
      map.addLayer({
        id: "complexes", type: "circle", source: "complexes",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["coalesce", ["get", "n_springs"], 1],
            1, 4, 10, 7, 50, 11,
          ],
          "circle-color": "#2563eb",
          "circle-stroke-color": "#fff", "circle-stroke-width": 1.2,
        },
        layout: { visibility: $("toggle-complexes").checked ? "visible" : "none" },
      });
      map.on("click", "complexes", (e) => {
        const p = e.features[0].properties || {};
        new maplibregl.Popup().setLngLat(e.lngLat)
          .setHTML(`<strong>${GABORA.escapeHtml(p.complex_id)}</strong><br/>${Number(p.n_springs) || 1} member springs`)
          .addTo(map);
      });
    }

    // Keep road and town labels above the grid/outcrop polygon fills.
    GABORA.raiseReferenceLayers(map);

    // Calibrated-field overlays (K and Ss): fetched lazily the first time
    // the regulator toggles them on. PNG raster overlays on the same
    // image_corners_4326 the drawdown maps use.
    setupPropertyOverlay(map, info, "k",  "Hydraulic conductivity", "viridis", "m/d");
    setupPropertyOverlay(map, info, "ss", "Specific storage",       "plasma",  "1/m");

    // Click-to-sample: when either property layer is visible, look up
    // the underlying cell value(s) at the clicked location and show a
    // popup. Works for K and Ss together if both are toggled on.
    map.on("click", (e) => samplePropertiesAt(map, e.lngLat));
    // Crosshair cursor whenever a property layer is visible, hand cursor
    // over interactive geojson layers (handled by maplibre's defaults).
    const updateCursor = () => {
      const propVisible = ["prop-k-layer", "prop-ss-layer"].some((l) =>
        map.getLayer(l) && map.getLayoutProperty(l, "visibility") !== "none",
      );
      map.getCanvas().style.cursor = propVisible ? "crosshair" : "";
    };
    map.on("idle", updateCursor);
  });

  // Wire layer toggles.
  const toggle = (id, layers) => {
    const cb = $(id);
    if (!cb) return;
    cb.addEventListener("change", () => {
      for (const l of layers) {
        if (map.getLayer(l)) {
          map.setLayoutProperty(l, "visibility", cb.checked ? "visible" : "none");
        }
      }
    });
  };
  toggle("toggle-active",    ["vec-active-fill",  "vec-active-line"]);
  toggle("toggle-outcrop",   ["vec-outcrop-fill", "vec-outcrop-line"]);
  toggle("toggle-drains",    ["vec-drains-fill",  "vec-drains-line"]);
  toggle("toggle-ghb",       ["vec-ghb-fill",     "vec-ghb-line"]);
  toggle("toggle-chd",       ["vec-chd-fill",     "vec-chd-line"]);
  toggle("toggle-noflow",    ["vec-noflow-fill",  "vec-noflow-line"]);
  toggle("toggle-bores",     ["pumping"]);
  toggle("toggle-complexes", ["complexes"]);
}

/**
 * Wire a K- or Ss-field overlay. The PNG is added below the roads/labels
 * layers so towns stay readable, and only fetched the first time the
 * checkbox is toggled on (so it doesn't slow the initial page load).
 */
function setupPropertyOverlay(map, info, name, label, rampClass, units) {
  const checkbox = document.getElementById(`toggle-${name}`);
  const legend   = document.getElementById(`${name}-legend`);
  if (!checkbox || !info?.image_corners_4326) return;

  const sourceId = `prop-${name}`;
  const layerId  = `prop-${name}-layer`;
  const fmt = (v) =>
    v == null ? "–" :
    Math.abs(v) >= 0.01 && Math.abs(v) < 1000 ? Number(v).toPrecision(3) :
    Number(v).toExponential(2);

  let loaded = false;
  const ensureLoaded = async () => {
    if (loaded) return true;
    // Property /info now also carries the WGS84-axis-aligned image
    // corners of the warped raster — they're different from the grid's
    // trapezoidal MGA corners returned by /model-setup/info, and using
    // them is what makes the K / Ss raster align with the active-cells
    // GeoJSON instead of drifting in the interior.
    let corners = info.image_corners_4326;
    try {
      const meta = await (await fetch(`/api/model-setup/property/${name}/info`)).json();
      if (Array.isArray(meta.image_corners_4326)) corners = meta.image_corners_4326;
      legend.innerHTML =
        `<span>${fmt(meta.vmin)}</span>` +
        `<div class="ramp-bar ${rampClass}"></div>` +
        `<span>${fmt(meta.vmax)}</span>` +
        `<span class="muted">${meta.units}</span>`;
    } catch (e) {
      console.warn(`failed to fetch ${name} metadata`, e);
    }
    map.addSource(sourceId, {
      type: "image",
      // Cache-buster: forces a fresh fetch each page load so stale
      // server-rendered PNGs can't be served from disk cache. MapLibre
      // image sources bypass the patched window.fetch, so the aquifer
      // param is baked into the URL explicitly.
      url: GABORA.withAquifer(`/api/model-setup/property/${name}.png?t=${Date.now()}`),
      coordinates: corners,
    });
    // Insert below the road layer so labels remain legible.
    const beforeId = map.getLayer("roads") ? "roads" : undefined;
    map.addLayer({
      id: layerId, type: "raster", source: sourceId,
      paint: {
        "raster-opacity": 0.78,
        "raster-fade-duration": 0,
        // Nearest-neighbour resampling so each grid cell renders as a
        // crisp square; without this MapLibre's default bilinear filter
        // smears values between neighbouring cells, which is misleading
        // for piecewise-constant calibrated K / Ss fields.
        "raster-resampling": "nearest",
      },
    }, beforeId);
    loaded = true;
    return true;
  };

  checkbox.addEventListener("change", async () => {
    const on = checkbox.checked;
    legend.hidden = !on;
    if (on) await ensureLoaded();
    if (map.getLayer(layerId)) {
      map.setLayoutProperty(layerId, "visibility", on ? "visible" : "none");
    }
  });
}

function fmtSci(v) {
  if (v == null || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 0.01 && a < 1000) return Number(v).toPrecision(3);
  return Number(v).toExponential(2);
}

async function samplePropertiesAt(map, lngLat) {
  const visible = [];
  for (const name of ["k", "ss"]) {
    const id = `prop-${name}-layer`;
    if (map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none") {
      visible.push(name);
    }
  }
  if (visible.length === 0) return;
  console.debug("[setup] sampling properties at", lngLat, "visible:", visible);

  // Fetch the sample for every visible property in parallel.
  const results = await Promise.all(
    visible.map(async (name) => {
      try {
        const r = await fetch(
          `/api/model-setup/property/${name}/sample?lng=${lngLat.lng}&lat=${lngLat.lat}`,
        );
        if (!r.ok) {
          console.warn(`[setup] /sample ${name} returned HTTP ${r.status}`);
          return { name, error: `HTTP ${r.status}` };
        }
        return await r.json();
      } catch (err) {
        console.warn(`[setup] /sample ${name} failed:`, err);
        return { name, error: String(err) };
      }
    }),
  );

  const errored = results.filter((d) => d && d.error);
  const usable  = results.filter((d) => d && !d.error);

  // If every successful response says we're outside the domain (and
  // nothing errored), tell the user.
  if (usable.length && usable.every((d) => d.in_domain === false)) {
    new maplibregl.Popup({ closeButton: true })
      .setLngLat(lngLat)
      .setHTML(`<div><em>outside model domain</em></div>`)
      .addTo(map);
    return;
  }
  // Backend unreachable / endpoint missing — surface it instead of
  // silently doing nothing (this used to look like the click was dead).
  if (errored.length && !usable.some((d) => d.in_domain)) {
    new maplibregl.Popup({ closeButton: true })
      .setLngLat(lngLat)
      .setHTML(
        `<div><strong>Sample request failed</strong></div>` +
        `<div class="muted-pop" style="color:#94a3b8;font-size:0.72rem">
          ${errored.map((d) => `${d.name}: ${d.error}`).join("<br/>")}
        </div>` +
        `<div class="muted-pop" style="color:#94a3b8;font-size:0.72rem;margin-top:0.3rem">
          restart the API and hard-refresh
        </div>`,
      )
      .addTo(map);
    return;
  }

  // Build a single popup with one row per visible property.
  let html = "";
  let rc = null;
  for (const d of usable) {
    if (!d || !d.in_domain) continue;
    rc = rc || [d.row, d.col];
    html += `<div style="margin-bottom:0.45rem">
      <strong>${d.label}</strong>
      <div style="font-size:1rem;margin:0.15rem 0">
        ${fmtSci(d.value)} <span style="color:#94a3b8;font-size:0.78rem">${d.units}</span>
      </div>`;
    if (d.name === "k") {
      const T = d.value * d.thickness_m;
      html += `<div style="color:#94a3b8;font-size:0.72rem">
        thickness ${d.thickness_m.toFixed(1)} m · T = ${fmtSci(T)} m²/d
      </div>`;
    } else if (d.name === "ss") {
      const S = d.value * d.thickness_m;
      html += `<div style="color:#94a3b8;font-size:0.72rem">
        thickness ${d.thickness_m.toFixed(1)} m · S = ${fmtSci(S)}
      </div>`;
    }
    html += `</div>`;
  }
  if (rc) {
    html += `<div style="color:#94a3b8;font-size:0.7rem;border-top:1px solid #1f2d4a;padding-top:0.3rem">
      cell (${rc[0]}, ${rc[1]})
    </div>`;
  }
  new maplibregl.Popup({ closeButton: true })
    .setLngLat(lngLat).setHTML(html).addTo(map);
}

window.addEventListener("DOMContentLoaded", init);

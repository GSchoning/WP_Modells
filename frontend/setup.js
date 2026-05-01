/* Model setup page: shows the modelled grid, boundaries, recharge zone,
 * pumping bores, and spring complexes on the satellite basemap.
 */

const $ = (id) => document.getElementById(id);

const SAT_STYLE = {
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

// Per-layer vector polygon configuration. Colours match the legend swatches
// in setup.css. Each layer renders the cell-square polygons fetched from
// /api/model-setup/<layer>.geojson.
const VECTOR_LAYERS = [
  { id: "active",  url: "/api/model-setup/active.geojson",  fill: "#9ca3af", stroke: "#475569", opacity: 0.25, strokeOpacity: 0.0 },
  { id: "outcrop", url: "/api/model-setup/outcrop.geojson", fill: "#10b981", stroke: "#047857", opacity: 0.55, strokeOpacity: 0.0 },
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

  $("counts").innerHTML =
    `<div><span class="swatch active" style="vertical-align:middle"></span> ` +
    `${info.grid.n_active_cells.toLocaleString()} active cells</div>` +
    `<div><span class="swatch outcrop" style="vertical-align:middle"></span> ` +
    `${info.grid.n_outcrop_cells.toLocaleString()} outcrop / recharge cells</div>` +
    `<div><span class="swatch chd" style="vertical-align:middle"></span> ` +
    `${info.boundaries.n_chd_cells.toLocaleString()} CHD boundary cells</div>` +
    `<div><span class="swatch noflow" style="vertical-align:middle"></span> ` +
    `${info.boundaries.n_noflow_boundary_cells.toLocaleString()} no-flow boundary cells</div>` +
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

    // Pumping bores (vector layer from /map-data).
    if (mapData.pumping_bores) {
      map.addSource("pumping", { type: "geojson", data: mapData.pumping_bores });
      map.addLayer({
        id: "pumping", type: "circle", source: "pumping",
        paint: {
          "circle-radius": 3, "circle-color": "#ef4444",
          "circle-opacity": 0.85, "circle-stroke-color": "#7f1d1d",
          "circle-stroke-width": 0.5,
        },
        layout: { visibility: $("toggle-bores").checked ? "visible" : "none" },
      });
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
          .setHTML(`<strong>${p.complex_id}</strong><br/>${p.n_springs} member springs`)
          .addTo(map);
      });
    }
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
  toggle("toggle-chd",       ["vec-chd-fill",     "vec-chd-line"]);
  toggle("toggle-noflow",    ["vec-noflow-fill",  "vec-noflow-line"]);
  toggle("toggle-bores",     ["pumping"]);
  toggle("toggle-complexes", ["complexes"]);
}

window.addEventListener("DOMContentLoaded", init);

/* Drawdown-maps page: side-by-side cumulative + proposed-only rasters
 * for the most recent Scenario C run.
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

const STATE = {
  info: null,
  maps: {},                  // layer -> map
  opacity: 0.7,
};

async function init() {
  let info;
  try {
    info = await (await fetch("/api/last-scenario/info")).json();
  } catch (e) {
    showEmpty("Backend unreachable.");
    return;
  }
  if (!info.available) {
    showEmpty("No scenario has been run yet. Go back and run one first.");
    return;
  }
  STATE.info = info;

  const meta = $("scenario-meta");
  meta.textContent =
    `${info.bore.bore_id} · ${info.bore.rate_ML_per_year} ML/yr · ` +
    `(${info.bore.x.toFixed(0)}, ${info.bore.y.toFixed(0)})`;

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
      url: `/api/last-scenario/drawdown.png?layer=${layer}&year=${year}`,
      coordinates: STATE.info.image_corners_4326,
    });
    map.addLayer({
      id: "dd", type: "raster", source: "dd",
      paint: { "raster-opacity": STATE.opacity, "raster-fade-duration": 0 },
    });
    new maplibregl.Marker({ color: "#f59e0b" })
      .setLngLat([STATE.info.bore.lng, STATE.info.bore.lat])
      .setPopup(new maplibregl.Popup().setHTML(
        `<strong>${STATE.info.bore.bore_id}</strong><br/>${STATE.info.bore.rate_ML_per_year} ML/yr`
      ))
      .addTo(map);

    // Click-to-sample: query the underlying grid value at the clicked
    // point. Server reprojects EPSG:4326 to project CRS and looks up
    // the cell drawdown.
    map.on("click", async (e) => {
      const yr = $("year-select").value;
      const url = `/api/last-scenario/drawdown/sample?lng=${e.lngLat.lng}&lat=${e.lngLat.lat}&layer=${layer}&year=${yr}`;
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

function updateOverlay(layer, year) {
  const map = STATE.maps[layer];
  if (!map || !map.getSource("dd")) return;
  map.getSource("dd").updateImage({
    url: `/api/last-scenario/drawdown.png?layer=${layer}&year=${year}`,
    coordinates: STATE.info.image_corners_4326,
  });
}

window.addEventListener("DOMContentLoaded", init);

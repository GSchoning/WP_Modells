/* Shared helpers for all GABORA pages. Loaded before each page script.
 * Exposed as window.GABORA — no build step, plain script tags.
 */
(function () {
  "use strict";

  /**
   * MapLibre raster style: Esri satellite imagery, optionally with the
   * transparent roads + town-labels reference layers on top.
   */
  function makeSatStyle({ roads = true, places = true } = {}) {
    const sources = {
      sat: {
        type: "raster",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        tileSize: 256,
        attribution: "Imagery © Esri, Maxar, Earthstar Geographics, USDA, USGS, IGN",
      },
    };
    const layers = [{ id: "sat", type: "raster", source: "sat" }];
    if (roads) {
      sources.roads = {
        type: "raster",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"],
        tileSize: 256,
        attribution: "Reference © Esri",
      };
      layers.push({ id: "roads", type: "raster", source: "roads",
        paint: { "raster-opacity": 0.85 } });
    }
    if (places) {
      sources.places = {
        type: "raster",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"],
        tileSize: 256,
      };
      layers.push({ id: "places", type: "raster", source: "places",
        paint: { "raster-opacity": 0.9 } });
    }
    return { version: 8, sources, layers };
  }

  /** Move the reference overlays back above any layers added since load. */
  function raiseReferenceLayers(map) {
    if (map.getLayer("roads")) map.moveLayer("roads");
    if (map.getLayer("places")) map.moveLayer("places");
  }

  /** Escape a data-driven string for safe interpolation into innerHTML. */
  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** 3-significant-figure number, scientific notation outside [0.01, 1000). */
  function fmtSci(v) {
    if (v == null || !Number.isFinite(Number(v))) return "–";
    const a = Math.abs(Number(v));
    if (a >= 0.01 && a < 1000) return Number(v).toPrecision(3);
    return Number(v).toExponential(2);
  }

  window.GABORA = { makeSatStyle, raiseReferenceLayers, escapeHtml, fmtSci };
})();

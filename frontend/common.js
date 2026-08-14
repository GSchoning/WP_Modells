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

  /* ---- Multi-aquifer routing --------------------------------------------
   * The module pages are aquifer-agnostic; ?aquifer=<key> in the page URL
   * selects which backend module serves them (default: precipice).
   *  - every same-origin /api/ fetch gets the aquifer appended, and
   *  - relative page links (setup.html, scenario.html, …) inherit it,
   * so navigation keeps the module context without touching page code.
   */
  const AQUIFER = new URLSearchParams(window.location.search).get("aquifer") || "precipice";

  const _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input?.url;
      if (url && (url.startsWith("/api/") || url.startsWith("api/"))) {
        const sep = url.includes("?") ? "&" : "?";
        const patched = `${url}${sep}aquifer=${encodeURIComponent(AQUIFER)}`;
        input = typeof input === "string" ? patched : new Request(patched, input);
      }
    } catch (e) { /* fall through with the original input */ }
    return _fetch(input, init);
  };

  function withAquifer(href) {
    const sep = href.includes("?") ? "&" : "?";
    return `${href}${sep}aquifer=${encodeURIComponent(AQUIFER)}`;
  }

  document.addEventListener("DOMContentLoaded", () => {
    // ALWAYS brand the header with the module actually serving this page
    // — a URL that lost its ?aquifer= parameter silently defaults to the
    // Precipice, and the title is how the user notices.
    const sub = document.getElementById("module-subtitle");
    if (sub) {
      fetch("/api/healthz").then((r) => r.json()).then((h) => {
        if (h && h.aquifer_title) {
          sub.textContent = `${h.aquifer_title} — water licence impact assessment`;
          document.title = `GABORA — ${h.aquifer_title}`;
        }
      }).catch(() => {});
    }
    if (AQUIFER === "precipice") return;   // default needs no link propagation
    document.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href");
      // only same-site module pages; leave the landing page (index.html)
      // and external/anchor links alone.
      if (!href || /^(https?:|#|mailto:)/.test(href)) return;
      if (!/^(precipice|setup|scenario)\.html/.test(href)) return;
      if (href.includes("aquifer=")) return;
      a.setAttribute("href", withAquifer(href));
    });
  });

  window.GABORA = { makeSatStyle, raiseReferenceLayers, escapeHtml, fmtSci,
                    AQUIFER, withAquifer };
})();

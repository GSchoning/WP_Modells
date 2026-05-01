"""FastAPI service: cached Scenario A + on-demand Scenario C.

Architecture (CLAUDE.md §12):
- App startup: load config + inputs + grid, build boundary CHD, run (or
  load cached) steady-state IC, run (or load cached) Scenario A. These
  are reused across all requests.
- POST /scenarios: runs Scenario C only with the user-supplied proposed
  bore, combines with cached A by superposition, returns drawdowns.
- GET /baseline: cached Scenario A drawdowns at all spring complexes.
- GET /map-data: GeoJSON layers for the frontend map.
- GET /healthz: liveness check.

Receptor unit of analysis is the **spring complex** (configurable via
assessment.spring_complex_col). Per-spring drawdowns are aggregated by
max within each complex — the conservative choice for trigger-threshold
reporting.
"""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyproj
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import Config, load_config
from ..grid import Grid, build_grid_from_properties, cell_of
from ..io_layer import Inputs, ML_PER_YEAR_TO_M3_PER_DAY, load_inputs
from ..model_builder import active_boundary_chd_cells
from ..scenarios import ScenarioResult, run_scenario, run_steady_state
from ..superposition import combine_receptor_tables
from ..theis import _local_T_S, theis_at_springs
from . import cache as cache_mod
from .schemas import (
    BaselineResponse,
    ComplexDrawdown,
    HealthResponse,
    ScenarioRequest,
    ScenarioResponse,
    TheisDiagnostics,
    YearResults,
)


class _State:
    cfg: Config | None = None
    config_path: Path | None = None
    inputs: Inputs | None = None
    grid: Grid | None = None
    ic_head: np.ndarray | None = None
    chd_cells: list | None = None
    workspace_root: Path | None = None
    baseline: cache_mod.BaselineCache | None = None
    complex_centroids_4326: dict | None = None      # GeoJSON FeatureCollection
    # Last Scenario C run, retained for the drawdown-maps page.
    last_proposed_bore: dict | None = None
    last_c_drawdown_by_year: dict | None = None      # year -> (nrow, ncol)


state = _State()


def _build_complex_centroids(springs: gpd.GeoDataFrame, complex_col: str) -> dict:
    """One Point feature per spring complex (centroid of member springs)."""
    if springs is None or complex_col not in springs.columns:
        return {"type": "FeatureCollection", "features": []}
    sp4326 = springs.to_crs("EPSG:4326")
    features = []
    for cname, group in sp4326.groupby(complex_col):
        if not cname or str(cname).lower() in ("nan", "none"):
            continue
        cx = float(group.geometry.x.mean())
        cy = float(group.geometry.y.mean())
        features.append({
            "type": "Feature",
            "properties": {
                "complex_id": str(cname),
                "n_springs": int(len(group)),
                "s_total": 0.0,
                "exceeds_threshold": False,
            },
            "geometry": {"type": "Point", "coordinates": [cx, cy]},
        })
    return {"type": "FeatureCollection", "features": features}


def _bootstrap_baseline(force: bool = False) -> cache_mod.BaselineCache:
    """Run (or load) the cached Scenario A baseline."""
    assert state.cfg and state.grid and state.inputs and state.ic_head is not None
    key = cache_mod.baseline_key(state.cfg, state.config_path)
    if not force:
        hit = cache_mod.load(key)
        if hit is not None:
            return hit

    result = run_scenario(
        state.cfg, state.grid, state.inputs, "A",
        state.ic_head, state.workspace_root / "scen_A",
        chd_cells=state.chd_cells,
    )
    cache = cache_mod.BaselineCache(
        key=key,
        receptors_df=result.receptors_df.copy(),
        drawdown_by_year=result.drawdown_at_output_years,
    )
    cache_mod.save(cache, state.cfg, state.config_path)
    return cache


def _bootstrap_ic() -> None:
    """Pick the best (CHD config + IC) for the current cfg.

    1. Try outcrop excluded from CHD (physically correct: outcrop edge is a
       recharge inflow, not a regional discharge).
    2. If steady-state can't converge with that boundary, fall back to
       outcrop included (the older, robust configuration) — recharge gets
       pinned to NTOP at the outcrop edge but at least the model runs.
    3. If both fail, use a uniform IC = mean(active NTOP). Drawdown
       computed from this is still well-behaved because of the twin-run
       differencing, just less physically meaningful.

    Sets state.chd_cells and state.ic_head as a side effect.
    """
    grid = state.grid
    workspace = state.workspace_root / "ss"

    chd_excluded = active_boundary_chd_cells(grid, exclude_mask=grid.outcrop_mask)
    chd_included = active_boundary_chd_cells(grid)
    attempts = [
        ("outcrop excluded", chd_excluded),
        ("outcrop included", chd_included),
    ]
    for label, chd in attempts:
        try:
            ic = run_steady_state(state.cfg, grid, workspace, chd_cells=chd)
            state.chd_cells = chd
            state.ic_head = ic
            print(f"[boundary] steady-state converged with {label} ({len(chd)} CHD cells)")
            return
        except RuntimeError as exc:
            print(f"[boundary] steady-state failed with {label}: {exc}")

    print("[boundary] all steady-state attempts failed; using uniform IC")
    active = grid.idomain[0] == 1
    mean_top = float(np.nanmean(np.where(active, grid.top, np.nan)))
    state.ic_head = np.full_like(grid.top, mean_top)
    # Use the safer (outcrop-included) CHD with the uniform IC.
    state.chd_cells = chd_included


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = Path(os.environ.get("PRECIPICE_CONFIG", "config.yaml"))
    state.cfg = load_config(config_path)
    state.config_path = config_path
    state.inputs = load_inputs(state.cfg)
    state.grid = build_grid_from_properties(
        state.inputs.properties, state.cfg.project.crs,
        layer=state.cfg.grid.properties_layer,
    )
    state.workspace_root = Path(state.cfg.run.workspace_root)
    state.workspace_root.mkdir(parents=True, exist_ok=True)

    _bootstrap_ic()
    state.baseline = _bootstrap_baseline()
    state.complex_centroids_4326 = _build_complex_centroids(
        state.inputs.springs, state.cfg.assessment.spring_complex_col,
    )
    yield


app = FastAPI(
    title="Precipice Sandstone — Water Licence Impact API",
    version="0.2.0",
    lifespan=lifespan,
)


def _df_to_year_results(combined: pd.DataFrame, threshold: float) -> list[YearResults]:
    """Build YearResults with three threshold classifications per complex:

    - already_exceeded: s_approved_m >= threshold (existing licences alone
      already cause an exceedance — informational, not the proposed bore's
      fault).
    - triggered_by_proposed: s_approved_m < threshold but s_total >= threshold
      (the proposed bore is what tips this complex over — the regulatory
      decision-maker).
    - exceeds_threshold: s_total >= threshold (union of the two — kept for
      back-compat).
    """
    out: list[YearResults] = []
    has_theis = "drawdown_m_theis" in combined.columns
    has_r = "r_m" in combined.columns
    has_n = "n_springs" in combined.columns
    for y in sorted(combined["time_years"].unique()):
        sub = combined[combined["time_years"] == y].sort_values("s_total", ascending=False)
        complexes: list[ComplexDrawdown] = []
        for _, r in sub.iterrows():
            s_appr = float(r["s_approved"])
            s_tot = float(r["s_total"])
            already = s_appr >= threshold
            exceeds = s_tot >= threshold
            triggered = exceeds and not already
            complexes.append(ComplexDrawdown(
                complex_id=str(r["receptor_id"]),
                n_springs=int(r["n_springs"]) if has_n and not pd.isna(r["n_springs"]) else 1,
                s_approved_m=s_appr,
                s_additional_m=float(r["s_additional"]),
                s_total_m=s_tot,
                s_additional_theis_m=float(r["drawdown_m_theis"]) if has_theis and not pd.isna(r["drawdown_m_theis"]) else None,
                r_to_proposed_m=float(r["r_m"]) if has_r and not pd.isna(r["r_m"]) else None,
                exceeds_threshold=exceeds,
                already_exceeded=already,
                triggered_by_proposed=triggered,
            ))
        out.append(YearResults(
            time_years=float(y),
            complexes=complexes,
            n_exceedances=sum(1 for c in complexes if c.exceeds_threshold),
            n_triggered=sum(1 for c in complexes if c.triggered_by_proposed),
            n_already_exceeded=sum(1 for c in complexes if c.already_exceeded),
        ))
    return out


def _n_complexes() -> int:
    if state.inputs is None or state.inputs.springs is None:
        return 0
    col = state.cfg.assessment.spring_complex_col
    if col not in state.inputs.springs.columns:
        return 0
    return int(state.inputs.springs[col].nunique())


@app.get("/api/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        project=state.cfg.project.name,
        crs=state.cfg.project.crs,
        n_pumping_bores=len(state.inputs.pumping_bores),
        n_springs=0 if state.inputs.springs is None else len(state.inputs.springs),
        n_spring_complexes=_n_complexes(),
        regulatory_threshold_m=state.cfg.assessment.regulatory_threshold_m,
        baseline_cached=state.baseline is not None,
    )


@app.get("/api/baseline", response_model=BaselineResponse)
def baseline() -> BaselineResponse:
    if state.baseline is None:
        raise HTTPException(503, "Baseline not ready")
    df = state.baseline.receptors_df.rename(columns={"drawdown_m": "s_approved"})
    df["s_additional"] = 0.0
    df["s_total"] = df["s_approved"]
    threshold = state.cfg.assessment.regulatory_threshold_m
    return BaselineResponse(
        cache_key=state.baseline.key,
        regulatory_threshold_m=threshold,
        output_years=sorted(df["time_years"].unique().tolist()),
        by_year=_df_to_year_results(df, threshold),
    )


@app.post("/api/scenarios", response_model=ScenarioResponse)
def scenarios(req: ScenarioRequest) -> ScenarioResponse:
    if state.baseline is None:
        raise HTTPException(503, "Baseline not ready")

    state.cfg.inputs.proposed_bore.bore_id = req.proposed_bore.bore_id
    state.cfg.inputs.proposed_bore.x = req.proposed_bore.x
    state.cfg.inputs.proposed_bore.y = req.proposed_bore.y
    state.cfg.inputs.proposed_bore.rate_ML_per_year = req.proposed_bore.rate_ML_per_year

    # Recharge multiplier change re-runs the IC and re-baselines Scenario A
    # against a different cache slot. Both the steady-state IC and the
    # cached A baseline are tied to the multiplier, so the cache key
    # automatically picks the right slot or computes fresh if missing.
    if req.recharge_multiplier != state.cfg.assessment.recharge_multiplier:
        state.cfg.assessment.recharge_multiplier = req.recharge_multiplier
        _bootstrap_ic()
        state.baseline = _bootstrap_baseline()

    t0 = time.time()
    workspace = state.workspace_root / f"scen_C_{req.proposed_bore.bore_id}"
    try:
        c_result = run_scenario(
            state.cfg, state.grid, state.inputs, "C",
            state.ic_head, workspace, chd_cells=state.chd_cells,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    runtime = time.time() - t0

    # Retain Scenario C grids for the drawdown-maps page.
    state.last_proposed_bore = req.proposed_bore.model_dump()
    state.last_c_drawdown_by_year = c_result.drawdown_at_output_years

    combined = combine_receptor_tables(
        state.baseline.receptors_df,
        c_result.receptors_df,
    )

    theis_diag: TheisDiagnostics | None = None
    if state.inputs.springs is not None and len(state.inputs.springs):
        spring_id_col = state.cfg.assessment.spring_id_col
        complex_col = state.cfg.assessment.spring_complex_col
        if spring_id_col not in state.inputs.springs.columns:
            spring_id_col = state.inputs.springs.columns[0]
        rate_m3d = req.proposed_bore.rate_ML_per_year * ML_PER_YEAR_TO_M3_PER_DAY
        theis_df = theis_at_springs(
            state.grid, state.inputs.springs, spring_id_col,
            req.proposed_bore.x, req.proposed_bore.y, rate_m3d,
            output_years=sorted(combined["time_years"].unique()),
            complex_col=complex_col if complex_col in state.inputs.springs.columns else None,
        )
        combined = combined.merge(
            theis_df[["receptor_id", "time_years", "drawdown_m_theis", "r_m"]],
            on=["receptor_id", "time_years"], how="left",
        )
        rc = cell_of(state.grid, req.proposed_bore.x, req.proposed_bore.y)
        T, S = _local_T_S(state.grid, rc[0], rc[1])
        theis_diag = TheisDiagnostics(T_m2_per_day=T, S_dimensionless=S, well_cell=[rc[0], rc[1]])

    threshold = state.cfg.assessment.regulatory_threshold_m
    year_results = _df_to_year_results(combined, threshold)
    last_year = max(combined["time_years"].unique())
    last_complexes = [yr for yr in year_results if yr.time_years == last_year][0].complexes
    top_n = last_complexes[:10]
    exceedance_ids = {
        c.complex_id for yr in year_results for c in yr.complexes if c.exceeds_threshold
    }
    triggered_ids = {
        c.complex_id for yr in year_results for c in yr.complexes if c.triggered_by_proposed
    }
    already_ids = {
        c.complex_id for yr in year_results for c in yr.complexes if c.already_exceeded
    }
    return ScenarioResponse(
        proposed_bore=req.proposed_bore,
        output_years=[yr.time_years for yr in year_results],
        regulatory_threshold_m=threshold,
        by_year=year_results,
        top_n_total=top_n,
        n_exceedances_any_year=len(exceedance_ids),
        n_triggered_any_year=len(triggered_ids),
        n_already_exceeded_any_year=len(already_ids),
        runtime_seconds=runtime,
        theis=theis_diag,
    )


@app.get("/api/map-data")
def map_data():
    """GeoJSON layers for the frontend map. Reprojects everything to EPSG:4326."""
    if state.inputs is None or state.grid is None:
        raise HTTPException(503, "Inputs not ready")
    inputs = state.inputs
    grid = state.grid
    cfg = state.cfg

    formation = inputs.formation_extent.to_crs("EPSG:4326")
    outcrop = inputs.outcrop.to_crs("EPSG:4326")
    pumping = inputs.pumping_bores.to_crs("EPSG:4326")

    import pyproj
    transformer = pyproj.Transformer.from_crs(cfg.project.crs, "EPSG:4326", always_xy=True)
    x0, y0 = grid.xorigin, grid.yorigin
    x1 = grid.xorigin + float(grid.delr.sum())
    y1 = grid.yorigin + float(grid.delc.sum())
    lon0, lat0 = transformer.transform(x0, y0)
    lon1, lat1 = transformer.transform(x1, y1)

    return JSONResponse({
        "crs": cfg.project.crs,
        "bbox_4326": [lon0, lat0, lon1, lat1],
        "regulatory_threshold_m": cfg.assessment.regulatory_threshold_m,
        "formation_extent": _gdf_to_geojson(formation),
        "outcrop": _gdf_to_geojson(outcrop),
        "pumping_bores": _gdf_to_geojson(
            pumping[["bore_id", "rate_m3_per_day", "geometry"]]
            if "bore_id" in pumping.columns
            else pumping[["rate_m3_per_day", "geometry"]]
        ),
        "spring_complexes": state.complex_centroids_4326,
    })


def _gdf_to_geojson(gdf):
    return json.loads(gdf.to_json())


# Custom 4-stop blue→red sequential colormap for drawdown maps.
# Navy → blue → amber → red; perceptually monotonic on a satellite
# basemap. Created once at import time.
_BLUE_RED_CMAP = mcolors.LinearSegmentedColormap.from_list(
    "drawdown_blue_red",
    ["#1e3a8a", "#3b82f6", "#fbbf24", "#dc2626"],
)
_BLUE_RED_CMAP.set_bad(alpha=0.0)

# Drawdown below this value is rendered transparent. Matches the
# regulatory trigger threshold's lower bound where values become
# decision-relevant.
DRAWDOWN_DISPLAY_FLOOR_M = 0.2


def _drawdown_to_png(arr: np.ndarray, idomain: np.ndarray, vmax: float | None = None) -> bytes:
    """Render a (nrow, ncol) drawdown grid to a transparent PNG.

    Cells outside the active domain or with drawdown below
    DRAWDOWN_DISPLAY_FLOOR_M are fully transparent. Values are clipped
    to vmax (default: 99th percentile of |arr| over the visible cells)
    and mapped through a navy→amber→red sequential colormap.
    """
    masked = np.where(idomain == 1, arr, np.nan)
    masked = np.where(masked >= DRAWDOWN_DISPLAY_FLOOR_M, masked, np.nan)
    valid = masked[~np.isnan(masked)]
    if vmax is None:
        vmax = float(np.nanpercentile(np.abs(valid), 99)) if valid.size else 1.0
        vmax = max(vmax, DRAWDOWN_DISPLAY_FLOOR_M + 0.1)
    nrow, ncol = arr.shape
    fig = plt.figure(figsize=(ncol / 100, nrow / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    norm = mcolors.Normalize(vmin=DRAWDOWN_DISPLAY_FLOOR_M, vmax=vmax)
    ax.imshow(masked, cmap=_BLUE_RED_CMAP, norm=norm,
              origin="upper", interpolation="nearest")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()


def _bbox_4326() -> dict:
    """Project-CRS grid extent reprojected to EPSG:4326 (corners + bbox)."""
    g = state.grid
    transformer = pyproj.Transformer.from_crs(state.cfg.project.crs, "EPSG:4326", always_xy=True)
    x0, y0 = g.xorigin, g.yorigin
    x1 = g.xorigin + float(g.delr.sum())
    y1 = g.yorigin + float(g.delc.sum())
    tl = list(transformer.transform(x0, y1))
    tr = list(transformer.transform(x1, y1))
    br = list(transformer.transform(x1, y0))
    bl = list(transformer.transform(x0, y0))
    return {
        "tl_tr_br_bl": [tl, tr, br, bl],
        "bbox": [
            min(tl[0], bl[0]), min(br[1], bl[1]),
            max(tr[0], br[0]), max(tl[1], tr[1]),
        ],
    }


@app.get("/api/last-scenario/info")
def last_scenario_info():
    """Metadata for the drawdown-maps page: bore, available years, bounds."""
    if state.last_c_drawdown_by_year is None or state.last_proposed_bore is None:
        return JSONResponse({"available": False})
    transformer = pyproj.Transformer.from_crs(state.cfg.project.crs, "EPSG:4326", always_xy=True)
    bore = dict(state.last_proposed_bore)
    bore_lng, bore_lat = transformer.transform(float(bore["x"]), float(bore["y"]))
    bbox = _bbox_4326()
    return JSONResponse({
        "available": True,
        "bore": {**bore, "lng": bore_lng, "lat": bore_lat},
        "years": sorted(state.last_c_drawdown_by_year.keys()),
        "image_corners_4326": bbox["tl_tr_br_bl"],
        "bbox_4326": bbox["bbox"],
        "threshold_m": state.cfg.assessment.regulatory_threshold_m,
    })


@app.get("/api/last-scenario/drawdown.png")
def last_scenario_drawdown_png(layer: str = "cumulative", year: float = 100.0):
    """Drawdown raster as a transparent PNG, ready for a MapLibre image source."""
    if state.last_c_drawdown_by_year is None:
        raise HTTPException(404, "No scenario has been run yet")
    available_years = list(state.last_c_drawdown_by_year.keys())
    # Tolerate small float mismatches (e.g. 100.0 vs 100.0000001).
    near = [y for y in available_years if abs(float(y) - float(year)) < 1e-6]
    if not near:
        raise HTTPException(400, f"Year {year} not available; choose from {available_years}")
    y_key = near[0]

    c_arr = state.last_c_drawdown_by_year[y_key]
    if layer == "additional":
        arr = c_arr
    elif layer == "cumulative":
        if state.baseline is None:
            raise HTTPException(503, "Baseline not ready")
        a_arr = state.baseline.drawdown_by_year.get(y_key)
        if a_arr is None:
            # Find the nearest baseline year.
            keys = list(state.baseline.drawdown_by_year.keys())
            nearest = min(keys, key=lambda k: abs(float(k) - float(y_key)))
            a_arr = state.baseline.drawdown_by_year[nearest]
        arr = a_arr + c_arr
    else:
        raise HTTPException(400, "layer must be 'cumulative' or 'additional'")

    png = _drawdown_to_png(arr, state.grid.idomain[0])
    headers = {"Cache-Control": "no-store"}    # avoid stale image after re-runs
    return Response(content=png, media_type="image/png", headers=headers)


@app.get("/api/last-scenario/drawdown/sample")
def last_scenario_drawdown_sample(lng: float, lat: float,
                                  layer: str = "cumulative", year: float = 100.0):
    """Drawdown value at a clicked map point (EPSG:4326)."""
    if state.last_c_drawdown_by_year is None:
        raise HTTPException(404, "No scenario has been run yet")
    available_years = list(state.last_c_drawdown_by_year.keys())
    near = [yk for yk in available_years if abs(float(yk) - float(year)) < 1e-6]
    if not near:
        raise HTTPException(400, f"Year {year} not available")
    y_key = near[0]

    transformer = pyproj.Transformer.from_crs("EPSG:4326", state.cfg.project.crs, always_xy=True)
    x, y_proj = transformer.transform(lng, lat)
    rc = cell_of(state.grid, x, y_proj)
    if rc is None or state.grid.idomain[0, rc[0], rc[1]] != 1:
        return JSONResponse({
            "in_domain": False,
            "x": float(x), "y": float(y_proj),
        })

    c_val = float(state.last_c_drawdown_by_year[y_key][rc[0], rc[1]])
    if layer == "additional":
        s_total = c_val
        s_approved = None
    elif layer == "cumulative":
        if state.baseline is None:
            raise HTTPException(503, "Baseline not ready")
        a_grid = state.baseline.drawdown_by_year.get(y_key)
        if a_grid is None:
            keys = list(state.baseline.drawdown_by_year.keys())
            nearest = min(keys, key=lambda k: abs(float(k) - float(y_key)))
            a_grid = state.baseline.drawdown_by_year[nearest]
        s_approved = float(a_grid[rc[0], rc[1]])
        s_total = s_approved + c_val
    else:
        raise HTTPException(400, "layer must be 'cumulative' or 'additional'")

    return JSONResponse({
        "in_domain": True,
        "x": float(x), "y": float(y_proj),
        "row": int(rc[0]), "col": int(rc[1]),
        "drawdown_m": s_total,
        "s_approved_m": s_approved,
        "s_additional_m": c_val,
    })


def _bool_mask_to_png(mask: np.ndarray, hex_color: str, alpha: float = 0.7) -> bytes:
    """Render a (nrow, ncol) bool mask as a transparent PNG.

    Cells where the mask is True get the chosen colour at the chosen
    alpha; cells where the mask is False are fully transparent.
    """
    nrow, ncol = mask.shape
    arr = np.where(mask, 1.0, np.nan)
    cmap = mcolors.ListedColormap([hex_color])
    cmap.set_bad(alpha=0.0)
    fig = plt.figure(figsize=(ncol / 100, nrow / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.imshow(arr, cmap=cmap, vmin=0, vmax=1, origin="upper",
              interpolation="nearest", alpha=alpha)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()


def _chd_mask() -> np.ndarray:
    """(nrow, ncol) bool mask of cells carrying CHD."""
    g = state.grid
    mask = np.zeros((g.nrow, g.ncol), dtype=bool)
    for (_l, r, c, _h) in (state.chd_cells or []):
        mask[r, c] = True
    return mask


def _noflow_boundary_mask() -> np.ndarray:
    """Active-boundary cells that aren't CHD = effective no-flow boundary."""
    g = state.grid
    active = g.idomain[0] == 1
    padded = np.pad(active, 1, constant_values=False)
    has_inactive_neighbour = (
        ~padded[:-2, 1:-1] | ~padded[2:, 1:-1]
        | ~padded[1:-1, :-2] | ~padded[1:-1, 2:]
    )
    on_boundary = active & has_inactive_neighbour
    return on_boundary & ~_chd_mask()


@app.get("/api/model-setup/info")
def model_setup_info():
    """Metadata for the model-setup page."""
    if state.grid is None:
        raise HTTPException(503, "Grid not ready")
    g = state.grid
    bbox = _bbox_4326()
    return JSONResponse({
        "image_corners_4326": bbox["tl_tr_br_bl"],
        "bbox_4326": bbox["bbox"],
        "grid": {
            "nrow": g.nrow, "ncol": g.ncol,
            "dx_m": float(g.delr[0]), "dy_m": float(g.delc[0]),
            "n_active_cells": int((g.idomain == 1).sum()),
            "n_outcrop_cells": int(g.outcrop_mask.sum()),
        },
        "boundaries": {
            "n_chd_cells": int(_chd_mask().sum()),
            "n_noflow_boundary_cells": int(_noflow_boundary_mask().sum()),
        },
        "recharge_multiplier": state.cfg.assessment.recharge_multiplier,
    })


@app.get("/api/model-setup/{layer}.png")
def model_setup_png(layer: str):
    """Per-layer PNG overlay of the model setup."""
    if state.grid is None:
        raise HTTPException(503, "Grid not ready")
    g = state.grid
    if layer == "active":
        mask, color, alpha = (g.idomain[0] == 1), "#9ca3af", 0.30
    elif layer == "outcrop":
        mask, color, alpha = g.outcrop_mask, "#10b981", 0.55
    elif layer == "chd":
        mask, color, alpha = _chd_mask(), "#dc2626", 0.85
    elif layer == "noflow":
        mask, color, alpha = _noflow_boundary_mask(), "#1f2937", 0.85
    else:
        raise HTTPException(400, "layer must be one of: active, outcrop, chd, noflow")
    png = _bool_mask_to_png(mask, color, alpha=alpha)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

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

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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
    state.chd_cells = active_boundary_chd_cells(state.grid)

    try:
        state.ic_head = run_steady_state(
            state.cfg, state.grid, state.workspace_root / "ss",
            chd_cells=state.chd_cells,
        )
    except RuntimeError:
        active = state.grid.idomain[0] == 1
        mean_top = float(np.nanmean(np.where(active, state.grid.top, np.nan)))
        state.ic_head = np.full_like(state.grid.top, mean_top)

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
    out: list[YearResults] = []
    has_theis = "drawdown_m_theis" in combined.columns
    has_r = "r_m" in combined.columns
    has_n = "n_springs" in combined.columns
    for y in sorted(combined["time_years"].unique()):
        sub = combined[combined["time_years"] == y].sort_values("s_total", ascending=False)
        complexes = [
            ComplexDrawdown(
                complex_id=str(r["receptor_id"]),
                n_springs=int(r["n_springs"]) if has_n and not pd.isna(r["n_springs"]) else 1,
                s_approved_m=float(r["s_approved"]),
                s_additional_m=float(r["s_additional"]),
                s_total_m=float(r["s_total"]),
                s_additional_theis_m=float(r["drawdown_m_theis"]) if has_theis and not pd.isna(r["drawdown_m_theis"]) else None,
                r_to_proposed_m=float(r["r_m"]) if has_r and not pd.isna(r["r_m"]) else None,
                exceeds_threshold=bool(float(r["s_total"]) >= threshold),
            )
            for _, r in sub.iterrows()
        ]
        n_exc = sum(1 for c in complexes if c.exceeds_threshold)
        out.append(YearResults(time_years=float(y), complexes=complexes, n_exceedances=n_exc))
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
    return ScenarioResponse(
        proposed_bore=req.proposed_bore,
        output_years=[yr.time_years for yr in year_results],
        regulatory_threshold_m=threshold,
        by_year=year_results,
        top_n_total=top_n,
        n_exceedances_any_year=len(exceedance_ids),
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


_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

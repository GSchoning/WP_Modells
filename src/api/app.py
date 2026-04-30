"""FastAPI service: cached Scenario A + on-demand Scenario C.

Architecture (CLAUDE.md §12):
- App startup: load config + inputs + grid, build boundary CHD, run (or
  load cached) steady-state IC, run (or load cached) Scenario A. These
  are reused across all requests.
- POST /scenarios: runs Scenario C only with the user-supplied proposed
  bore, combines with cached A by superposition, returns drawdowns.
- GET /baseline: cached Scenario A drawdowns at all springs.
- GET /map-data: GeoJSON layers for the frontend map.
- GET /healthz: liveness check.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

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
from ..theis import theis_at_springs, _local_T_S
from . import cache as cache_mod
from .schemas import (
    BaselineResponse,
    HealthResponse,
    ScenarioRequest,
    ScenarioResponse,
    SpringDrawdown,
    TheisDiagnostics,
    YearResults,
)


# Module-level state. FastAPI lifespan populates this once at startup
# and every request reuses it. No per-request grid/IC rebuilds.
class _State:
    cfg: Config | None = None
    config_path: Path | None = None
    inputs: Inputs | None = None
    grid: Grid | None = None
    ic_head: np.ndarray | None = None
    chd_cells: list | None = None
    workspace_root: Path | None = None
    baseline: cache_mod.BaselineCache | None = None


state = _State()


def _result_to_receptors_df(result: ScenarioResult) -> pd.DataFrame:
    return result.receptors_df.copy()


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
        receptors_df=_result_to_receptors_df(result),
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

    # IC: try steady-state, fall back to uniform mean-of-active-top.
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
    yield


app = FastAPI(
    title="Precipice Sandstone — Water Licence Impact API",
    version="0.1.0",
    lifespan=lifespan,
)


def _df_to_year_results(combined: pd.DataFrame) -> list[YearResults]:
    out: list[YearResults] = []
    has_theis = "drawdown_m_theis" in combined.columns
    has_r = "r_m" in combined.columns
    for y in sorted(combined["time_years"].unique()):
        sub = combined[combined["time_years"] == y].sort_values("s_total", ascending=False)
        out.append(YearResults(
            time_years=float(y),
            springs=[
                SpringDrawdown(
                    spring_id=str(r["receptor_id"]),
                    s_approved_m=float(r["s_approved"]),
                    s_additional_m=float(r["s_additional"]),
                    s_total_m=float(r["s_total"]),
                    s_additional_theis_m=float(r["drawdown_m_theis"]) if has_theis else None,
                    r_to_proposed_m=float(r["r_m"]) if has_r else None,
                )
                for _, r in sub.iterrows()
            ],
        ))
    return out


@app.get("/api/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        project=state.cfg.project.name,
        crs=state.cfg.project.crs,
        n_pumping_bores=len(state.inputs.pumping_bores),
        n_springs=0 if state.inputs.springs is None else len(state.inputs.springs),
        baseline_cached=state.baseline is not None,
    )


@app.get("/api/baseline", response_model=BaselineResponse)
def baseline() -> BaselineResponse:
    if state.baseline is None:
        raise HTTPException(503, "Baseline not ready")
    df = state.baseline.receptors_df.rename(columns={"drawdown_m": "s_approved"})
    df["s_additional"] = 0.0
    df["s_total"] = df["s_approved"]
    return BaselineResponse(
        cache_key=state.baseline.key,
        output_years=sorted(df["time_years"].unique().tolist()),
        by_year=_df_to_year_results(df),
    )


@app.post("/api/scenarios", response_model=ScenarioResponse)
def scenarios(req: ScenarioRequest) -> ScenarioResponse:
    if state.baseline is None:
        raise HTTPException(503, "Baseline not ready")

    # Override the proposed bore on the live cfg, then run Scenario C.
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

    # Theis comparison for the proposed bore: analytical drawdown at each
    # spring using local T and S at the well cell. Useful as a sanity
    # check — heterogeneity, boundaries, and recharge cause the modelled
    # value to depart from Theis, but in homogeneous-ish areas they should
    # agree to within a factor of ~2.
    theis_diag: TheisDiagnostics | None = None
    if state.inputs.springs is not None and len(state.inputs.springs):
        spring_id_col = next(
            (c for c in ("spring_id", "SpringID", "Spring_ID", "ID", "OBJECTID", "FID")
             if c in state.inputs.springs.columns),
            state.inputs.springs.columns[0],
        )
        rate_m3d = req.proposed_bore.rate_ML_per_year * ML_PER_YEAR_TO_M3_PER_DAY
        theis_df = theis_at_springs(
            state.grid, state.inputs.springs, spring_id_col,
            req.proposed_bore.x, req.proposed_bore.y, rate_m3d,
            output_years=sorted(combined["time_years"].unique()),
        )
        combined = combined.merge(
            theis_df[["receptor_id", "time_years", "drawdown_m_theis", "r_m"]],
            on=["receptor_id", "time_years"], how="left",
        )
        rc = cell_of(state.grid, req.proposed_bore.x, req.proposed_bore.y)
        T, S = _local_T_S(state.grid, rc[0], rc[1])
        theis_diag = TheisDiagnostics(T_m2_per_day=T, S_dimensionless=S, well_cell=[rc[0], rc[1]])

    year_results = _df_to_year_results(combined)
    last_year = max(combined["time_years"].unique())
    last_springs = [yr for yr in year_results if yr.time_years == last_year][0].springs
    top_n = last_springs[:10]
    return ScenarioResponse(
        proposed_bore=req.proposed_bore,
        output_years=[yr.time_years for yr in year_results],
        by_year=year_results,
        top_n_total=top_n,
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
    springs = (
        inputs.springs.to_crs("EPSG:4326") if inputs.springs is not None else None
    )

    # Active-domain bbox in EPSG:4326 for map fit.
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
        "formation_extent": _gdf_to_geojson(formation),
        "outcrop": _gdf_to_geojson(outcrop),
        "pumping_bores": _gdf_to_geojson(
            pumping[["bore_id", "rate_m3_per_day", "geometry"]]
            if "bore_id" in pumping.columns
            else pumping[["rate_m3_per_day", "geometry"]]
        ),
        "springs": _gdf_to_geojson(springs) if springs is not None else None,
    })


def _gdf_to_geojson(gdf):
    import json as _json
    return _json.loads(gdf.to_json())


# Frontend static files. Mounted last so /api/* routes win.
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

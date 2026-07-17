"""FastAPI service: cached Scenario A + on-demand Scenario C.

Architecture (CLAUDE.md §12):
- App startup: load config + inputs + grid, build boundary CHD, run (or
  load cached) steady-state IC, run (or load cached) Scenario A. These
  are reused across all requests.
- POST /scenarios: runs Scenario C only with a user-supplied well change
  set (single / multi / trade), combines with cached A by superposition,
  returns drawdowns.
- GET /baseline: cached Scenario A drawdowns at all spring complexes.
- GET /map-data: GeoJSON layers for the frontend map.
- GET /existing-bores: list of licensed pumping bores for the trade UI.
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
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from ..io_layer import Inputs, ML_PER_YEAR_TO_M3_PER_DAY, load_inputs, load_recharge_by_inode
from ..model_builder import active_boundary_chd_cells, boundary_ghb_for_config
from ..scenarios import ScenarioResult, run_scenario, run_steady_state
from ..superposition import combine_receptor_tables
from ..theis import formation_avg_T_S, theis_at_springs
from . import cache as cache_mod
from . import decisions as decisions_mod
from .schemas import (
    BaselineResponse,
    ComplexDrawdown,
    Decision,
    DecisionsResponse,
    ExistingBore,
    ExistingBoresResponse,
    HealthResponse,
    ProposedBore,
    RecordDecisionRequest,
    RollbackRequest,
    ScenarioJobStatus,
    ScenarioQA,
    ScenarioRequest,
    ScenarioResponse,
    TheisDiagnostics,
    WellSpec,
    YearResults,
)

# QA thresholds. Budget imbalance above 1% or boundary drawdown above 5 cm
# flags the result; these are warnings, not blocks — the regulator sees them.
MASS_BALANCE_WARN_PCT = 1.0
BOUNDARY_WARN_M = 0.05

# How many completed scenario runs to keep in memory for the drawdown-maps
# page. Two regulators iterating concurrently stay well within this.
SCENARIO_STORE_MAX = 8


class _ScenarioJob:
    """In-memory job record for a background scenario run."""

    def __init__(self, job_id: str, req: ScenarioRequest):
        self.id = job_id
        self.req = req
        self.status: str = "queued"
        self.progress: str = "waiting for a model slot"
        self.created_at: str = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.result: ScenarioResponse | None = None
        self.error: str | None = None

    def to_status(self) -> ScenarioJobStatus:
        return ScenarioJobStatus(
            job_id=self.id, status=self.status, progress=self.progress,
            created_at=self.created_at, result=self.result, error=self.error,
        )


class _State:
    cfg: Config | None = None
    config_path: Path | None = None
    inputs: Inputs | None = None
    grid: Grid | None = None
    ic_head: np.ndarray | None = None
    chd_cells: list | None = None
    drn_cells: list | None = None                     # outcrop drains (steady state)
    ghb_cells: list | None = None                     # linearised drains (transient)
    boundary_ghb: list | None = None                  # truncation-face far-field GHBs
    workspace_root: Path | None = None
    baseline: cache_mod.BaselineCache | None = None
    complex_centroids_4326: dict | None = None      # GeoJSON FeatureCollection
    setup_geojson: dict | None = None                 # layer -> FeatureCollection
    decisions_path: Path | None = None                # append-only audit-trail store
    aquifers_geojson: dict | None = None              # GeoJSON FC of GABORA units/subareas
    provenance: dict | None = None                    # config/input hashes + mf6 version

    def __init__(self):
        # Job registry for background scenario runs. run_lock serialises
        # MF6 executions (one at a time); jobs queue behind it.
        self.jobs: dict[str, _ScenarioJob] = {}
        self.jobs_lock = threading.Lock()
        self.run_lock = threading.Lock()
        # Completed runs keyed by job id, for the drawdown-maps page.
        # OrderedDict as LRU: oldest evicted past SCENARIO_STORE_MAX.
        self.scenario_store: OrderedDict[str, dict] = OrderedDict()


state = _State()


def _store_scenario(job_id: str, entry: dict) -> None:
    with state.jobs_lock:
        state.scenario_store[job_id] = entry
        while len(state.scenario_store) > SCENARIO_STORE_MAX:
            state.scenario_store.popitem(last=False)


def _get_scenario(job_id: str | None) -> dict | None:
    """Entry for `job_id`, or the most recently completed run if None."""
    with state.jobs_lock:
        if job_id is not None:
            return state.scenario_store.get(job_id)
        if state.scenario_store:
            return next(reversed(state.scenario_store.values()))
    return None


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
        ghb_cells=(state.boundary_ghb or []) + (state.ghb_cells or []),
        drain_ghb_cells=state.ghb_cells or [],
    )
    cache = cache_mod.BaselineCache(
        key=key,
        receptors_df=result.receptors_df.copy(),
        drawdown_by_year=result.drawdown_at_output_years,
        complex_series_df=result.complex_series_df.copy(),
        nopump_times_days=result.times_days,
        nopump_heads=result.heads_nopump,
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

    # Rejected-recharge drains (parent-model device): the parent model's
    # RIV cells (calibrated stage/conductance) when configured; otherwise
    # outcrop cells drain at the minimum DEM elevation within the cell.
    drn_cells = []
    if state.cfg.drains.enabled:
        from ..drains import drain_cells_for_config
        try:
            drn_cells, drn_source = drain_cells_for_config(state.cfg, grid)
            print(f"[drains] {len(drn_cells)} drain cells ({drn_source})")
        except Exception as exc:
            print(f"[drains] disabled — failed to build drain cells: {exc}")
    state.drn_cells = drn_cells

    quadrants = state.cfg.assessment.chd_quadrants
    mode = state.cfg.assessment.boundary_mode
    chd_filtered = active_boundary_chd_cells(
        grid, exclude_mask=grid.outcrop_mask, quadrants=quadrants,
    )
    chd_unfiltered = active_boundary_chd_cells(grid)
    ghb_ws, ghb_source = boundary_ghb_for_config(state.cfg, grid)

    # Attempt ladder ordered by the configured boundary mode. The boundary
    # audit showed the perimeter is almost entirely a thin pinch-out fringe,
    # and UWIR 2019 Fig A1-14 places GHBs only on the W/S truncation faces —
    # so "uwir_ghb" (closed pinch-outs + far-field GHBs where the formation
    # is cut by the domain frame, drains as the steady-state outlet) is the
    # preferred default. CHD configs remain as convergence fallbacks only,
    # and falling back is reported loudly because it changes the
    # conceptual model.
    attempts: list[tuple[str, list, list]] = []   # (label, chd, boundary_ghb)
    if mode in ("uwir_ghb", "no_flow") and not drn_cells:
        print(f"[boundary] WARNING: {mode} mode without drains — the "
              "steady state has no outlet for recharge and will likely "
              "fail; enable drains or switch boundary_mode.")
    if mode == "uwir_ghb":
        attempts.append((
            f"no-flow pinch-outs + {len(ghb_ws)} truncation-face GHBs "
            f"({ghb_source}) + drains",
            [], ghb_ws,
        ))
    if mode in ("uwir_ghb", "no_flow"):
        attempts.append(("no-flow perimeter (pinch-out) + drains", [], []))
    if mode in ("uwir_ghb", "no_flow", "chd_quadrants"):
        attempts.append((f"CHD quadrants={quadrants or 'all'} + outcrop excluded", chd_filtered, []))
    attempts.append(("all-edges CHD", chd_unfiltered, []))

    from ..model_builder import anchor_ghb_cells
    for i, (label, chd, bghb) in enumerate(attempts):
        # Weak anchors for active islands with no BC in THIS attempt's
        # configuration — without them the steady state is singular.
        bc_cells = {(rec[1], rec[2]) for rec in chd}
        bc_cells |= {(rec[1], rec[2]) for rec in bghb}
        bc_cells |= {(rec[1], rec[2]) for rec in drn_cells}
        anchors = anchor_ghb_cells(grid, bc_cells)
        if anchors:
            print(f"[boundary] {len(anchors)} weak anchor GHBs added for "
                  f"BC-less active islands")
        bghb_eff = list(bghb) + anchors
        try:
            ic = run_steady_state(state.cfg, grid, workspace, chd_cells=chd,
                                  drn_cells=drn_cells, ghb_cells=bghb_eff)
            state.chd_cells = chd
            state.boundary_ghb = bghb_eff
            state.ic_head = ic
            _linearise_drains(ic)
            if i > 0:
                print(f"[boundary] WARNING: configured boundary_mode='{mode}' "
                      f"did not converge; fell back to '{label}'. The "
                      f"conceptual model differs from the configured one.")
            print(f"[boundary] steady-state converged with {label} "
                  f"({len(chd)} CHD, {len(bghb)} boundary-GHB, "
                  f"{len(anchors)} anchor cells)")
            return
        except RuntimeError as exc:
            print(f"[boundary] steady-state failed with {label}: {exc}")

    print("[boundary] all steady-state attempts failed; using uniform IC")
    active = grid.idomain[0] == 1
    mean_top = float(np.nanmean(np.where(active, grid.top, np.nan)))
    state.ic_head = np.full_like(grid.top, mean_top)
    # Use the safer (all-edges) CHD with the uniform IC.
    state.chd_cells = chd_unfiltered
    state.boundary_ghb = []
    _linearise_drains(state.ic_head)


def _linearise_drains(ss_head: np.ndarray) -> None:
    """Convert drains flowing at steady state into linear GHBs for the
    transient runs (see src/drains.py). Sets state.ghb_cells."""
    if not state.drn_cells:
        state.ghb_cells = []
        return
    from ..drains import linearise_drains
    state.ghb_cells = linearise_drains(state.drn_cells, ss_head)
    print(f"[drains] {len(state.ghb_cells)} of {len(state.drn_cells)} drains "
          f"flowing at steady state → linearised as GHB for transient runs")


def _mf6_version() -> str:
    try:
        out = subprocess.run(
            ["mf6", "--version"], capture_output=True, text=True, timeout=15,
        ).stdout
        for line in out.splitlines():
            if "mf6" in line.lower() or "version" in line.lower():
                return line.strip()
        return out.strip().splitlines()[0] if out.strip() else "unknown"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "unknown"


def _build_provenance() -> dict:
    """Traceability bundle: what code/config/data produced these numbers."""
    cfg = state.cfg
    return {
        "config_sha256": cache_mod._file_sha256(state.config_path)[:16],
        "properties_sha256": cache_mod._file_sha256(Path(cfg.inputs.properties_csv))[:16],
        "water_use_sha256": cache_mod._file_sha256(Path(cfg.inputs.water_use.path))[:16],
        "baseline_cache_key": cache_mod.baseline_key(cfg, state.config_path),
        "cache_schema": cache_mod.CACHE_SCHEMA_VERSION,
        "mf6_version": _mf6_version(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = Path(os.environ.get("PRECIPICE_CONFIG", "config.yaml"))
    state.cfg = load_config(config_path)
    state.config_path = config_path
    state.inputs = load_inputs(state.cfg)
    state.grid = build_grid_from_properties(
        state.inputs.properties, state.cfg.project.crs,
        layer=state.cfg.grid.properties_layer,
        recharge_by_inode=load_recharge_by_inode(state.cfg),
        recharge_fallback_m_per_day=state.cfg.inputs.recharge_fallback_m_per_day,
        outcrop_storage=state.cfg.assessment.outcrop_storage,
    )
    state.workspace_root = Path(state.cfg.run.workspace_root)
    state.workspace_root.mkdir(parents=True, exist_ok=True)
    # Audit trail lives under var/ — a dedicated runtime-data directory,
    # NOT outputs/ (which is treated as disposable by cleanups/redeploys).
    state.decisions_path = Path("var") / "decision_events.jsonl"
    state.provenance = _build_provenance()

    _bootstrap_ic()
    state.baseline = _bootstrap_baseline()
    state.complex_centroids_4326 = _build_complex_centroids(
        state.inputs.springs, state.cfg.assessment.spring_complex_col,
    )
    state.setup_geojson = _build_setup_geojson()
    state.aquifers_geojson = _build_aquifers_geojson()
    yield


app = FastAPI(
    title="Precipice Sandstone — Water Licence Impact API",
    version="0.3.0",
    lifespan=lifespan,
)


def _df_to_year_results(combined: pd.DataFrame, threshold: float) -> list[YearResults]:
    """Build YearResults with three threshold classifications per complex:

    - already_exceeded: s_approved_m >= threshold (existing licences alone
      already cause an exceedance — informational, not the proposal's fault).
    - triggered_by_proposed: s_approved_m < threshold but s_total >= threshold
      (the proposal is what tips this complex over — the regulatory
      decision-maker).
    - exceeds_threshold: s_total >= threshold (union of the two — kept for
      back-compat).
    """
    out: list[YearResults] = []
    has_theis = "drawdown_m_theis" in combined.columns
    has_r = "r_m" in combined.columns
    has_n = "n_springs" in combined.columns
    # Receptors closer than ~2 cells to a proposed well get a mesh-
    # dependence flag: the FD solution in/next to the well cell is biased
    # by the point-sink-in-a-finite-cell treatment (CLAUDE.md §10).
    mesh_limit_m = 2.0 * float(state.grid.delr[0]) if state.grid is not None else 3000.0
    for y in sorted(combined["time_years"].unique()):
        sub = combined[combined["time_years"] == y].sort_values("s_total", ascending=False)
        complexes: list[ComplexDrawdown] = []
        for _, r in sub.iterrows():
            s_appr = float(r["s_approved"])
            s_tot = float(r["s_total"])
            already = s_appr >= threshold
            exceeds = s_tot >= threshold
            triggered = exceeds and not already
            r_m = float(r["r_m"]) if has_r and not pd.isna(r["r_m"]) else None
            complexes.append(ComplexDrawdown(
                complex_id=str(r["receptor_id"]),
                n_springs=int(r["n_springs"]) if has_n and not pd.isna(r["n_springs"]) else 1,
                s_approved_m=s_appr,
                s_additional_m=float(r["s_additional"]),
                s_total_m=s_tot,
                s_additional_theis_m=float(r["drawdown_m_theis"]) if has_theis and not pd.isna(r["drawdown_m_theis"]) else None,
                r_to_proposed_m=r_m,
                exceeds_threshold=exceeds,
                already_exceeded=already,
                triggered_by_proposed=triggered,
                mesh_dependent=(r_m is not None and r_m < mesh_limit_m),
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


@app.get("/api/version")
def version_info():
    """Quick way to verify which build the API process is actually running.

    `property_renderer` is "pil" if the new pixel-perfect PIL pipeline
    is in use; if the process is still on the old matplotlib renderer
    this endpoint won't exist at all (404 → restart needed).
    """
    return JSONResponse({
        "property_renderer": "pil-warped-3857",
        "has_property_sample": True,
        "build": "2026-07-13-mercator-warp",
    })


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


def _build_wells_from_request(req: ScenarioRequest) -> list[dict]:
    """Resolve the scenario request to a list of well-change dicts.

    Each entry: {label, x, y, rate_ML_per_year}. Positive rate = adding
    extraction, negative rate = removing existing extraction (used for
    trade scenarios).
    """
    if req.scenario_type == "single":
        if req.proposed_bore is None:
            raise HTTPException(400, "single mode requires `proposed_bore`")
        pb = req.proposed_bore
        return [{"label": pb.bore_id, "x": pb.x, "y": pb.y,
                 "rate_ML_per_year": pb.rate_ML_per_year}]

    if req.scenario_type == "multi":
        if not req.new_wells:
            raise HTTPException(400, "multi mode requires at least one entry in `new_wells`")
        return [w.model_dump() for w in req.new_wells]

    if req.scenario_type == "trade":
        if not req.from_bore_id:
            raise HTTPException(400, "trade mode requires `from_bore_id`")
        bores = state.inputs.pumping_bores
        if "bore_id" not in bores.columns:
            raise HTTPException(500, "pumping bores lack a `bore_id` column")
        match = bores[bores["bore_id"].astype(str) == str(req.from_bore_id)]
        if match.empty:
            raise HTTPException(404, f"existing bore {req.from_bore_id} not found")
        row = match.iloc[0]
        from_x = float(row.geometry.x)
        from_y = float(row.geometry.y)
        source_rate = float(row["rate_m3_per_day"]) / ML_PER_YEAR_TO_M3_PER_DAY

        # Resolve destination(s). Prefer to_wells (multi-destination); fall
        # back to a single destination at (to_x, to_y) carrying the full
        # source rate (back-compat).
        if req.to_wells:
            dests = [
                {"x": float(d.x), "y": float(d.y),
                 "rate_ML_per_year": float(d.rate_ML_per_year)}
                for d in req.to_wells
            ]
        elif req.to_x is not None and req.to_y is not None:
            dests = [{"x": float(req.to_x), "y": float(req.to_y),
                      "rate_ML_per_year": source_rate}]
        else:
            raise HTTPException(400, "trade mode requires `to_wells` or `(to_x, to_y)`")

        total_dest = sum(d["rate_ML_per_year"] for d in dests)
        # Allow 0.1% slack for floating-point round-off.
        if total_dest > source_rate * 1.001:
            raise HTTPException(
                400,
                f"trade is over-subscribed: destinations sum to {total_dest:.2f} ML/yr "
                f"but source ({req.from_bore_id}) only carries {source_rate:.2f} ML/yr",
            )

        # Build the change set: +rate at each destination, -total at the source.
        # The source is taken out by the actual *destination total*, not the
        # full source rate — so a partial trade (sum < source) leaves some
        # extraction at the original location.
        out: list[dict] = [
            {"label": f"to[{i + 1}] {req.from_bore_id}", "x": d["x"], "y": d["y"],
             "rate_ML_per_year": d["rate_ML_per_year"]}
            for i, d in enumerate(dests)
        ]
        out.append({"label": f"from {req.from_bore_id}", "x": from_x, "y": from_y,
                    "rate_ML_per_year": -total_dest})
        return out

    raise HTTPException(400, f"unknown scenario_type: {req.scenario_type}")


def _execute_scenario(
    req: ScenarioRequest, job_id: str, progress=lambda msg: None,
) -> ScenarioResponse:
    """Run Scenario C for `req` and assemble the full response.

    Called from a job worker thread (holding state.run_lock). Raises
    ValueError for bad requests and RuntimeError for solver failures —
    the job wrapper maps those onto the job's error state.
    """
    wells_dicts = _build_wells_from_request(req)
    proposed_wells_xy_rate = [(w["x"], w["y"], w["rate_ML_per_year"]) for w in wells_dicts]

    # Recharge multiplier change re-runs the IC and re-baselines Scenario A
    # against a different cache slot. Both the steady-state IC and the
    # cached A baseline are tied to the multiplier, so the cache key
    # automatically picks the right slot or computes fresh if missing.
    if req.recharge_multiplier != state.cfg.assessment.recharge_multiplier:
        progress("re-baselining for recharge multiplier")
        state.cfg.assessment.recharge_multiplier = req.recharge_multiplier
        _bootstrap_ic()
        state.baseline = _bootstrap_baseline()

    t0 = time.time()
    label_safe = "".join(c if c.isalnum() else "_" for c in (wells_dicts[0]["label"] or "scen_C"))[:40]
    workspace = state.workspace_root / f"scen_C_{label_safe}"

    # Reuse the cached no-pump twin — identical for every scenario with
    # this config, so each request only pays for one MF6 run.
    nopump_twin = None
    if state.baseline.nopump_times_days is not None and state.baseline.nopump_heads is not None:
        nopump_twin = (state.baseline.nopump_times_days, state.baseline.nopump_heads)

    progress("running MODFLOW 6")
    c_result = run_scenario(
        state.cfg, state.grid, state.inputs, "C",
        state.ic_head, workspace, chd_cells=state.chd_cells,
        ghb_cells=(state.boundary_ghb or []) + (state.ghb_cells or []),
        drain_ghb_cells=state.ghb_cells or [],
        proposed_wells=proposed_wells_xy_rate,
        nopump_twin=nopump_twin,
    )
    runtime = time.time() - t0
    progress("sampling receptors")

    # Retain Scenario C grids for the drawdown-maps page, keyed by job so
    # concurrent regulators don't clobber each other's results.
    positive = [w for w in wells_dicts if w["rate_ML_per_year"] > 0]
    proposed_bore_entry = None
    if positive:
        first = positive[0]
        proposed_bore_entry = {
            "bore_id": first["label"],
            "x": first["x"], "y": first["y"],
            "rate_ML_per_year": first["rate_ML_per_year"],
        }
    _store_scenario(job_id, {
        "wells_run": wells_dicts,
        "c_drawdown_by_year": c_result.drawdown_at_output_years,
        "c_series_df": c_result.complex_series_df.copy(),
        "proposed_bore": proposed_bore_entry,
    })

    combined = combine_receptor_tables(
        state.baseline.receptors_df,
        c_result.receptors_df,
    )

    # Theis comparison: sum the analytical drawdown contribution from every
    # well in the change set (linear superposition). Signs follow rate.
    theis_diag: TheisDiagnostics | None = None
    if state.inputs.springs is not None and len(state.inputs.springs):
        spring_id_col = state.cfg.assessment.spring_id_col
        complex_col = state.cfg.assessment.spring_complex_col
        if spring_id_col not in state.inputs.springs.columns:
            spring_id_col = state.inputs.springs.columns[0]
        output_years_sorted = sorted(combined["time_years"].unique())
        # Formation-averaged T/S: representative homogeneous-equivalent
        # values across active cells. Using these (rather than the T/S
        # at whichever cell the proposed bore lands on) keeps the Theis
        # column comparable across scenarios and avoids the outlier
        # behaviour when a bore happens to sit in a low-K cell.
        try:
            T_form, S_form = formation_avg_T_S(state.grid)
        except ValueError:
            T_form = S_form = None

        theis_merged = None
        for w in wells_dicts:
            rate_m3d = float(w["rate_ML_per_year"]) * ML_PER_YEAR_TO_M3_PER_DAY
            try:
                df = theis_at_springs(
                    state.grid, state.inputs.springs, spring_id_col,
                    float(w["x"]), float(w["y"]), rate_m3d,
                    output_years=output_years_sorted,
                    complex_col=complex_col if complex_col in state.inputs.springs.columns else None,
                    T=T_form, S=S_form,
                )
            except ValueError:
                continue
            # theis_at_springs always returns positive drawdown for |Q|;
            # recover the sign of the contribution from rate.
            sign = 1.0 if rate_m3d >= 0 else -1.0
            df = df[["receptor_id", "time_years", "drawdown_m_theis", "r_m"]].copy()
            df["drawdown_m_theis"] = df["drawdown_m_theis"] * sign
            if theis_merged is None:
                theis_merged = df.rename(columns={"r_m": "r_m_first"})
            else:
                m = df.rename(columns={"drawdown_m_theis": "_dd2", "r_m": "_r2"})
                theis_merged = theis_merged.merge(
                    m, on=["receptor_id", "time_years"], how="outer",
                )
                theis_merged["drawdown_m_theis"] = (
                    theis_merged["drawdown_m_theis"].fillna(0) + theis_merged["_dd2"].fillna(0)
                )
                theis_merged["r_m_first"] = theis_merged[["r_m_first", "_r2"]].min(axis=1)
                theis_merged = theis_merged.drop(columns=["_dd2", "_r2"])
        if theis_merged is not None:
            combined = combined.merge(
                theis_merged.rename(columns={"r_m_first": "r_m"}),
                on=["receptor_id", "time_years"], how="left",
            )
        # Diagnostic T/S = formation-averaged values used for the Theis
        # column. `well_cell` still reports the first positive-rate well's
        # grid cell for traceability.
        if positive and T_form is not None and S_form is not None:
            rc = cell_of(state.grid, float(positive[0]["x"]), float(positive[0]["y"]))
            well_cell = [rc[0], rc[1]] if rc is not None else [-1, -1]
            theis_diag = TheisDiagnostics(
                T_m2_per_day=T_form, S_dimensionless=S_form, well_cell=well_cell,
            )

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

    proposed_bore_echo = None
    if positive:
        p = positive[0]
        proposed_bore_echo = ProposedBore(
            bore_id=p["label"], x=p["x"], y=p["y"],
            rate_ML_per_year=p["rate_ML_per_year"],
        )

    qa = ScenarioQA(
        max_pct_discrepancy=c_result.max_pct_discrepancy,
        chd_max_drawdown_m=c_result.chd_max_drawdown_m,
        noflow_max_drawdown_m=c_result.noflow_max_drawdown_m,
        boundary_warning=(
            max(c_result.chd_max_drawdown_m, c_result.noflow_max_drawdown_m)
            >= BOUNDARY_WARN_M
        ),
        mass_balance_warning=c_result.max_pct_discrepancy >= MASS_BALANCE_WARN_PCT,
        n_drain_reversals=c_result.n_drain_reversals,
        drain_warning=c_result.n_drain_reversals > 0,
    )

    return ScenarioResponse(
        scenario_type=req.scenario_type,
        wells_run=[WellSpec(**w) for w in wells_dicts],
        proposed_bore=proposed_bore_echo,
        output_years=[yr.time_years for yr in year_results],
        regulatory_threshold_m=threshold,
        by_year=year_results,
        top_n_total=top_n,
        n_exceedances_any_year=len(exceedance_ids),
        n_triggered_any_year=len(triggered_ids),
        n_already_exceeded_any_year=len(already_ids),
        runtime_seconds=runtime,
        theis=theis_diag,
        qa=qa,
        provenance=state.provenance,
        job_id=job_id,
    )


def _run_job(job: _ScenarioJob) -> None:
    """Worker-thread entry: serialise MF6 runs behind run_lock."""
    def progress(msg: str) -> None:
        job.progress = msg

    with state.run_lock:
        job.status = "running"
        job.progress = "starting"
        try:
            job.result = _execute_scenario(job.req, job.id, progress)
            job.status = "done"
            job.progress = "complete"
        except ValueError as exc:            # bad request (e.g. bore off-domain)
            job.status = "error"
            job.error = str(exc)
        except Exception as exc:             # solver failure or bug — surface it
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"


@app.post("/api/scenarios", response_model=ScenarioJobStatus, status_code=202)
def submit_scenario(req: ScenarioRequest) -> ScenarioJobStatus:
    """Queue a scenario run and return immediately with a job id.

    Poll GET /api/scenarios/jobs/{job_id} for progress; `result` is the
    full ScenarioResponse once status == "done".
    """
    if state.baseline is None:
        raise HTTPException(503, "Baseline not ready")
    # Validate the change set eagerly so an obviously-bad request fails
    # now with a 400 rather than minutes later inside the job.
    _build_wells_from_request(req)

    job = _ScenarioJob(uuid.uuid4().hex[:12], req)
    with state.jobs_lock:
        state.jobs[job.id] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job.to_status()


@app.get("/api/scenarios/jobs/{job_id}", response_model=ScenarioJobStatus)
def scenario_job_status(job_id: str) -> ScenarioJobStatus:
    with state.jobs_lock:
        job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job id: {job_id}")
    return job.to_status()


@app.get("/api/existing-bores", response_model=ExistingBoresResponse)
def existing_bores() -> ExistingBoresResponse:
    """List existing licensed pumping bores for the trade-mode selector."""
    if state.inputs is None:
        raise HTTPException(503, "Inputs not ready")
    bores = state.inputs.pumping_bores
    if bores is None or len(bores) == 0:
        return ExistingBoresResponse(bores=[])

    # Only show bores inside the formation extent — out-of-domain bores
    # aren't tradeable through this tool.
    extent = state.inputs.formation_extent
    if extent is not None and len(extent):
        domain = extent.unary_union
        bores = bores[bores.within(domain)]
        if len(bores) == 0:
            return ExistingBoresResponse(bores=[])

    transformer = pyproj.Transformer.from_crs(state.cfg.project.crs, "EPSG:4326", always_xy=True)
    out: list[ExistingBore] = []
    has_id = "bore_id" in bores.columns
    for _, row in bores.iterrows():
        x = float(row.geometry.x)
        y = float(row.geometry.y)
        lng, lat = transformer.transform(x, y)
        rate_ml = float(row["rate_m3_per_day"]) / ML_PER_YEAR_TO_M3_PER_DAY
        out.append(ExistingBore(
            bore_id=str(row["bore_id"]) if has_id else f"row{int(row.name)}",
            x=x, y=y, lng=float(lng), lat=float(lat),
            rate_ML_per_year=rate_ml,
        ))
    # Largest rates first — typical trade interest.
    out.sort(key=lambda b: -b.rate_ML_per_year)
    return ExistingBoresResponse(bores=out)


# --- Decision audit trail ------------------------------------------------

def _decisions_path() -> Path:
    if state.decisions_path is None:
        raise HTTPException(503, "Decisions store not initialised")
    return state.decisions_path


def _active_head_id(decisions: list[dict]) -> str | None:
    """Most-recent active approve in the list (already newest-first)."""
    for d in decisions:
        if d.get("decision") == "approve" and d.get("status") == "active":
            return d.get("id")
    return None


@app.get("/api/decisions", response_model=DecisionsResponse)
def list_decisions() -> DecisionsResponse:
    items = decisions_mod.list_decisions(_decisions_path())
    return DecisionsResponse(
        decisions=[Decision(**d) for d in items],
        active_head_id=_active_head_id(items),
    )


@app.post("/api/decisions", response_model=Decision)
def record_decision(req: RecordDecisionRequest) -> Decision:
    record = decisions_mod.record_decision(
        _decisions_path(),
        decision=req.decision,
        regulator=req.regulator,
        scenario=req.scenario.model_dump(),
        summary=req.summary.model_dump(),
        note=req.note,
    )
    return Decision(**record)


@app.post("/api/decisions/{decision_id}/rollback", response_model=DecisionsResponse)
def rollback_decision(decision_id: str, req: RollbackRequest) -> DecisionsResponse:
    try:
        decisions_mod.rollback_to(_decisions_path(), decision_id, req.regulator)
    except KeyError:
        raise HTTPException(404, f"unknown decision id: {decision_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    items = decisions_mod.list_decisions(_decisions_path())
    return DecisionsResponse(
        decisions=[Decision(**d) for d in items],
        active_head_id=_active_head_id(items),
    )


@app.get("/api/spring-series")
def spring_series(complex_id: str | None = None, job: str | None = None):
    """Per-complex drawdown time series across every model timestep."""
    if state.baseline is None or state.baseline.complex_series_df is None:
        raise HTTPException(503, "Baseline not ready")
    base = state.baseline.complex_series_df
    entry = _get_scenario(job)

    if complex_id is None:
        live = entry["c_series_df"] if entry else None
        ranked = (base.groupby("complex_id")["drawdown_m"].max().rename("peak_s_approved_m"))
        out = ranked.reset_index().to_dict("records")
        if live is not None and len(live):
            peak_c = live.groupby("complex_id")["drawdown_m"].max()
            for row in out:
                row["peak_s_additional_m"] = float(peak_c.get(row["complex_id"], 0.0))
        return JSONResponse({"complexes": out})

    base_c = base[base["complex_id"] == complex_id].sort_values("time_days")
    if base_c.empty:
        raise HTTPException(404, f"complex_id '{complex_id}' not found")

    times_days = base_c["time_days"].to_numpy()
    s_approved = base_c["drawdown_m"].to_numpy()
    YEAR_DAYS_LOC = 365.25
    times_years = (times_days / YEAR_DAYS_LOC).tolist()

    n_springs = 0
    if state.inputs is not None and state.inputs.springs is not None:
        col = state.cfg.assessment.spring_complex_col
        if col in state.inputs.springs.columns:
            n_springs = int((state.inputs.springs[col] == complex_id).sum())

    payload = {
        "complex_id": complex_id,
        "n_springs": n_springs,
        "threshold_m": state.cfg.assessment.regulatory_threshold_m,
        "times_years": times_years,
        "s_approved_m": s_approved.tolist(),
        "s_additional_m": None,
        "s_total_m": None,
    }

    live = entry["c_series_df"] if entry else None
    if live is not None and len(live):
        live_c = live[live["complex_id"] == complex_id].sort_values("time_days")
        if not live_c.empty:
            merged = base_c.merge(
                live_c, on="time_days", how="inner",
                suffixes=("_a", "_c"),
            )
            payload["times_years"] = (merged["time_days"].to_numpy() / YEAR_DAYS_LOC).tolist()
            payload["s_approved_m"] = merged["drawdown_m_a"].tolist()
            payload["s_additional_m"] = merged["drawdown_m_c"].tolist()
            payload["s_total_m"] = (merged["drawdown_m_a"] + merged["drawdown_m_c"]).tolist()
    return JSONResponse(payload)


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


_BLUE_RED_CMAP = mcolors.LinearSegmentedColormap.from_list(
    "drawdown_jet",
    # Matplotlib-jet-style ramp: deep blue → blue → cyan → green → yellow → orange → red.
    ["#00007f", "#0000ff", "#007fff", "#00ffff",
     "#7fff7f", "#ffff00", "#ff7f00", "#ff0000", "#7f0000"],
)
_BLUE_RED_CMAP.set_bad(alpha=0.0)

DRAWDOWN_DISPLAY_FLOOR_M = 0.2


def _warp_upscale_factor() -> int:
    """Upscale so MapLibre's bilinear filter only bleeds a fraction of a
    cell, while capping the output image at ~4096 px on its long side so a
    large grid can't produce a hundred-megabyte PNG."""
    meta = _property_warp_meta()
    longest = max(meta["dst_width"], meta["dst_height"])
    return max(1, min(8, 4096 // max(1, longest)))


def _rgba_warp_to_png(rgba: np.ndarray) -> bytes:
    """Warp an MGA-space (nrow, ncol, 4) RGBA array to Web Mercator and
    encode as PNG. Shared by the drawdown and property overlays so they
    carry identical georeferencing (the corners from _property_warp_meta).

    Why warp instead of just supplying four corners to MapLibre? An image
    source is texture-mapped linearly *in mercator space* between its four
    corners. Any raster that isn't mercator-axis-aligned (raw MGA, or an
    EPSG:4326 equirectangular warp) is exact at the corners but drifts in
    the interior — kilometres at this domain's scale. Warping to
    EPSG:3857 makes the GPU's linear interpolation exact everywhere, so
    the raster registers with the vector layers at any zoom.
    """
    from rasterio.warp import reproject, Resampling

    meta = _property_warp_meta()
    dst_rgba = np.zeros((meta["dst_height"], meta["dst_width"], 4), dtype=np.uint8)
    for band in range(4):
        reproject(
            source=np.ascontiguousarray(rgba[..., band]),
            destination=dst_rgba[..., band],
            src_transform=meta["src_transform"],
            src_crs=meta["src_crs"],
            dst_transform=meta["dst_transform"],
            dst_crs=meta["dst_crs"],
            resampling=Resampling.nearest,
        )

    # Block-upscale so MapLibre's image-source bilinear filter (which
    # ignores raster-resampling for image sources) only blurs across a
    # fraction of a cell boundary.
    up = _warp_upscale_factor()
    if up > 1:
        dst_rgba = np.repeat(np.repeat(dst_rgba, up, axis=0), up, axis=1)

    from PIL import Image
    img = Image.fromarray(dst_rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def _drawdown_to_png(arr: np.ndarray, idomain: np.ndarray, vmax: float | None = None) -> bytes:
    """Render a (nrow, ncol) drawdown grid to a transparent, warped PNG."""
    masked = np.where(idomain == 1, arr, np.nan)
    masked = np.where(masked >= DRAWDOWN_DISPLAY_FLOOR_M, masked, np.nan)
    show = ~np.isnan(masked)
    valid = masked[show]
    if vmax is None:
        vmax = float(np.nanpercentile(np.abs(valid), 99)) if valid.size else 1.0
        vmax = max(vmax, DRAWDOWN_DISPLAY_FLOOR_M + 0.1)

    norm01 = (np.where(show, masked, 0.0) - DRAWDOWN_DISPLAY_FLOOR_M) / (vmax - DRAWDOWN_DISPLAY_FLOOR_M)
    norm01 = np.clip(norm01, 0.0, 1.0)
    rgba = (_BLUE_RED_CMAP(norm01) * 255.0).astype(np.uint8)
    rgba[..., 3] = np.where(show, 255, 0).astype(np.uint8)
    return _rgba_warp_to_png(rgba)


def _property_warp_meta():
    """Cached warp parameters for reprojecting rasters from the project CRS
    to **EPSG:3857 (Web Mercator)**. Computed once (grid-extent-only).

    Why 3857 and not 4326: MapLibre renders in mercator space and an
    `image` source is texture-mapped *linearly in mercator coordinates*
    between its four corners. A raster that is linear in latitude
    (EPSG:4326) therefore drifts in the interior — for this domain
    (23–29°S) the mid-image error is ~4 km (≈2.7 cells) southward, zero
    at the corners. Warping to a mercator-axis-aligned raster makes the
    GPU's linear interpolation exact everywhere.

    Returns the source/destination affine transforms, destination raster
    size, and the four image corners in lng/lat `[tl, tr, br, bl]` order
    (MapLibre wants corner coordinates in lng/lat even though it maps
    them in mercator space).
    """
    cached = getattr(state, "_property_warp_cache", None)
    if cached is not None:
        return cached

    from rasterio.transform import from_origin
    from rasterio.warp import calculate_default_transform

    g = state.grid
    src_crs = state.cfg.project.crs
    src_transform = from_origin(
        g.xorigin, g.yorigin + float(g.delc.sum()),
        float(g.delr[0]), float(g.delc[0]),
    )
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs, "EPSG:3857", g.ncol, g.nrow,
        g.xorigin, g.yorigin,
        g.xorigin + float(g.delr.sum()),
        g.yorigin + float(g.delc.sum()),
    )
    west  = dst_transform.c
    north = dst_transform.f
    east  = west  + dst_transform.a * dst_width
    south = north + dst_transform.e * dst_height
    # Corner lng/lats of the mercator-aligned rectangle.
    to_ll = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    tl = list(to_ll.transform(west, north))
    tr_ = list(to_ll.transform(east, north))
    br = list(to_ll.transform(east, south))
    bl = list(to_ll.transform(west, south))
    cached = {
        "src_transform": src_transform,
        "src_crs": src_crs,
        "dst_crs": "EPSG:3857",
        "dst_transform": dst_transform,
        "dst_width": int(dst_width),
        "dst_height": int(dst_height),
        "image_corners_4326": [tl, tr_, br, bl],
    }
    state._property_warp_cache = cached
    return cached


def _property_to_png(
    arr: np.ndarray,
    idomain: np.ndarray,
    cmap_name: str,
    vmin: float,
    vmax: float,
) -> bytes:
    """Render the K / Ss field on a log10 colour scale to a warped PNG."""
    pos = (idomain == 1) & (arr > 0)
    log_min = np.log10(max(vmin, 1e-30))
    log_max = np.log10(max(vmax, vmin * 10))
    log_arr = np.full(arr.shape, np.nan, dtype=np.float64)
    log_arr[pos] = np.log10(arr[pos])
    norm = (log_arr - log_min) / (log_max - log_min)
    norm = np.where(np.isnan(norm), 0.0, np.clip(norm, 0.0, 1.0))

    cmap = plt.get_cmap(cmap_name)
    rgba = (cmap(norm) * 255.0).astype(np.uint8)        # (nrow, ncol, 4)
    rgba[..., 3] = np.where(pos, 255, 0).astype(np.uint8)
    return _rgba_warp_to_png(rgba)


def _property_stats(arr: np.ndarray, idomain: np.ndarray) -> tuple[float, float, float]:
    """Return (p2, p98, median) of positive values in active cells.

    The 2nd / 98th percentiles clip the colour ramp without being thrown
    off by a handful of extreme outliers from the calibrated field.
    """
    pos = arr[(idomain == 1) & (arr > 0)]
    if pos.size == 0:
        return 1e-6, 1.0, 1e-3
    return (
        float(np.percentile(pos, 2)),
        float(np.percentile(pos, 98)),
        float(np.median(pos)),
    )


# K (hydraulic conductivity)        → viridis on log scale.
# Ss (specific storage)              → plasma  on log scale.
_PROPERTY_CMAPS = {"k": "viridis", "ss": "plasma"}


def _property_array(name: str) -> np.ndarray:
    if name == "k":
        return state.grid.k[0]
    if name == "ss":
        return state.grid.ss[0]
    raise HTTPException(400, f"unknown property: {name!r}")


def _bbox_4326() -> dict:
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
def last_scenario_info(job: str | None = None):
    """Info for a completed scenario run. `job` selects a specific run;
    omitted = the most recently completed one."""
    entry = _get_scenario(job)
    if entry is None or entry.get("proposed_bore") is None:
        return JSONResponse({"available": False})
    transformer = pyproj.Transformer.from_crs(state.cfg.project.crs, "EPSG:4326", always_xy=True)
    bore = dict(entry["proposed_bore"])
    bore_lng, bore_lat = transformer.transform(float(bore["x"]), float(bore["y"]))

    # Echo every well in the change-set so the map can plot multi-bore
    # and trade scenarios in full (not just the first bore).
    wells_out: list[dict] = []
    for w in (entry.get("wells_run") or []):
        wx, wy = float(w["x"]), float(w["y"])
        wlng, wlat = transformer.transform(wx, wy)
        wells_out.append({
            "label": str(w.get("label", "")),
            "x": wx, "y": wy, "lng": float(wlng), "lat": float(wlat),
            "rate_ML_per_year": float(w.get("rate_ML_per_year", 0.0)),
        })

    bbox = _bbox_4326()
    return JSONResponse({
        "available": True,
        "bore": {**bore, "lng": bore_lng, "lat": bore_lat},
        "wells": wells_out,
        "years": sorted(entry["c_drawdown_by_year"].keys()),
        # Axis-aligned corners of the warped (EPSG:4326) raster — must be
        # the warp corners, not the reprojected MGA trapezoid, or the
        # overlay drifts against the vector layers in the interior.
        "image_corners_4326": _property_warp_meta()["image_corners_4326"],
        "bbox_4326": bbox["bbox"],
        "threshold_m": state.cfg.assessment.regulatory_threshold_m,
    })


@app.get("/api/last-scenario/drawdown.png")
def last_scenario_drawdown_png(layer: str = "cumulative", year: float = 100.0,
                               job: str | None = None):
    entry = _get_scenario(job)
    if entry is None:
        raise HTTPException(404, "No scenario has been run yet")
    drawdown_by_year = entry["c_drawdown_by_year"]
    available_years = list(drawdown_by_year.keys())
    near = [y for y in available_years if abs(float(y) - float(year)) < 1e-6]
    if not near:
        raise HTTPException(400, f"Year {year} not available; choose from {available_years}")
    y_key = near[0]

    c_arr = drawdown_by_year[y_key]
    if layer == "additional":
        arr = c_arr
    elif layer == "cumulative":
        if state.baseline is None:
            raise HTTPException(503, "Baseline not ready")
        a_arr = state.baseline.drawdown_by_year.get(y_key)
        if a_arr is None:
            keys = list(state.baseline.drawdown_by_year.keys())
            nearest = min(keys, key=lambda k: abs(float(k) - float(y_key)))
            a_arr = state.baseline.drawdown_by_year[nearest]
        arr = a_arr + c_arr
    else:
        raise HTTPException(400, "layer must be 'cumulative' or 'additional'")

    png = _drawdown_to_png(arr, state.grid.idomain[0])
    headers = {"Cache-Control": "no-store"}
    return Response(content=png, media_type="image/png", headers=headers)


@app.get("/api/last-scenario/drawdown/sample")
def last_scenario_drawdown_sample(lng: float, lat: float,
                                  layer: str = "cumulative", year: float = 100.0,
                                  job: str | None = None):
    entry = _get_scenario(job)
    if entry is None:
        raise HTTPException(404, "No scenario has been run yet")
    drawdown_by_year = entry["c_drawdown_by_year"]
    available_years = list(drawdown_by_year.keys())
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

    c_val = float(drawdown_by_year[y_key][rc[0], rc[1]])
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


def _cells_to_geojson(mask: np.ndarray, props_fn=None) -> dict:
    """Cell squares as GeoJSON polygons (per-corner reprojection to 4326).

    `props_fn(row, col) -> dict` optionally adds per-feature properties
    (merged over the default row/col)."""
    g = state.grid
    rs, cs = np.where(mask)
    if rs.size == 0:
        return {"type": "FeatureCollection", "features": []}

    dx = float(g.delr[0])
    dy = float(g.delc[0])
    y_top = g.yorigin + float(g.delc.sum())
    x0s = g.xorigin + cs * dx
    x1s = x0s + dx
    y1s = y_top - rs * dy
    y0s = y1s - dy

    transformer = pyproj.Transformer.from_crs(state.cfg.project.crs, "EPSG:4326", always_xy=True)
    all_xs = np.concatenate([x0s, x1s, x1s, x0s])
    all_ys = np.concatenate([y0s, y0s, y1s, y1s])
    all_lons, all_lats = transformer.transform(all_xs, all_ys)

    n = rs.size
    ll_lon, ll_lat = all_lons[:n],       all_lats[:n]
    lr_lon, lr_lat = all_lons[n:2*n],    all_lats[n:2*n]
    ur_lon, ur_lat = all_lons[2*n:3*n],  all_lats[2*n:3*n]
    ul_lon, ul_lat = all_lons[3*n:],     all_lats[3*n:]

    features = []
    for i in range(n):
        ring = [
            [float(ll_lon[i]), float(ll_lat[i])],
            [float(lr_lon[i]), float(lr_lat[i])],
            [float(ur_lon[i]), float(ur_lat[i])],
            [float(ul_lon[i]), float(ul_lat[i])],
            [float(ll_lon[i]), float(ll_lat[i])],
        ]
        props = {"row": int(rs[i]), "col": int(cs[i])}
        if props_fn is not None:
            props.update(props_fn(int(rs[i]), int(cs[i])))
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return {"type": "FeatureCollection", "features": features}


# GAB aquifer subareas (53 polygons in Data/Aquifers/GABORA_units_subareas.shp).
# Currently only this label routes to a runnable assessment; everything else
# falls back to coming-soon.html with the LABEL as a query string.
AQUIFER_SHAPEFILE = Path("Data/Aquifers/GABORA_units_subareas.shp")
READY_AQUIFER_LABELS: set[str] = {"Surat Precipice"}


def _build_aquifers_geojson() -> dict | None:
    """Read the GABORA units/subareas shapefile and return it as a GeoJSON
    FeatureCollection in EPSG:4326. Polygons are simplified to ~100 m so the
    landing-page payload is small while still rendering well at QLD scale.

    Returns None if the file is missing — the landing page falls back to
    the static aquifer list it used to show.
    """
    if not AQUIFER_SHAPEFILE.exists():
        import sys
        print(f"[aquifers] shapefile not found at {AQUIFER_SHAPEFILE}", file=sys.stderr)
        return None
    try:
        import geopandas as gpd
        from shapely.geometry import mapping as shapely_mapping
        gdf = gpd.read_file(AQUIFER_SHAPEFILE)
    except Exception as e:
        import sys
        print(f"[aquifers] failed to read shapefile: {e}", file=sys.stderr)
        return None

    gdf = gdf.to_crs("EPSG:4326")
    # Simplify in degrees: ~0.001° ≈ 100 m. Preserves topology at this zoom.
    gdf["geometry"] = gdf.geometry.simplify(0.001, preserve_topology=True)

    from urllib.parse import quote as urlquote
    features = []
    for _, row in gdf.iterrows():
        label = str(row.get("LABEL", "")).strip()
        ready = label in READY_AQUIFER_LABELS
        # Precipice (ready) routes into the runnable module; everything
        # else goes to the coming-soon page with the label preserved.
        href = "precipice.html" if ready else f"coming-soon.html?aquifer={urlquote(label)}"
        features.append({
            "type": "Feature",
            "geometry": shapely_mapping(row.geometry),
            "properties": {
                "label": label,
                "unit": str(row.get("UNIT", "")).strip(),
                "subarea": str(row.get("SUBAREA", "")).strip(),
                "basin": str(row.get("BASIN", "")).strip(),
                "status_zone": str(row.get("STATUS", "")).strip(),
                "ready": ready,
                "href": href,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _mask_from_records(records, nrc=None) -> np.ndarray:
    """(nrow, ncol) bool mask from (l, r, c, ...) boundary/drain records."""
    g = state.grid
    mask = np.zeros((g.nrow, g.ncol), dtype=bool)
    for rec in (records or []):
        mask[rec[1], rec[2]] = True
    return mask


def _build_setup_geojson() -> dict:
    """Layers reflect the boundary state the bootstrap actually converged
    with (built after _bootstrap_ic): CHD and GHB are separate layers now,
    and drains get their own layer with a `flowing` property (true where
    the steady-state head sits above the drain elevation — i.e. the cell
    was linearised into a GHB for the transient runs)."""
    flowing_rc = {(rec[1], rec[2]) for rec in (state.ghb_cells or [])}
    return {
        "active":  _cells_to_geojson(state.grid.idomain[0] == 1),
        "outcrop": _cells_to_geojson(state.grid.outcrop_mask),
        "chd":     _cells_to_geojson(_mask_from_records(state.chd_cells)),
        "ghb":     _cells_to_geojson(_mask_from_records(state.boundary_ghb)),
        "drains":  _cells_to_geojson(
            _mask_from_records(state.drn_cells),
            props_fn=lambda r, c: {"flowing": (r, c) in flowing_rc},
        ),
        "noflow":  _cells_to_geojson(_noflow_boundary_mask()),
    }


def _bool_mask_to_png(mask: np.ndarray, hex_color: str, alpha: float = 0.7) -> bytes:
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
    """Far-field boundary cells (CHD and truncation-face GHB) for the
    setup-page layer."""
    g = state.grid
    mask = np.zeros((g.nrow, g.ncol), dtype=bool)
    for (_l, r, c, _h) in (state.chd_cells or []):
        mask[r, c] = True
    for (_l, r, c, _h, _cd) in (state.boundary_ghb or []):
        mask[r, c] = True
    return mask


def _noflow_boundary_mask() -> np.ndarray:
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
            # CHD-only count (legacy modes / convergence fallback).
            "n_chd_cells": len(state.chd_cells or []),
            "n_ghb_cells": len(state.boundary_ghb or []),
            "n_noflow_boundary_cells": int(_noflow_boundary_mask().sum()),
            "n_drain_cells": len(state.drn_cells or []),
            "n_drains_flowing": len(state.ghb_cells or []),
        },
        "recharge_multiplier": state.cfg.assessment.recharge_multiplier,
    })


@app.get("/api/aquifers")
def aquifers_geojson():
    """GABORA units/subareas as a GeoJSON FeatureCollection in EPSG:4326.

    Powers the clickable aquifer map on the landing page. Each feature
    carries `label`, `unit`, `subarea`, `basin`, `status_zone`, `ready`
    (bool) and `href` (where a click should navigate).
    """
    if state.aquifers_geojson is None:
        raise HTTPException(503, "Aquifers data not available")
    return JSONResponse(state.aquifers_geojson)


@app.get("/api/model-setup/{layer}.geojson")
def model_setup_geojson(layer: str):
    if state.setup_geojson is None:
        raise HTTPException(503, "Setup not ready")
    if layer not in state.setup_geojson:
        raise HTTPException(
            400, f"layer must be one of: {', '.join(sorted(state.setup_geojson))}")
    return JSONResponse(state.setup_geojson[layer])


@app.get("/api/model-setup/property/{name}/info")
def model_setup_property_info(name: str):
    """Min/median/max + colour-ramp metadata for the K and Ss overlays.

    The PNG endpoint uses the same percentile clamp, so the legend bounds
    returned here match the colour-scale extremes shown on the map.
    """
    if state.grid is None:
        raise HTTPException(503, "Grid not ready")
    name = name.lower()
    if name not in _PROPERTY_CMAPS:
        raise HTTPException(400, "name must be 'k' or 'ss'")
    arr = _property_array(name)
    p2, p98, median = _property_stats(arr, state.grid.idomain[0])
    units = "m/day" if name == "k" else "1/m"
    label = "Hydraulic conductivity (K)" if name == "k" else "Specific storage (Ss)"
    return JSONResponse({
        "name": name,
        "label": label,
        "units": units,
        "vmin": p2,
        "vmax": p98,
        "median": median,
        "cmap": _PROPERTY_CMAPS[name],
        # Image corners specific to the warped (EPSG:4326-axis-aligned)
        # property raster. Different from /api/model-setup/info's
        # image_corners_4326 (which traces the MGA grid's four corners
        # after individual reprojection — a slight trapezoid).
        "image_corners_4326": _property_warp_meta()["image_corners_4326"],
    })


@app.get("/api/model-setup/property/{name}.png")
def model_setup_property_png(name: str):
    """Cell-property field rendered to a transparent PNG (log scale).

    Returned on the same grid georeferencing as the drawdown PNGs, so the
    frontend can drop it in as a maplibre `image` source with
    `image_corners_4326` from /api/model-setup/info.
    """
    if state.grid is None:
        raise HTTPException(503, "Grid not ready")
    name = name.lower()
    if name not in _PROPERTY_CMAPS:
        raise HTTPException(400, "name must be 'k' or 'ss'")

    # The field is static for the life of the process — render once.
    cache: dict[str, bytes] = getattr(state, "_property_png_cache", None) or {}
    if name not in cache:
        arr = _property_array(name)
        p2, p98, _ = _property_stats(arr, state.grid.idomain[0])
        cache[name] = _property_to_png(
            arr, state.grid.idomain[0],
            cmap_name=_PROPERTY_CMAPS[name], vmin=p2, vmax=p98,
        )
        state._property_png_cache = cache
    return Response(content=cache[name], media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/model-setup/property/{name}/sample")
def model_setup_property_sample(name: str, lng: float, lat: float):
    """Cell value of the K / Ss field at the clicked lng/lat.

    Reprojects (lng, lat) into the project CRS, finds the grid cell, and
    returns the raw cell value, the aquifer thickness at that cell, and
    the row/col index. `in_domain` is false when the click lands outside
    the modelled extent.
    """
    if state.grid is None:
        raise HTTPException(503, "Grid not ready")
    name = name.lower()
    if name not in _PROPERTY_CMAPS:
        raise HTTPException(400, "name must be 'k' or 'ss'")

    transformer = pyproj.Transformer.from_crs("EPSG:4326", state.cfg.project.crs, always_xy=True)
    x, y_proj = transformer.transform(lng, lat)
    rc = cell_of(state.grid, x, y_proj)
    if rc is None or state.grid.idomain[0, rc[0], rc[1]] != 1:
        return JSONResponse({
            "in_domain": False, "name": name,
            "x": float(x), "y": float(y_proj),
        })

    r, c = rc
    arr = _property_array(name)
    value = float(arr[r, c])
    thickness = float(state.grid.top[r, c] - state.grid.botm[0, r, c])
    units = "m/day" if name == "k" else "1/m"
    label = "Hydraulic conductivity (K)" if name == "k" else "Specific storage (Ss)"
    return JSONResponse({
        "in_domain": True,
        "name": name, "label": label, "units": units,
        "value": value, "thickness_m": thickness,
        "row": int(r), "col": int(c),
        "x": float(x), "y": float(y_proj),
    })


@app.middleware("http")
async def _no_cache_statics(request, call_next):
    """Frontend assets revalidate on every load (ETag/304 keeps it cheap).

    Replaces the manual `?v=N` cache-busting that repeatedly served stale
    JS/CSS after deploys. API responses manage their own cache headers.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

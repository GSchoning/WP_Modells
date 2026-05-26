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
from ..theis import _local_T_S, formation_avg_T_S, theis_at_springs
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
    ScenarioRequest,
    ScenarioResponse,
    TheisDiagnostics,
    WellSpec,
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
    last_c_series_df: pd.DataFrame | None = None     # per-complex time series
    last_wells_run: list[dict] | None = None          # full change set echoed back
    setup_geojson: dict | None = None                 # layer -> FeatureCollection
    decisions_path: Path | None = None                # JSON store for the audit trail
    aquifers_geojson: dict | None = None              # GeoJSON FC of GABORA units/subareas


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
        complex_series_df=result.complex_series_df.copy(),
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

    quadrants = state.cfg.assessment.chd_quadrants
    chd_filtered = active_boundary_chd_cells(
        grid, exclude_mask=grid.outcrop_mask, quadrants=quadrants,
    )
    chd_unfiltered = active_boundary_chd_cells(grid)
    attempts = [
        (
            f"quadrants={quadrants or 'all'} + outcrop excluded",
            chd_filtered,
        ),
        ("all-edges fallback", chd_unfiltered),
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
    # Use the safer (all-edges) CHD with the uniform IC.
    state.chd_cells = chd_unfiltered


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
    state.decisions_path = Path("outputs") / "decisions.json"

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


@app.get("/api/version")
def version_info():
    """Quick way to verify which build the API process is actually running.

    `property_renderer` is "pil" if the new pixel-perfect PIL pipeline
    is in use; if the process is still on the old matplotlib renderer
    this endpoint won't exist at all (404 → restart needed).
    """
    return JSONResponse({
        "property_renderer": "pil",
        "has_property_sample": True,
        "build": "2026-05-26-pil-property",
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


@app.post("/api/scenarios", response_model=ScenarioResponse)
def scenarios(req: ScenarioRequest) -> ScenarioResponse:
    if state.baseline is None:
        raise HTTPException(503, "Baseline not ready")

    wells_dicts = _build_wells_from_request(req)
    proposed_wells_xy_rate = [(w["x"], w["y"], w["rate_ML_per_year"]) for w in wells_dicts]

    # Recharge multiplier change re-runs the IC and re-baselines Scenario A
    # against a different cache slot. Both the steady-state IC and the
    # cached A baseline are tied to the multiplier, so the cache key
    # automatically picks the right slot or computes fresh if missing.
    if req.recharge_multiplier != state.cfg.assessment.recharge_multiplier:
        state.cfg.assessment.recharge_multiplier = req.recharge_multiplier
        _bootstrap_ic()
        state.baseline = _bootstrap_baseline()

    t0 = time.time()
    label_safe = "".join(c if c.isalnum() else "_" for c in (wells_dicts[0]["label"] or "scen_C"))[:40]
    workspace = state.workspace_root / f"scen_C_{label_safe}"
    try:
        c_result = run_scenario(
            state.cfg, state.grid, state.inputs, "C",
            state.ic_head, workspace, chd_cells=state.chd_cells,
            proposed_wells=proposed_wells_xy_rate,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    runtime = time.time() - t0

    # Retain Scenario C grids for the drawdown-maps page. wells_run carries
    # the resolved change set (for trade this is +at-new and -at-old).
    state.last_wells_run = wells_dicts
    state.last_c_drawdown_by_year = c_result.drawdown_at_output_years
    state.last_c_series_df = c_result.complex_series_df.copy()
    # Back-compat slot for the drawdown-maps page: first positive-rate well.
    positive = [w for w in wells_dicts if w["rate_ML_per_year"] > 0]
    if positive:
        first = positive[0]
        state.last_proposed_bore = {
            "bore_id": first["label"],
            "x": first["x"], "y": first["y"],
            "rate_ML_per_year": first["rate_ML_per_year"],
        }

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
    )


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
def spring_series(complex_id: str | None = None):
    """Per-complex drawdown time series across every model timestep."""
    if state.baseline is None or state.baseline.complex_series_df is None:
        raise HTTPException(503, "Baseline not ready")
    base = state.baseline.complex_series_df

    if complex_id is None:
        live = state.last_c_series_df
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

    live = state.last_c_series_df
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


def _drawdown_to_png(arr: np.ndarray, idomain: np.ndarray, vmax: float | None = None) -> bytes:
    """Render a (nrow, ncol) drawdown grid to a transparent PNG."""
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


def _property_to_png(
    arr: np.ndarray,
    idomain: np.ndarray,
    cmap_name: str,
    vmin: float,
    vmax: float,
) -> bytes:
    """Render a (nrow, ncol) cell-property array to a transparent PNG on a
    log10 colour scale, with one image pixel per grid cell — no resampling,
    no anti-aliasing, exact half-pixel registration.

    matplotlib's `savefig` was producing blurry edges and a slight
    sub-pixel offset because `imshow` places the image extent at
    `(-0.5, ncol-0.5)` and savefig anti-aliases on resize. Going via
    PIL writes pixel (i, j) at the integer (i, j) corner of the image
    so the four corners returned by `_bbox_4326()` line up exactly
    with the four corners of the modelled domain.
    """
    pos = (idomain == 1) & (arr > 0)
    nrow, ncol = arr.shape

    # log10 normalise into [0, 1] over the displayed range.
    log_min = np.log10(max(vmin, 1e-30))
    log_max = np.log10(max(vmax, vmin * 10))
    log_arr = np.full(arr.shape, np.nan, dtype=np.float64)
    log_arr[pos] = np.log10(arr[pos])
    norm = (log_arr - log_min) / (log_max - log_min)
    norm = np.clip(norm, 0.0, 1.0)

    cmap = plt.get_cmap(cmap_name)
    # cmap returns RGBA float in [0,1]; convert to uint8.
    rgba = (cmap(norm) * 255.0).astype(np.uint8)        # (nrow, ncol, 4)
    # Inactive / non-positive cells → fully transparent.
    rgba[..., 3] = np.where(pos, rgba[..., 3], 0)

    from PIL import Image
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


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
def last_scenario_info():
    if state.last_c_drawdown_by_year is None or state.last_proposed_bore is None:
        return JSONResponse({"available": False})
    transformer = pyproj.Transformer.from_crs(state.cfg.project.crs, "EPSG:4326", always_xy=True)
    bore = dict(state.last_proposed_bore)
    bore_lng, bore_lat = transformer.transform(float(bore["x"]), float(bore["y"]))

    # Echo every well in the change-set so the map can plot multi-bore
    # and trade scenarios in full (not just the first bore).
    wells_out: list[dict] = []
    for w in (state.last_wells_run or []):
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
        "years": sorted(state.last_c_drawdown_by_year.keys()),
        "image_corners_4326": bbox["tl_tr_br_bl"],
        "bbox_4326": bbox["bbox"],
        "threshold_m": state.cfg.assessment.regulatory_threshold_m,
    })


@app.get("/api/last-scenario/drawdown.png")
def last_scenario_drawdown_png(layer: str = "cumulative", year: float = 100.0):
    if state.last_c_drawdown_by_year is None:
        raise HTTPException(404, "No scenario has been run yet")
    available_years = list(state.last_c_drawdown_by_year.keys())
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
                                  layer: str = "cumulative", year: float = 100.0):
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


def _cells_to_geojson(mask: np.ndarray) -> dict:
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
        features.append({
            "type": "Feature",
            "properties": {"row": int(rs[i]), "col": int(cs[i])},
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


def _build_setup_geojson() -> dict:
    return {
        "active":  _cells_to_geojson(state.grid.idomain[0] == 1),
        "outcrop": _cells_to_geojson(state.grid.outcrop_mask),
        "chd":     _cells_to_geojson(_chd_mask()),
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
    g = state.grid
    mask = np.zeros((g.nrow, g.ncol), dtype=bool)
    for (_l, r, c, _h) in (state.chd_cells or []):
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
            "n_chd_cells": int(_chd_mask().sum()),
            "n_noflow_boundary_cells": int(_noflow_boundary_mask().sum()),
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
        raise HTTPException(400, "layer must be one of: active, outcrop, chd, noflow")
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
    arr = _property_array(name)
    p2, p98, _ = _property_stats(arr, state.grid.idomain[0])
    png = _property_to_png(
        arr, state.grid.idomain[0],
        cmap_name=_PROPERTY_CMAPS[name], vmin=p2, vmax=p98,
    )
    return Response(content=png, media_type="image/png",
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


_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

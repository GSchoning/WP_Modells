"""Run scenarios and sample drawdown at receptors (CLAUDE.md §6.4)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import flopy
import geopandas as gpd
import numpy as np
import pandas as pd

from .config import Config
from .grid import Grid, cell_of
from .io_layer import Inputs, ML_PER_YEAR_TO_M3_PER_DAY
from .model_builder import (
    YEAR_DAYS,
    WellRecord,
    boundary_chd_cells,
    build_steady_state,
    build_transient,
)


@dataclass
class ScenarioResult:
    name: str
    times_days: np.ndarray            # length nstp
    heads: np.ndarray                 # (nstp, nrow, ncol)
    drawdown: np.ndarray              # (nstp, nrow, ncol) = h_initial - h(t)
    drawdown_at_output_years: dict[float, np.ndarray]  # year -> (nrow, ncol)
    receptors_df: pd.DataFrame        # tidy (receptor_id, time_years, drawdown_m)
    complex_series_df: pd.DataFrame   # (complex_id, time_days, drawdown_m) — every timestep


def _bores_to_wells(
    bores: gpd.GeoDataFrame,
    grid: Grid,
    rate_col: str = "rate_m3_per_day",
    drop_off_domain: bool = True,
) -> tuple[list[WellRecord], gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Map a bores GeoDataFrame to MF6 WEL records.

    Returns (well_records, accepted_bores, rejected_bores). Bores in inactive
    cells are rejected when drop_off_domain=True.
    """
    wells: list[WellRecord] = []
    accept_mask: list[bool] = []
    for x, y, q in zip(bores.geometry.x, bores.geometry.y, bores[rate_col]):
        rc = cell_of(grid, float(x), float(y))
        if rc is None:
            accept_mask.append(False)
            continue
        r, c = rc
        if grid.idomain[0, r, c] != 1:
            accept_mask.append(not drop_off_domain)
            if not drop_off_domain:
                wells.append((0, r, c, -float(q)))
            continue
        wells.append((0, r, c, -float(q)))
        accept_mask.append(True)
    accepted = bores[accept_mask].copy()
    rejected = bores[[not m for m in accept_mask]].copy()
    return wells, accepted, rejected


def _read_heads(workspace: Path, name: str) -> tuple[np.ndarray, np.ndarray]:
    hf = flopy.utils.HeadFile(str(workspace / f"{name}.hds"))
    times = np.asarray(hf.get_times())
    heads = np.stack([hf.get_data(totim=float(t)) for t in times])  # (nt, nlay, nrow, ncol)
    return times, heads[:, 0, :, :]                                  # collapse single layer


def _times_to_output_years(times_days: np.ndarray, output_years: list[float]) -> dict[float, int]:
    """Map each requested output year to the index of the closest sim time."""
    targets = np.array(output_years) * YEAR_DAYS
    return {float(y): int(np.argmin(np.abs(times_days - t))) for y, t in zip(output_years, targets)}


def _pick_id_column(gdf: gpd.GeoDataFrame, candidates: tuple[str, ...]) -> str:
    for c in candidates:
        if c in gdf.columns:
            return c
    return gdf.columns[0]


def _sample_receptors(
    drawdown: np.ndarray,                       # (nrow, ncol)
    receptor_points: gpd.GeoDataFrame,
    id_col: str,
    grid: Grid,
    time_years: float,
) -> pd.DataFrame:
    rows = []
    for _, p in receptor_points.iterrows():
        rc = cell_of(grid, float(p.geometry.x), float(p.geometry.y))
        if rc is None or grid.idomain[0, rc[0], rc[1]] != 1:
            continue
        rows.append(
            {
                "receptor_id": p[id_col],
                "time_years": time_years,
                "drawdown_m": float(drawdown[rc[0], rc[1]]),
            }
        )
    return pd.DataFrame(rows)


def run_steady_state(
    cfg: Config, grid: Grid, workspace: Path, *, chd_cells=None
) -> np.ndarray:
    """Run the no-pumping steady-state pre-run; return initial heads (nlay, nrow, ncol)."""
    sim = build_steady_state(
        grid,
        workspace,
        name="ss",
        chd_cells=chd_cells,
        complexity=cfg.solver.complexity,
        recharge_multiplier=cfg.assessment.recharge_multiplier,
    )
    sim.write_simulation(silent=True)
    success, _ = sim.run_simulation(silent=False)
    if not success:
        raise RuntimeError("steady-state pre-run failed; check listing file in workspace")
    _, heads = _read_heads(Path(workspace), "ss")
    return heads[-1]                              # (nrow, ncol)


def run_scenario(
    cfg: Config,
    grid: Grid,
    inputs: Inputs,
    scenario: str,
    initial_head: np.ndarray,
    workspace: Path,
    *,
    chd_cells=None,
    proposed_wells: list[tuple[float, float, float]] | None = None,
) -> ScenarioResult:
    """Run Scenario A (existing) or C (change set vs baseline) and sample receptors.

    For Scenario C, `proposed_wells` is a list of (x, y, rate_ML_per_year)
    tuples. Positive rate = new extraction; negative rate = removed extraction
    (used for trade scenarios where an existing licence is transferred —
    +rate at the new location and -rate at the old). The change set is fed
    to MF6 as WEL records with the sign flipped (MF6 takes extraction as
    negative q). If `proposed_wells` is None, falls back to the single
    cfg.inputs.proposed_bore for backward compat.
    """
    if scenario == "A":
        wells, _accepted, _rejected = _bores_to_wells(inputs.pumping_bores, grid)
    elif scenario == "C":
        if proposed_wells is None:
            pb = cfg.inputs.proposed_bore
            if pb.x is None or pb.y is None or pb.rate_ML_per_year is None:
                raise ValueError(
                    "Scenario C requires either `proposed_wells` or "
                    "cfg.inputs.proposed_bore.{x,y,rate_ML_per_year}"
                )
            proposed_wells = [(float(pb.x), float(pb.y), float(pb.rate_ML_per_year))]
        if not proposed_wells:
            raise ValueError("Scenario C: proposed_wells list is empty")
        wells = []
        for x, y, rate_ml in proposed_wells:
            rc = cell_of(grid, float(x), float(y))
            if rc is None or grid.idomain[0, rc[0], rc[1]] != 1:
                raise ValueError(f"Proposed well at ({x:.0f}, {y:.0f}) falls outside the active domain.")
            rate_m3d = float(rate_ml) * ML_PER_YEAR_TO_M3_PER_DAY
            wells.append((0, rc[0], rc[1], -rate_m3d))
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    # Stress periods: optional yearly-step fine period + geometric remainder.
    fine_years = int(cfg.time.fine_period_years or 0)
    if 0 < fine_years < cfg.time.total_years:
        perioddata = [
            (fine_years * YEAR_DAYS, fine_years, 1.0),
            ((cfg.time.total_years - fine_years) * YEAR_DAYS,
             cfg.time.nstp, cfg.time.tsmult),
        ]
    else:
        perioddata = [(cfg.time.total_years * YEAR_DAYS,
                       cfg.time.nstp, cfg.time.tsmult)]

    name = f"scen_{scenario}"
    # Twin-run drawdown: run the same model with and without wells, and
    # compute s = h_no_pump(t) − h_with_pump(t). Anything that's identical
    # between the two runs (IC, recharge, CHD, boundary effects, IC drift)
    # cancels by construction, so drawdown is purely the well response.
    # More robust than relying on s = h_initial − h(t), which only works
    # cleanly if the IC is exactly steady-state for the same forcing.
    sim_pump = build_transient(
        grid,
        workspace / "pump",
        name=name,
        wells=wells,
        initial_head=initial_head,
        perioddata=perioddata,
        chd_cells=chd_cells,
        recharge=True,
        complexity=cfg.solver.complexity,
        recharge_multiplier=cfg.assessment.recharge_multiplier,
    )
    sim_pump.write_simulation(silent=True)
    success, _ = sim_pump.run_simulation(silent=False)
    if not success:
        raise RuntimeError(f"scenario {scenario} pump run failed; check listing file in workspace")

    sim_nopump = build_transient(
        grid,
        workspace / "nopump",
        name=name,
        wells=[],
        initial_head=initial_head,
        perioddata=perioddata,
        chd_cells=chd_cells,
        recharge=True,
        complexity=cfg.solver.complexity,
        recharge_multiplier=cfg.assessment.recharge_multiplier,
    )
    sim_nopump.write_simulation(silent=True)
    success, _ = sim_nopump.run_simulation(silent=False)
    if not success:
        raise RuntimeError(f"scenario {scenario} no-pump twin failed; check listing file in workspace")

    times_days, heads = _read_heads(workspace / "pump", name)
    _, heads_nopump = _read_heads(workspace / "nopump", name)
    drawdown = heads_nopump - heads                           # (nt, nrow, ncol)
    year_idx = _times_to_output_years(times_days, cfg.time.output_years)
    drawdown_by_year = {y: drawdown[i] for y, i in year_idx.items()}

    # Sample drawdown at every member spring, then aggregate to complex
    # taking the max — the regulatory unit of analysis is the complex,
    # and the conservative choice for trigger-threshold reporting is the
    # worst-affected spring within the complex.
    receptor_frames: list[pd.DataFrame] = []
    if inputs.springs is not None and len(inputs.springs):
        spring_id_col = cfg.assessment.spring_id_col
        complex_col = cfg.assessment.spring_complex_col
        if spring_id_col not in inputs.springs.columns:
            spring_id_col = _pick_id_column(
                inputs.springs,
                ("spring_id", "SpringID", "Spring_ID", "ID", "OBJECTID", "FID"),
            )
        for y, idx in year_idx.items():
            receptor_frames.append(
                _sample_receptors(drawdown[idx], inputs.springs, spring_id_col, grid, y)
            )
    if receptor_frames:
        per_spring = pd.concat(receptor_frames, ignore_index=True)
        if complex_col in inputs.springs.columns:
            spring_to_complex = dict(
                zip(inputs.springs[spring_id_col], inputs.springs[complex_col])
            )
            per_spring["complex"] = per_spring["receptor_id"].map(spring_to_complex)
            receptors_df = (
                per_spring.dropna(subset=["complex"])
                .groupby(["complex", "time_years"], as_index=False)
                .agg(drawdown_m=("drawdown_m", "max"),
                     n_springs=("drawdown_m", "size"))
                .rename(columns={"complex": "receptor_id"})
            )
        else:
            receptors_df = per_spring
            receptors_df["n_springs"] = 1
    else:
        receptors_df = pd.DataFrame(columns=["receptor_id", "time_years", "drawdown_m", "n_springs"])

    # Per-complex drawdown time series across every model timestep, taking
    # max over the member springs at each step. Powers the click-to-plot
    # line chart so the regulator can see when (not just whether) a
    # complex crosses the threshold.
    complex_series_df = _build_complex_series(
        drawdown, times_days, inputs.springs, grid,
        complex_col if inputs.springs is not None else None,
    )

    return ScenarioResult(
        name=name,
        times_days=times_days,
        heads=heads,
        drawdown=drawdown,
        drawdown_at_output_years=drawdown_by_year,
        receptors_df=receptors_df,
        complex_series_df=complex_series_df,
    )


def _build_complex_series(
    drawdown_grid: np.ndarray,                   # (nt, nrow, ncol)
    times_days: np.ndarray,                      # (nt,)
    springs: gpd.GeoDataFrame | None,
    grid: Grid,
    complex_col: str | None,
) -> pd.DataFrame:
    """Long-form time series per spring complex: (complex_id, time_days, drawdown_m).

    Max-over-member-springs at every timestep. Returns an empty frame if
    there's no springs layer or no complex column.
    """
    cols = ["complex_id", "time_days", "drawdown_m"]
    if springs is None or len(springs) == 0 or not complex_col or complex_col not in springs.columns:
        return pd.DataFrame(columns=cols)

    # Group member springs by complex; resolve each to its model cell.
    complex_cells: dict[str, list[tuple[int, int]]] = {}
    for cname, group in springs.groupby(complex_col):
        cname_str = str(cname).strip()
        if not cname_str or cname_str.lower() in ("nan", "none"):
            continue
        cells: list[tuple[int, int]] = []
        for x, y in zip(group.geometry.x, group.geometry.y):
            rc = cell_of(grid, float(x), float(y))
            if rc is None or grid.idomain[0, rc[0], rc[1]] != 1:
                continue
            cells.append(rc)
        if cells:
            complex_cells[cname_str] = cells

    if not complex_cells:
        return pd.DataFrame(columns=cols)

    rows = []
    for cname, cells in complex_cells.items():
        rs = np.array([r for r, _c in cells])
        cs = np.array([_c for _r, _c in cells])
        # (nt, n_members) → max over member axis → (nt,)
        max_dd = drawdown_grid[:, rs, cs].max(axis=1)
        for t_days, dd in zip(times_days, max_dd):
            rows.append({
                "complex_id": cname,
                "time_days": float(t_days),
                "drawdown_m": float(dd),
            })
    return pd.DataFrame(rows)

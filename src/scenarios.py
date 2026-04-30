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
from .io_layer import Inputs
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
) -> ScenarioResult:
    """Run Scenario A (existing) or C (proposed only) and sample receptors."""
    if scenario == "A":
        wells, _accepted, _rejected = _bores_to_wells(inputs.pumping_bores, grid)
    elif scenario == "C":
        pb = cfg.inputs.proposed_bore
        if pb.x is None or pb.y is None or pb.rate_m3_per_day is None:
            raise ValueError("Scenario C requires inputs.proposed_bore.{x,y,rate_m3_per_day}")
        rc = cell_of(grid, float(pb.x), float(pb.y))
        if rc is None or grid.idomain[0, rc[0], rc[1]] != 1:
            raise ValueError(f"Proposed bore {pb.bore_id} falls outside the active domain.")
        wells = [(0, rc[0], rc[1], -float(pb.rate_m3_per_day))]
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    perlen_days = cfg.time.total_years * YEAR_DAYS
    perioddata = [(perlen_days, cfg.time.nstp, cfg.time.tsmult)]

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

    # Sample springs only for now. Receptor bores share cells with the
    # pumping bores, so their reported drawdown is dominated by mesh-
    # artefact in-cell self-pumping; needs a Theis correction before the
    # number is meaningful at a 1500 m cell size. TODO: re-enable with
    # correction.
    receptor_frames: list[pd.DataFrame] = []
    if inputs.springs is not None and len(inputs.springs):
        spring_id_col = _pick_id_column(inputs.springs, ("spring_id", "SpringID", "Spring_ID", "ID", "OBJECTID", "FID"))
        for y, idx in year_idx.items():
            receptor_frames.append(
                _sample_receptors(drawdown[idx], inputs.springs, spring_id_col, grid, y)
            )
    receptors_df = pd.concat(receptor_frames, ignore_index=True) if receptor_frames else pd.DataFrame(
        columns=["receptor_id", "time_years", "drawdown_m"]
    )

    return ScenarioResult(
        name=name,
        times_days=times_days,
        heads=heads,
        drawdown=drawdown,
        drawdown_at_output_years=drawdown_by_year,
        receptors_df=receptors_df,
    )

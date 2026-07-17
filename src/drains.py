"""Rejected-recharge drains for the outcrop, linearised for superposition.

The parent regional model simulates unconfined outcrop behaviour within a
confined (linear) framework using two devices:

  1. water-table-scale storage in outcrop cells (the negative-SS
     convention in the properties CSV), and
  2. DRN cells whose elevation is the *minimum* of a finer DEM within
     each model cell — when the simulated head reaches the lowest
     topographic point, further recharge is rejected (discharged as
     seeps/springs/baseflow) instead of mounding.

This module reproduces that with a linearisation that keeps the POC's
superposition architecture exactly valid:

  - The steady-state pre-run uses real DRN cells (piecewise-linear is
    fine there; it just shapes the initial condition).
  - For the transient runs, every drain that is *flowing* at steady
    state (h_ss > drain elevation) becomes a GHB with head fixed at the
    drain elevation and the same conductance. A GHB is a linear
    boundary, so A + C = B still holds to solver precision.
  - The linearisation is wrong only where pumping would draw the head
    at a drain cell *below* its elevation (a real drain would shut off;
    the GHB keeps supplying water). `count_reversals` reports how many
    cells are in that state so every run carries a QA number instead of
    a silent assumption.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .grid import Grid

# Values at or below this are treated as DEM nodata (the supplied DEM uses
# a large negative sentinel offshore).
DEM_NODATA_CEILING = -1000.0

# Each entry: (layer, row, col, elevation_m, conductance_m2_per_day)
DrnRecord = tuple[int, int, int, float, float]
# Each entry: (layer, row, col, head_m, conductance_m2_per_day)
GhbRecord = tuple[int, int, int, float, float]


def min_dem_per_cell(
    grid: Grid, dem_path: str | Path, mask: np.ndarray | None = None
) -> np.ndarray:
    """(nrow, ncol) array of the minimum DEM elevation within each model
    cell; NaN where the DEM has no valid coverage. `mask` restricts the
    computation to True cells (e.g. the outcrop) — others stay NaN."""
    import rasterio

    out = np.full((grid.nrow, grid.ncol), np.nan)
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(float)
        if src.nodata is not None:
            dem[dem == src.nodata] = np.nan
        dem[dem <= DEM_NODATA_CEILING] = np.nan
        inv = ~src.transform

        dx = float(grid.delr[0])
        dy = float(grid.delc[0])
        y_top = grid.yorigin + float(grid.delc.sum())
        if mask is None:
            cells_iter = ((r, c) for r in range(grid.nrow) for c in range(grid.ncol))
        else:
            cells_iter = zip(*np.where(mask))
        for r, c in cells_iter:
            r, c = int(r), int(c)
            x0 = grid.xorigin + c * dx
            y1 = y_top - r * dy
            c0f, r0f = inv * (x0, y1)
            c1f, r1f = inv * (x0 + dx, y1 - dy)
            r0, r1 = int(min(r0f, r1f)), int(max(r0f, r1f)) + 1
            c0, c1 = int(min(c0f, c1f)), int(max(c0f, c1f)) + 1
            r0, c0 = max(0, r0), max(0, c0)
            r1, c1 = min(dem.shape[0], r1), min(dem.shape[1], c1)
            if r1 <= r0 or c1 <= c0:
                continue
            window = dem[r0:r1, c0:c1]
            if np.isnan(window).all():
                continue
            out[r, c] = float(np.nanmin(window))
    return out


# Clamp on the estimated drain conductance. Unclamped K·A/b reaches
# ~7e7 m²/d on the Precipice grid — 7 orders of magnitude above the
# cell-to-cell conductances, which drove the steady-state solve to
# floating overflow. 5,000 m²/d is the only conductance OGIA publishes
# (UWIR 2025 §6.3.2.3, mine RIV cells) and is ample: at the ~126 m³/d
# recharge a cell receives, the head excess over the drain elevation is
# ~0.025 m.
DRAIN_COND_MAX = 5_000.0
DRAIN_COND_MIN = 1.0


def estimate_conductance(grid: Grid, r: int, c: int, scale: float = 1.0) -> float:
    """Interim drain conductance estimate: C = K · A / b (m²/day), clamped
    to [DRAIN_COND_MIN, DRAIN_COND_MAX].

    Uses horizontal K as a stand-in for the (unavailable) calibrated
    parent-model conductance. Replace with the parent model's DRN
    conductances when supplied.
    """
    thickness = max(float(grid.top[r, c] - grid.botm[0, r, c]), 1.0)
    area = float(grid.delr[c] * grid.delc[r])
    raw = scale * float(grid.k[0, r, c]) * area / thickness
    return float(np.clip(raw, DRAIN_COND_MIN, DRAIN_COND_MAX))


def build_drain_cells_from_riv(
    grid: Grid,
    riv_csv: str | Path,
    *,
    conductance: float | None = None,
    conductance_scale: float = 1.0,
) -> list[DrnRecord]:
    """DRN records from the parent model's RIV export (riv_cells.csv).

    The UWIR 2025 regional model implements the surficial (rejected-
    recharge) drains with RIV cells whose stage equals rbot, so each reach
    only ever removes water — i.e. a drain at the calibrated stage with the
    calibrated conductance. This supersedes deriving elevations from a DEM
    and conductances from K·A/b.

    Rows are mapped by IROW/ICOL — the same parent-model frame the Grid
    arrays are indexed by. Where several reaches land in one tool cell
    (e.g. the merged Hutton layers 19+20), the drain takes the minimum
    stage and the summed conductance. Cells inactive in the tool grid are
    dropped.
    """
    import pandas as pd

    df = pd.read_csv(riv_csv)
    required = {"IROW", "ICOL", "stage_m", "cond_m2_per_day"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{riv_csv}: missing columns {sorted(missing)}")

    df["r"] = df["IROW"].astype(int) - 1
    df["c"] = df["ICOL"].astype(int) - 1
    in_bounds = (df.r >= 0) & (df.r < grid.nrow) & (df.c >= 0) & (df.c < grid.ncol)
    df = df[in_bounds]
    active = grid.idomain[0][df.r.to_numpy(), df.c.to_numpy()] == 1
    df = df[active]

    merged = df.groupby(["r", "c"]).agg(
        elev=("stage_m", "min"), cond=("cond_m2_per_day", "sum")
    ).reset_index()
    cells: list[DrnRecord] = []
    for row in merged.itertuples(index=False):
        cond = (float(conductance) if conductance is not None
                else float(row.cond) * conductance_scale)
        cells.append((0, int(row.r), int(row.c), float(row.elev), cond))
    return cells


def drain_cells_for_config(cfg, grid: Grid) -> tuple[list[DrnRecord], str]:
    """Build drain cells per the config's source priority.

    1. `drains.riv_cells_csv` (parent-model calibrated stages/conductances)
       when set and present;
    2. otherwise `inputs.dem` minimum-elevation drains with estimated
       conductances.
    Returns (records, source description for logging). Raises if neither
    source is available.
    """
    riv = getattr(cfg.drains, "riv_cells_csv", None)
    if riv is not None and Path(riv).exists():
        cells = build_drain_cells_from_riv(
            grid, riv,
            conductance=cfg.drains.conductance_m2_per_day,
            conductance_scale=cfg.drains.conductance_scale,
        )
        return cells, "parent-model RIV stages/conductances"
    if cfg.inputs.dem is not None:
        cells = build_drain_cells(
            grid, cfg.inputs.dem,
            conductance=cfg.drains.conductance_m2_per_day,
            conductance_scale=cfg.drains.conductance_scale,
        )
        return cells, "min-DEM elevation, estimated conductance"
    raise ValueError("drains enabled but neither drains.riv_cells_csv nor inputs.dem is available")


def build_drain_cells(
    grid: Grid,
    dem_path: str | Path,
    *,
    conductance: float | None = None,
    conductance_scale: float = 1.0,
) -> list[DrnRecord]:
    """DRN records for every active outcrop cell with DEM coverage.

    `conductance`: fixed value in m²/day for every drain; None estimates
    per-cell from K (see estimate_conductance).
    """
    mask = grid.outcrop_mask & (grid.idomain[0] == 1)
    dem_min = min_dem_per_cell(grid, dem_path, mask)
    cells: list[DrnRecord] = []
    rs, cs = np.where(mask)
    for r, c in zip(rs, cs):
        elev = dem_min[r, c]
        if np.isnan(elev):
            continue
        cond = (
            float(conductance) if conductance is not None
            else estimate_conductance(grid, int(r), int(c), conductance_scale)
        )
        cells.append((0, int(r), int(c), float(elev), cond))
    return cells


def linearise_drains(
    drn_cells: list[DrnRecord], ss_head: np.ndarray, *, tol: float = 1e-3
) -> list[GhbRecord]:
    """GHB records for every drain flowing at steady state.

    A drain flows when h_ss > elevation. Non-flowing drains are dropped
    entirely (a dry drain exerts no influence, and representing it as a
    GHB would wrongly *inject* water).
    """
    ghb: list[GhbRecord] = []
    for (l, r, c, elev, cond) in drn_cells:
        if float(ss_head[r, c]) > elev + tol:
            ghb.append((l, r, c, elev, cond))
    return ghb


def count_reversals(ghb_cells: list[GhbRecord], head: np.ndarray, *, tol: float = 1e-3) -> int:
    """Number of linearised drains whose simulated head fell below the
    drain elevation — i.e. cells where a real drain would have shut off
    but the GHB kept supplying water. Non-zero means the linearisation
    is optimistic (drawdown under-predicted) near those cells."""
    n = 0
    for (_l, r, c, elev, _cond) in ghb_cells:
        if float(head[r, c]) < elev - tol:
            n += 1
    return n

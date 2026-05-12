"""Build MF6 simulations for Scenarios A and C (CLAUDE.md §6.3).

Decoupled from `Config` so the same builder can be exercised by both the
production pipeline and the synthetic Theis test.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import flopy
import numpy as np
from flopy.mf6 import (
    MFSimulation, ModflowGwf, ModflowTdis, ModflowIms,
    ModflowGwfdis, ModflowGwfic, ModflowGwfnpf, ModflowGwfsto,
    ModflowGwfchd, ModflowGwfrch, ModflowGwfwel, ModflowGwfoc,
)

from .grid import Grid


YEAR_DAYS = 365.25

# Each entry: (layer, row, col, rate_m3_per_day). rate is *negative* for extraction.
WellRecord = tuple[int, int, int, float]
# Each entry: (layer, row, col, head_m).
ChdRecord = tuple[int, int, int, float]


def _add_dis(gwf: ModflowGwf, grid: Grid) -> None:
    ModflowGwfdis(
        gwf,
        nlay=grid.nlay,
        nrow=grid.nrow,
        ncol=grid.ncol,
        delr=grid.delr.tolist(),
        delc=grid.delc.tolist(),
        top=grid.top,
        botm=grid.botm,
        idomain=grid.idomain,
        xorigin=grid.xorigin,
        yorigin=grid.yorigin,
    )


def _add_npf(gwf: ModflowGwf, grid: Grid) -> None:
    ModflowGwfnpf(gwf, icelltype=0, k=grid.k)


def _add_sto(gwf: ModflowGwf, grid: Grid, *, transient: bool, n_periods: int = 1) -> None:
    # Flag every period the same way; STO defaults to the previous flag
    # if a period isn't listed, but being explicit avoids surprises with
    # multi-period transient runs.
    steady = {i: not transient for i in range(n_periods)}
    trans = {i: transient for i in range(n_periods)}
    ModflowGwfsto(
        gwf,
        iconvert=0,
        ss=grid.ss,
        steady_state=steady,
        transient=trans,
    )


def _add_ic(gwf: ModflowGwf, initial_head: np.ndarray | float) -> None:
    ModflowGwfic(gwf, strt=initial_head)


def _add_rch(gwf: ModflowGwf, grid: Grid, multiplier: float = 1.0) -> None:
    if not np.any(grid.rch):
        return
    rch = grid.rch * float(multiplier) if multiplier != 1.0 else grid.rch
    ModflowGwfrch(gwf, recharge=rch)


def _add_chd(gwf: ModflowGwf, chd_cells: Sequence[ChdRecord]) -> None:
    if not chd_cells:
        return
    spd = {0: [[(int(l), int(r), int(c)), float(h)] for (l, r, c, h) in chd_cells]}
    ModflowGwfchd(gwf, stress_period_data=spd)


def _add_wel(gwf: ModflowGwf, wells: Sequence[WellRecord]) -> None:
    if not wells:
        return
    spd = {0: [[(int(l), int(r), int(c)), float(q)] for (l, r, c, q) in wells]}
    ModflowGwfwel(gwf, stress_period_data=spd)


def _add_oc(gwf: ModflowGwf, name: str, n_periods: int = 1) -> None:
    # saverecord as a dict so the SAVE rule fires in every stress period;
    # passing a bare list applies only to period 0.
    saverecord = {i: [("HEAD", "ALL"), ("BUDGET", "ALL")] for i in range(n_periods)}
    ModflowGwfoc(
        gwf,
        head_filerecord=f"{name}.hds",
        budget_filerecord=f"{name}.cbc",
        saverecord=saverecord,
    )


def _make_sim(workspace: Path, name: str, perioddata, complexity: str) -> tuple[MFSimulation, ModflowGwf]:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    sim = MFSimulation(sim_name=name, sim_ws=str(workspace), exe_name="mf6")
    # nper must match len(perioddata); FloPy's TDIS default is nper=1
    # and silently truncates PERLEN otherwise (mem_set_value size mismatch).
    ModflowTdis(sim, time_units="days", nper=len(perioddata), perioddata=perioddata)
    ims = ModflowIms(sim, complexity=complexity, print_option="SUMMARY")
    gwf = ModflowGwf(sim, modelname=name, save_flows=True)
    sim.register_ims_package(ims, [gwf.name])
    return sim, gwf


def build_steady_state(
    grid: Grid,
    workspace: Path,
    *,
    name: str = "ss",
    chd_cells: Sequence[ChdRecord] | None = None,
    initial_head: np.ndarray | float | None = None,
    complexity: str = "MODERATE",
    recharge_multiplier: float = 1.0,
) -> MFSimulation:
    """Pre-development steady-state run (no wells, recharge active)."""
    sim, gwf = _make_sim(workspace, name, perioddata=[(1.0, 1, 1.0)], complexity=complexity)
    _add_dis(gwf, grid)
    _add_ic(gwf, initial_head if initial_head is not None else grid.top)
    _add_npf(gwf, grid)
    _add_sto(gwf, grid, transient=False)
    _add_rch(gwf, grid, multiplier=recharge_multiplier)
    _add_chd(gwf, chd_cells or [])
    _add_oc(gwf, name)
    return sim


def build_transient(
    grid: Grid,
    workspace: Path,
    *,
    name: str,
    wells: Iterable[WellRecord],
    initial_head: np.ndarray | float,
    perioddata: list[tuple[float, int, float]],
    chd_cells: Sequence[ChdRecord] | None = None,
    recharge: bool = True,
    complexity: str = "MODERATE",
    recharge_multiplier: float = 1.0,
) -> MFSimulation:
    """One transient stress period, with wells, optionally recharge + CHD."""
    sim, gwf = _make_sim(workspace, name, perioddata=perioddata, complexity=complexity)
    _add_dis(gwf, grid)
    _add_ic(gwf, initial_head)
    _add_npf(gwf, grid)
    _add_sto(gwf, grid, transient=True, n_periods=len(perioddata))
    if recharge:
        _add_rch(gwf, grid, multiplier=recharge_multiplier)
    _add_chd(gwf, chd_cells or [])
    _add_wel(gwf, list(wells))
    _add_oc(gwf, name, n_periods=len(perioddata))
    return sim


def boundary_chd_cells(grid: Grid, head: float | np.ndarray) -> list[ChdRecord]:
    """Return CHD records along the outermost ring of active cells.

    `head` may be a scalar (uniform far-field head) or an (nrow, ncol) array.
    """
    cells: list[ChdRecord] = []
    nrow, ncol = grid.nrow, grid.ncol

    def _h(r: int, c: int) -> float:
        if np.isscalar(head):
            return float(head)
        return float(head[r, c])

    for c in range(ncol):
        cells.append((0, 0, c, _h(0, c)))
        cells.append((0, nrow - 1, c, _h(nrow - 1, c)))
    for r in range(1, nrow - 1):
        cells.append((0, r, 0, _h(r, 0)))
        cells.append((0, r, ncol - 1, _h(r, ncol - 1)))
    return cells


def _quadrant_filter(grid: Grid, on_boundary: np.ndarray, allowed: list[str]) -> np.ndarray:
    """Restrict on_boundary to cells whose centroid-relative direction is in `allowed`.

    Quadrants are 90° wedges centred on N/E/S/W; e.g. NW = upper-left
    relative to the mean (row, col) of all active cells. "N", "S", "E",
    "W" are 90° wedges as well, oriented to those cardinals. Used to
    keep CHD off, say, the outcrop face of the active domain.
    """
    if not allowed:
        return on_boundary
    active = grid.idomain[0] == 1
    rs_a, cs_a = np.where(active)
    rc, cc = float(rs_a.mean()), float(cs_a.mean())

    rs_b, cs_b = np.where(on_boundary)
    if rs_b.size == 0:
        return on_boundary
    dr = rs_b - rc
    dc = cs_b - cc
    # arctan2 with north = -dr (row 0 is at top), east = +dc.
    # range = (-π, π]. North = π/2, west = π, south = -π/2, east = 0.
    angles = np.arctan2(-dr, dc)

    pi = np.pi
    quadrant_ranges = {
        "E":  ((-pi/4, pi/4),),
        "NE": ((0, pi/2),),
        "N":  ((pi/4, 3*pi/4),),
        "NW": ((pi/2, pi), (-pi, -pi)),       # second range a no-op (handled by closure below)
        "W":  ((3*pi/4, pi), (-pi, -3*pi/4)),
        "SW": ((-pi, -pi/2),),
        "S":  ((-3*pi/4, -pi/4),),
        "SE": ((-pi/2, 0),),
    }
    keep = np.zeros_like(angles, dtype=bool)
    for name in allowed:
        for (lo, hi) in quadrant_ranges.get(name, ()):
            keep |= (angles >= lo) & (angles <= hi)

    out = np.zeros_like(on_boundary)
    out[rs_b[keep], cs_b[keep]] = True
    return out


def active_boundary_chd_cells(
    grid: Grid, head: float | np.ndarray | None = None,
    *, exclude_mask: np.ndarray | None = None,
    quadrants: list[str] | None = None,
) -> list[ChdRecord]:
    """CHD on the boundary of the active domain (active cells with ≥1 inactive neighbour).

    Provides a far-field head sink so recharge can equilibrate. Head defaults
    to grid.top per cell — i.e. the water table is bound to the top of the
    formation at the model boundary.

    `exclude_mask`: if given (shape (nrow, ncol), bool), cells where the
    mask is True are skipped. Used to keep CHD off the outcrop pinch-out
    edge where the boundary is a recharge inflow, not a regional discharge
    — putting CHD there would pin heads in the recharge zone and prevent
    recharge from raising heads at all.
    """
    active = grid.idomain[0] == 1
    padded = np.pad(active, 1, constant_values=False)
    has_inactive_neighbour = (
        ~padded[:-2, 1:-1] | ~padded[2:, 1:-1]
        | ~padded[1:-1, :-2] | ~padded[1:-1, 2:]
    )
    on_boundary = active & has_inactive_neighbour
    if exclude_mask is not None:
        on_boundary = on_boundary & ~exclude_mask
    if quadrants:
        on_boundary = _quadrant_filter(grid, on_boundary, quadrants)
    rs, cs = np.where(on_boundary)

    if head is None:
        head_arr = grid.top
    elif np.isscalar(head):
        head_arr = np.full_like(grid.top, float(head))
    else:
        head_arr = head

    return [(0, int(r), int(c), float(head_arr[r, c])) for r, c in zip(rs, cs)]

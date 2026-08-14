"""Run scenarios and sample drawdown at receptors (CLAUDE.md §6.4)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import flopy
import geopandas as gpd
import numpy as np
import pandas as pd

from .config import Config
from .grid import Grid, cell_of, resolve_receptor_cells
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
    # Drawdown sampled at receptor water bores (non-S&D), tidy
    # (receptor_id, time_years, drawdown_m). NOTE: receptor bores are also
    # pumping bores in scenarios A/L, so the value at a bore's own cell
    # includes its own (cell-averaged) drawdown — fine for the cumulative
    # picture; the decision-relevant number for a proposal is s_additional,
    # which contains no self-impact.
    bores_df: pd.DataFrame | None = None
    heads_nopump: np.ndarray | None = None   # twin-run heads, reusable across scenarios
    # QA metrics ----------------------------------------------------------
    # Worst volumetric-budget percent discrepancy across both MF6 runs.
    max_pct_discrepancy: float = 0.0
    # Max final-time drawdown at boundary cells. CHD cells absorb drawdown
    # (under-predicts impact near the boundary); no-flow edges reflect it
    # (over-predicts). Either being non-trivial means the boundary is
    # influencing the answer and the result needs a caveat.
    chd_max_drawdown_m: float = 0.0
    noflow_max_drawdown_m: float = 0.0
    # Linearised rejected-recharge drains whose head fell below the drain
    # elevation in the pumped run — where a real drain would have shut
    # off. Non-zero → drawdown near those cells is under-predicted.
    # (Legacy linearised_ghb mode only; always 0 in drn mode.)
    n_drain_reversals: int = 0
    # DRN mode: drains dried by this scenario's pumping (below elevation in
    # the pumped run but not the no-pump twin, final time) and the total
    # reduction in drain discharge — i.e. captured rejected-recharge /
    # spring/baseflow discharge, in m³/day. Genuine physics, not an error
    # metric: reported so the regulator can see how much of a proposal's
    # take comes out of surface discharge.
    n_drains_dried: int = 0
    drain_capture_m3d: float = 0.0


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


def build_perioddata(cfg: Config) -> list[tuple[float, int, float]]:
    """Stress periods whose ends land exactly on every output year.

    MF6 always ends a timestep at a stress-period boundary, so making each
    output year a period boundary guarantees the sampled heads are at
    exactly t = 10/50/100 yr — not at whichever geometric step happens to
    fall nearest (with tsmult=1.2 late steps are 9–15 yr wide, so nearest
    could be several years off the labelled time).

    Layout: [0, fine] in annual steps (if fine_period_years > 0), then one
    geometric block per inter-output-year interval, with cfg.time.nstp
    steps distributed across the blocks proportionally to their duration.
    """
    total = float(cfg.time.total_years)
    fine = float(int(cfg.time.fine_period_years or 0))
    fine = min(max(fine, 0.0), total)

    bounds = sorted({float(y) for y in cfg.time.output_years if 0 < y <= total} | {total})
    if fine > 0:
        bounds = sorted(set(bounds) | {fine})

    perioddata: list[tuple[float, int, float]] = []
    t0 = 0.0
    coarse_total = total - fine
    for t1 in bounds:
        length_yr = t1 - t0
        if length_yr <= 0:
            continue
        if t1 <= fine:
            # Annual steps through the fine period.
            perioddata.append((length_yr * YEAR_DAYS, max(1, int(round(length_yr))), 1.0))
        else:
            share = length_yr / coarse_total if coarse_total > 0 else 1.0
            nstp = max(4, int(round(cfg.time.nstp * share)))
            perioddata.append((length_yr * YEAR_DAYS, nstp, cfg.time.tsmult))
        t0 = t1
    return perioddata


def _max_percent_discrepancy(workspace: Path, name: str) -> float:
    """Worst |PERCENT DISCREPANCY| from the MF6 listing file's budgets.

    Returns 0.0 if the listing file is missing or contains no budget block
    (never blocks a result on a parsing problem — this is a QA metric).
    """
    import re

    lst = Path(workspace) / f"{name}.lst"
    if not lst.exists():
        return 0.0
    worst = 0.0
    pattern = re.compile(r"PERCENT DISCREPANCY\s*=\s*(-?\d+(?:\.\d+)?)")
    try:
        with open(lst, errors="ignore") as f:
            for line in f:
                m = pattern.search(line)
                if m:
                    worst = max(worst, abs(float(m.group(1))))
    except OSError:
        return 0.0
    return worst


def _boundary_drawdown_stats(
    drawdown_final: np.ndarray, grid: Grid, chd_cells
) -> tuple[float, float]:
    """(max drawdown at CHD cells, max at no-flow active-boundary cells)."""
    active = grid.idomain[0] == 1
    padded = np.pad(active, 1, constant_values=False)
    has_inactive_neighbour = (
        ~padded[:-2, 1:-1] | ~padded[2:, 1:-1]
        | ~padded[1:-1, :-2] | ~padded[1:-1, 2:]
    )
    boundary = active & has_inactive_neighbour

    chd_mask = np.zeros_like(boundary)
    for (_l, r, c, _h) in (chd_cells or []):
        chd_mask[r, c] = True
    noflow_mask = boundary & ~chd_mask

    chd_max = float(np.max(drawdown_final[chd_mask])) if chd_mask.any() else 0.0
    noflow_max = float(np.max(drawdown_final[noflow_mask])) if noflow_mask.any() else 0.0
    return chd_max, noflow_max


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
    *,
    snap_max_m: float = 0.0,
) -> pd.DataFrame:
    cells = resolve_receptor_cells(
        receptor_points.geometry.x, receptor_points.geometry.y, grid,
        snap_max_m=snap_max_m,
    )
    rows = []
    for (_, p), cell in zip(receptor_points.iterrows(), cells):
        if cell is None:
            continue
        r, c, _snap_m = cell
        rows.append(
            {
                "receptor_id": p[id_col],
                "time_years": time_years,
                "drawdown_m": float(drawdown[r, c]),
            }
        )
    return pd.DataFrame(rows)


def run_steady_state(
    cfg: Config, grid: Grid, workspace: Path, *,
    chd_cells=None, drn_cells=None, ghb_cells=None,
) -> np.ndarray:
    """Run the no-pumping steady-state pre-run; return initial heads (nlay, nrow, ncol)."""
    sim = build_steady_state(
        grid,
        workspace,
        name="ss",
        chd_cells=chd_cells,
        drn_cells=drn_cells,
        ghb_cells=ghb_cells,
        complexity=cfg.solver.complexity,
        recharge_multiplier=cfg.assessment.recharge_multiplier,
    )
    sim.write_simulation(silent=True)
    success, _ = sim.run_simulation(silent=False)
    if not success:
        raise RuntimeError("steady-state pre-run failed; check listing file in workspace")
    _, heads = _read_heads(Path(workspace), "ss")
    return heads[-1]                              # (nrow, ncol)


def resolve_initial_head(cfg: Config, grid: Grid, ss_head: np.ndarray) -> np.ndarray:
    """The transient IC per assessment.ic_source.

    "steady_state": the pre-run solution as-is. "parent_predev": the
    parent model's pre-development surface overlaid on it (the SS fills
    uncovered cells). The IC cancels in twin-run drawdown except through
    the head-dependent switches (drain drying, storage conversion) — the
    parent surface gives the outcrop drains their observed discharge
    headroom, which our lower-equilibrating SS starves.
    """
    if cfg.assessment.ic_source != "parent_predev":
        return ss_head
    if cfg.inputs.predev_heads_csv is None:
        raise ValueError(
            'assessment.ic_source: "parent_predev" requires inputs.predev_heads_csv')
    from .grid import read_head_surface
    surf = read_head_surface(cfg.inputs.predev_heads_csv, grid)
    covered = np.isfinite(surf)
    print(f"[ic] parent predev heads on {int(covered.sum())} cells "
          f"(steady state fills the remaining "
          f"{int((grid.idomain[0] == 1).sum() - covered.sum())} active cells)")
    return np.where(covered, surf, ss_head)


def run_scenario(
    cfg: Config,
    grid: Grid,
    inputs: Inputs,
    scenario: str,
    initial_head: np.ndarray,
    workspace: Path,
    *,
    chd_cells=None,
    ghb_cells=None,
    drain_ghb_cells=None,
    drn_cells=None,
    proposed_wells: list[tuple[float, float, float]] | None = None,
    nopump_twin: tuple[np.ndarray, np.ndarray] | None = None,
) -> ScenarioResult:
    """Run a scenario and sample receptors.

    Scenarios: "A" (all existing take), "L" (licensed subset), "C" (change
    set only — legacy linearised mode), "B" (existing take + change set —
    the per-request scenario in drn mode, where s_additional is derived
    downstream as B − A).

    `drn_cells`: real MF6 DRN cells for both twins (drains.transient_mode
    = "drn"). Mutually intended with `ghb_cells` carrying only far-field
    boundary GHBs; in the legacy mode the linearised drains ride in
    `ghb_cells` and `drn_cells` is None.

    For Scenario C, `proposed_wells` is a list of (x, y, rate_ML_per_year)
    tuples. Positive rate = new extraction; negative rate = removed extraction
    (used for trade scenarios where an existing licence is transferred —
    +rate at the new location and -rate at the old). The change set is fed
    to MF6 as WEL records with the sign flipped (MF6 takes extraction as
    negative q). If `proposed_wells` is None, falls back to the single
    cfg.inputs.proposed_bore for backward compat.

    `nopump_twin`: optional precomputed (times_days, heads) from a previous
    scenario's no-pump run. The twin is identical for every scenario (same
    IC, recharge, CHD, and time discretisation), so passing it here skips
    one of the two MF6 runs — halving the wall-clock of a request.
    """
    if scenario == "A":
        wells, _accepted, _rejected = _bores_to_wells(inputs.pumping_bores, grid)
    elif scenario == "L":
        # Licensed-take layer: Scenario A restricted to the entitlement
        # (auth-holding, non-S&D) subset. Structurally identical to A —
        # a subset of the same wells — so it composes linearly and
        # s_licensed <= s_approved everywhere.
        wells, _accepted, _rejected = _bores_to_wells(inputs.licensed_bores, grid)
    elif scenario in ("C", "B"):
        if proposed_wells is None:
            pb = cfg.inputs.proposed_bore
            if pb.x is None or pb.y is None or pb.rate_ML_per_year is None:
                raise ValueError(
                    f"Scenario {scenario} requires either `proposed_wells` or "
                    "cfg.inputs.proposed_bore.{x,y,rate_ML_per_year}"
                )
            proposed_wells = [(float(pb.x), float(pb.y), float(pb.rate_ML_per_year))]
        if not proposed_wells:
            raise ValueError(f"Scenario {scenario}: proposed_wells list is empty")
        wells = []
        for x, y, rate_ml in proposed_wells:
            rc = cell_of(grid, float(x), float(y))
            if rc is None or grid.idomain[0, rc[0], rc[1]] != 1:
                raise ValueError(
                    f"Proposed well at ({x:.0f}, {y:.0f}) falls outside the "
                    f"active domain of the {cfg.project.name} model — check "
                    f"that the intended aquifer module is selected.")
            rate_m3d = float(rate_ml) * ML_PER_YEAR_TO_M3_PER_DAY
            wells.append((0, rc[0], rc[1], -rate_m3d))
        if scenario == "B":
            # Combined scenario: the change set ON TOP of all existing take.
            # Run directly (not by superposition) so head-dependent drains
            # respond to the true total stress.
            existing, _acc, _rej = _bores_to_wells(inputs.pumping_bores, grid)
            wells = existing + wells
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    # Stress periods end exactly on every output year (see build_perioddata).
    perioddata = build_perioddata(cfg)
    storage_convertible = cfg.assessment.storage_mode == "convertible"

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
        ghb_cells=ghb_cells,
        drn_cells=drn_cells,
        recharge=True,
        complexity=cfg.solver.complexity,
        recharge_multiplier=cfg.assessment.recharge_multiplier,
        storage_convertible=storage_convertible,
    )
    sim_pump.write_simulation(silent=True)
    success, _ = sim_pump.run_simulation(silent=False)
    if not success:
        raise RuntimeError(f"scenario {scenario} pump run failed; check listing file in workspace")

    times_days, heads = _read_heads(workspace / "pump", name)
    pct_disc = _max_percent_discrepancy(workspace / "pump", name)

    if nopump_twin is not None and len(nopump_twin[0]) == len(times_days):
        heads_nopump = nopump_twin[1]
    else:
        sim_nopump = build_transient(
            grid,
            workspace / "nopump",
            name=name,
            wells=[],
            initial_head=initial_head,
            perioddata=perioddata,
            chd_cells=chd_cells,
            ghb_cells=ghb_cells,
            drn_cells=drn_cells,
            recharge=True,
            complexity=cfg.solver.complexity,
            recharge_multiplier=cfg.assessment.recharge_multiplier,
            storage_convertible=storage_convertible,
        )
        sim_nopump.write_simulation(silent=True)
        success, _ = sim_nopump.run_simulation(silent=False)
        if not success:
            raise RuntimeError(f"scenario {scenario} no-pump twin failed; check listing file in workspace")
        _, heads_nopump = _read_heads(workspace / "nopump", name)
        pct_disc = max(pct_disc, _max_percent_discrepancy(workspace / "nopump", name))

    drawdown = heads_nopump - heads                           # (nt, nrow, ncol)
    year_idx = _times_to_output_years(times_days, cfg.time.output_years)
    drawdown_by_year = {y: drawdown[i] for y, i in year_idx.items()}
    chd_max_dd, noflow_max_dd = _boundary_drawdown_stats(drawdown[-1], grid, chd_cells)

    # Drain-reversal QA runs only over the *linearised drain* GHBs — a
    # far-field boundary GHB supplying water under drawdown is correct
    # behaviour, not a linearisation error. Callers that mix boundary
    # GHBs into ghb_cells pass the drain subset separately. Not meaningful
    # in drn mode (real drains cannot reverse).
    n_reversals = 0
    if drn_cells is None:
        reversal_cells = drain_ghb_cells if drain_ghb_cells is not None else ghb_cells
        if reversal_cells:
            from .drains import count_reversals
            n_reversals = count_reversals(list(reversal_cells), heads[-1])

    # DRN mode QA: drains dried by this scenario's pumping, and the total
    # reduction in drain discharge (captured rejected recharge / spring and
    # baseflow discharge) at the final time.
    n_dried = 0
    capture_m3d = 0.0
    if drn_cells:
        h_p_final = heads[-1]
        h_np_final = heads_nopump[-1]
        for (_l, r, c, elev, cond) in drn_cells:
            d_np = cond * max(float(h_np_final[r, c]) - elev, 0.0)
            d_p = cond * max(float(h_p_final[r, c]) - elev, 0.0)
            capture_m3d += d_np - d_p
            if d_np > 0.0 and float(h_p_final[r, c]) < elev - 1e-3:
                n_dried += 1

    # Sample drawdown at every member spring, then aggregate to complex
    # taking the max — the regulatory unit of analysis is the complex,
    # and the conservative choice for trigger-threshold reporting is the
    # worst-affected spring within the complex.
    receptor_frames: list[pd.DataFrame] = []
    spring_snap_m = float(getattr(cfg.assessment, "spring_snap_max_m", 0.0))
    if inputs.springs is not None and len(inputs.springs):
        spring_id_col = cfg.assessment.spring_id_col
        complex_col = cfg.assessment.spring_complex_col
        if spring_id_col not in inputs.springs.columns:
            spring_id_col = _pick_id_column(
                inputs.springs,
                ("spring_id", "SpringID", "Spring_ID", "ID", "OBJECTID", "FID"),
            )
        cells = resolve_receptor_cells(
            inputs.springs.geometry.x, inputs.springs.geometry.y, grid,
            snap_max_m=spring_snap_m,
        )
        n_snapped = sum(1 for t in cells if t is not None and t[2] > 0)
        n_excluded = sum(1 for t in cells if t is None)
        if n_snapped or n_excluded:
            import sys
            print(f"[springs] {n_snapped} spring(s) outside the active domain "
                  f"snapped to the nearest active cell (≤{spring_snap_m:.0f} m); "
                  f"{n_excluded} beyond snap range excluded", file=sys.stderr)
        for y, idx in year_idx.items():
            receptor_frames.append(
                _sample_receptors(drawdown[idx], inputs.springs, spring_id_col, grid, y,
                                  snap_max_m=spring_snap_m)
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
        snap_max_m=spring_snap_m,
    )

    # Drawdown at receptor water bores (non-S&D), same sampling machinery
    # as springs. Bores in inactive cells are skipped by _sample_receptors.
    bore_frames: list[pd.DataFrame] = []
    if inputs.receptor_bores is not None and len(inputs.receptor_bores):
        bore_id_col = "bore_id" if "bore_id" in inputs.receptor_bores.columns \
            else _pick_id_column(inputs.receptor_bores, ("bore_id", "RN", "ID"))
        for y, idx in year_idx.items():
            bore_frames.append(
                _sample_receptors(drawdown[idx], inputs.receptor_bores, bore_id_col, grid, y)
            )
    bores_df = (pd.concat(bore_frames, ignore_index=True) if bore_frames
                else pd.DataFrame(columns=["receptor_id", "time_years", "drawdown_m"]))
    if len(bores_df):
        # Normalise bore ids to strings: merges can upcast int ids to float
        # ("107775" -> 107775.0), which breaks id-keyed lookups downstream.
        bores_df["receptor_id"] = bores_df["receptor_id"].astype(str)

    return ScenarioResult(
        name=name,
        times_days=times_days,
        heads=heads,
        drawdown=drawdown,
        drawdown_at_output_years=drawdown_by_year,
        receptors_df=receptors_df,
        complex_series_df=complex_series_df,
        bores_df=bores_df,
        heads_nopump=heads_nopump,
        max_pct_discrepancy=pct_disc,
        chd_max_drawdown_m=chd_max_dd,
        noflow_max_drawdown_m=noflow_max_dd,
        n_drain_reversals=n_reversals,
        n_drains_dried=n_dried,
        drain_capture_m3d=capture_m3d,
    )


def _build_complex_series(
    drawdown_grid: np.ndarray,                   # (nt, nrow, ncol)
    times_days: np.ndarray,                      # (nt,)
    springs: gpd.GeoDataFrame | None,
    grid: Grid,
    complex_col: str | None,
    snap_max_m: float = 0.0,
) -> pd.DataFrame:
    """Long-form time series per spring complex: (complex_id, time_days, drawdown_m).

    Max-over-member-springs at every timestep, resolving members with the
    same nearest-active-cell snapping as the output-year tables so the
    click-through chart agrees with them. Returns an empty frame if
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
        resolved = resolve_receptor_cells(
            group.geometry.x, group.geometry.y, grid, snap_max_m=snap_max_m,
        )
        cells = [(r, c) for t in resolved if t is not None for r, c in [(t[0], t[1])]]
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

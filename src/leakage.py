"""Quasi-3D vertical leakage through the confining bed (Hantush device).

The single-layer reconstruction has no vertical exchange, so sustained
pumping can only mine storage, capture drain discharge, or pull on the
few boundary GHBs — a closed container whose late-time drawdown grows
linearly without bound. The parent model's main stabiliser is leakage
from the layers above/below. This module reproduces it as a per-cell
head-dependent boundary:

    Q_leak = C · (h_source − h),   C = (Kv/b') · cell_area

with h_source taken from a steady-state head export of the source layer
(the overlying layer once extracted; the aquifer's own pre-development
surface as an interim). See config.LeakageCfg for the assumptions.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .grid import Grid, cell_of
from .model_builder import GhbRecord


def leakage_ghb_cells(cfg: Config, grid: Grid) -> tuple[list[GhbRecord], str]:
    """Build the leakage GHB records for every covered active cell.

    Returns (records, source_description). Empty list when disabled or
    not configured. Raises on a configured-but-missing file so a broken
    setup fails loudly rather than silently dropping the physics.
    """
    lk = cfg.leakage
    if not lk.enabled:
        return [], "disabled"
    if lk.source_heads_csv is None or (lk.kv_over_b_per_day is None
                                       and lk.conductance_csv is None):
        raise ValueError(
            "leakage.enabled requires leakage.source_heads_csv and one of "
            "leakage.conductance_csv / leakage.kv_over_b_per_day")
    path = Path(lk.source_heads_csv)
    if not path.exists():
        raise FileNotFoundError(f"leakage source heads not found: {path}")

    df = pd.read_csv(path)
    if lk.head_col not in df.columns:
        raise ValueError(
            f"{path.name}: no '{lk.head_col}' column (has {list(df.columns)})")

    # Map source points onto grid cells by X/Y centre; average duplicates
    # (a multi-layer source file may stack nodes on one plan-view cell).
    heads: dict[tuple[int, int], list[float]] = {}
    for x, y, h in zip(df["X"], df["Y"], df[lk.head_col]):
        rc = cell_of(grid, float(x), float(y))
        if rc is None or not np.isfinite(h):
            continue
        heads.setdefault(rc, []).append(float(h))

    dx = float(np.mean(grid.delr)) if grid.delr.size else 1500.0
    dy = float(np.mean(grid.delc)) if grid.delc.size else dx
    scale = float(lk.conductance_scale)

    # Per-cell conductance: derived-from-Kz CSV when given (cells absent
    # from the file get NO leakage boundary), else uniform Kv/b' · area.
    cond_by_cell: dict[tuple[int, int], float] | None = None
    if lk.conductance_csv is not None:
        cpath = Path(lk.conductance_csv)
        if not cpath.exists():
            raise FileNotFoundError(f"leakage conductance CSV not found: {cpath}")
        cdf = pd.read_csv(cpath)
        if "cond_m2_per_day" not in cdf.columns:
            raise ValueError(f"{cpath.name}: no 'cond_m2_per_day' column")
        cond_by_cell = {}
        for x, y, cv in zip(cdf["X"], cdf["Y"], cdf["cond_m2_per_day"]):
            rc = cell_of(grid, float(x), float(y))
            if rc is None or not np.isfinite(cv) or cv <= 0:
                continue
            cond_by_cell[rc] = cond_by_cell.get(rc, 0.0) + float(cv)
        src_desc = f"C from {cpath.name}"
    else:
        uniform = float(lk.kv_over_b_per_day) * dx * dy
        src_desc = f"C={uniform * scale:.3g} m²/d (Kv/b'={lk.kv_over_b_per_day:g}/d)"

    records: list[GhbRecord] = []
    for (r, c), hs in heads.items():
        if grid.idomain[0, r, c] != 1:
            continue
        if cond_by_cell is not None:
            cond = cond_by_cell.get((r, c))
            if cond is None:
                continue
        else:
            cond = uniform
        records.append((0, r, c, float(np.mean(hs)), cond * scale))

    n_active = int((grid.idomain[0] == 1).sum())
    total_c = sum(rec[4] for rec in records)
    desc = (f"{len(records)}/{n_active} active cells, {src_desc}, "
            f"ΣC={total_c:.3g} m²/d, heads from {path.name}")
    return records, desc

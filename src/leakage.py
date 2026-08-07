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
    if lk.kv_over_b_per_day is None or lk.source_heads_csv is None:
        raise ValueError(
            "leakage.enabled requires both leakage.kv_over_b_per_day and "
            "leakage.source_heads_csv")
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
    cond = float(lk.kv_over_b_per_day) * dx * dy * float(lk.conductance_scale)

    records: list[GhbRecord] = []
    for (r, c), hs in heads.items():
        if grid.idomain[0, r, c] != 1:
            continue
        records.append((0, r, c, float(np.mean(hs)), cond))

    n_active = int((grid.idomain[0] == 1).sum())
    desc = (f"{len(records)}/{n_active} active cells, C={cond:.3g} m²/d "
            f"(Kv/b'={lk.kv_over_b_per_day:g}/d), heads from {path.name}")
    return records, desc

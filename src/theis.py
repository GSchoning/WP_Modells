"""Theis analytical solution for comparison vs modelled drawdown.

The Theis (1935) solution for confined-aquifer drawdown from a single
well at constant rate Q is

    s(r, t) = Q / (4 π T) · W(u),    u = r² S / (4 T t)

where W is the well function (= exp1, the exponential integral).

This module provides:
  - theis_drawdown(Q, T, S, r, t): scalar/vector analytical drawdown
  - theis_at_springs(...): drawdown at every spring for a single
    pumping bore, using local T and S sampled at the bore's cell
"""
from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.special import exp1

from .grid import Grid, cell_of


YEAR_DAYS = 365.25


def theis_drawdown(Q: float, T: float, S: float, r: np.ndarray | float, t: float) -> np.ndarray | float:
    """Drawdown s(r, t) for a confined aquifer with transmissivity T and storativity S.

    Q: extraction rate (m³/day; positive number).
    T: transmissivity (m²/day) = K × thickness.
    S: storativity (dimensionless) = Ss × thickness.
    r: distance(s) from the well (m). Floored at a small positive number
       so r=0 doesn't blow up.
    t: time since pumping started (days).
    """
    r_arr = np.asarray(r, dtype=float)
    r_safe = np.maximum(r_arr, 1.0)              # 1 m floor — Theis is singular at r=0
    u = r_safe * r_safe * S / (4.0 * T * t)
    return Q / (4.0 * math.pi * T) * exp1(u)


def _local_T_S(grid: Grid, r: int, c: int) -> tuple[float, float]:
    """Local transmissivity and storativity at cell (r, c)."""
    thickness = float(grid.top[r, c] - grid.botm[0, r, c])
    T = float(grid.k[0, r, c]) * thickness
    S = float(grid.ss[0, r, c]) * thickness
    return T, S


def theis_at_springs(
    grid: Grid,
    springs: gpd.GeoDataFrame,
    spring_id_col: str,
    well_x: float,
    well_y: float,
    well_rate_m3_per_day: float,
    output_years: list[float],
    complex_col: str | None = None,
) -> pd.DataFrame:
    """Theis drawdown at each spring for a single pumping bore.

    If `complex_col` is given, aggregates by complex taking the **max**
    drawdown over member springs (= min distance) and the corresponding
    minimum r — matches the model-side aggregation in scenarios.py.

    Returns a tidy frame keyed by (receptor_id, time_years).
    """
    rc = cell_of(grid, well_x, well_y)
    if rc is None:
        raise ValueError("Theis comparison: well falls outside the grid.")
    T, S = _local_T_S(grid, rc[0], rc[1])
    if T <= 0 or S <= 0:
        raise ValueError(f"Theis comparison: non-physical local T={T}, S={S} at well cell.")

    sp_x = springs.geometry.x.to_numpy()
    sp_y = springs.geometry.y.to_numpy()
    r = np.hypot(sp_x - well_x, sp_y - well_y)

    rows = []
    Q = abs(float(well_rate_m3_per_day))
    spring_ids = springs[spring_id_col].to_numpy()
    complex_names = (
        springs[complex_col].to_numpy() if complex_col and complex_col in springs.columns else None
    )
    for y in output_years:
        t_days = y * YEAR_DAYS
        s = theis_drawdown(Q, T, S, r, t_days)
        for i, (sid, ri, di) in enumerate(zip(spring_ids, r, s)):
            rows.append({
                "receptor_id": str(complex_names[i]) if complex_names is not None else sid,
                "spring_id": sid,
                "time_years": float(y),
                "drawdown_m_theis": float(di),
                "T_m2_per_day": T,
                "S_dimensionless": S,
                "r_m": float(ri),
            })
    df = pd.DataFrame(rows)
    if complex_names is not None and not df.empty:
        df = (
            df.groupby(["receptor_id", "time_years"], as_index=False)
            .agg(
                drawdown_m_theis=("drawdown_m_theis", "max"),
                r_m=("r_m", "min"),
                T_m2_per_day=("T_m2_per_day", "first"),
                S_dimensionless=("S_dimensionless", "first"),
            )
        )
    return df

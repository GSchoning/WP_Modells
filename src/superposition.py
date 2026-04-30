"""Combine scenario outputs by superposition (CLAUDE.md §6.5).

Scenario B = A + C. We do not re-run MF6.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def combine_receptor_tables(scen_a: pd.DataFrame, scen_c: pd.DataFrame) -> pd.DataFrame:
    """Return a tidy table with s_approved, s_total, s_additional per receptor × time.

    Both inputs must have columns: receptor_id, time_years, drawdown_m.
    """
    key = ["receptor_id", "time_years"]
    a = scen_a.rename(columns={"drawdown_m": "s_approved"})
    c = scen_c.rename(columns={"drawdown_m": "s_additional"})
    out = a.merge(c, on=key, how="outer").fillna({"s_approved": 0.0, "s_additional": 0.0})
    out["s_total"] = out["s_approved"] + out["s_additional"]
    return out


def combine_rasters(s_a: np.ndarray, s_c: np.ndarray) -> dict[str, np.ndarray]:
    """Return s_approved / s_total / s_additional grids."""
    return {
        "s_approved": s_a,
        "s_additional": s_c,
        "s_total": s_a + s_c,
    }

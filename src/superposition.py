"""Combine scenario outputs by superposition (CLAUDE.md §6.5).

Scenario B = A + C. We do not re-run MF6.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def combine_receptor_tables(
    scen_a: pd.DataFrame,
    scen_c: pd.DataFrame,
    scen_l: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return a tidy table with s_approved, s_total, s_additional per receptor × time.

    All inputs must have columns: receptor_id, time_years, drawdown_m.
    n_springs (member springs per complex) is preserved if present. When
    `scen_l` (the licensed-take subset of A) is supplied, an `s_licensed`
    column is added — a subset of A, so s_licensed <= s_approved.
    """
    key = ["receptor_id", "time_years"]
    a = scen_a.rename(columns={"drawdown_m": "s_approved"})
    c = scen_c.rename(columns={"drawdown_m": "s_additional"})
    # n_springs is identical across A/C/L (same springs/complexes), so take
    # it from A and drop the copies to avoid suffix collisions.
    if "n_springs" in c.columns:
        c = c.drop(columns=["n_springs"])
    out = a.merge(c, on=key, how="outer").fillna({"s_approved": 0.0, "s_additional": 0.0})
    out["s_total"] = out["s_approved"] + out["s_additional"]
    if scen_l is not None:
        l = scen_l.rename(columns={"drawdown_m": "s_licensed"})[key + ["s_licensed"]]
        out = out.merge(l, on=key, how="left").fillna({"s_licensed": 0.0})
    return out


def subtract_receptor_tables(scen_b: pd.DataFrame, scen_a: pd.DataFrame) -> pd.DataFrame:
    """B − A on tidy (receptor_id, time_years, drawdown_m) tables.

    Used in drn transient mode, where the combined scenario B is modelled
    directly (head-dependent drains respond to the true total stress) and
    the additional layer is the marginal impact B − A. Receptors missing
    from A subtract zero.
    """
    key = ["receptor_id", "time_years"]
    a = scen_a.rename(columns={"drawdown_m": "_a"})[key + ["_a"]]
    out = scen_b.merge(a, on=key, how="left").fillna({"_a": 0.0})
    out["drawdown_m"] = out["drawdown_m"] - out["_a"]
    return out.drop(columns=["_a"])


def combine_rasters(
    s_a: np.ndarray, s_c: np.ndarray, s_l: np.ndarray | None = None
) -> dict[str, np.ndarray]:
    """Return s_approved / s_total / s_additional grids (+ s_licensed if given)."""
    out = {
        "s_approved": s_a,
        "s_additional": s_c,
        "s_total": s_a + s_c,
    }
    if s_l is not None:
        out["s_licensed"] = s_l
    return out

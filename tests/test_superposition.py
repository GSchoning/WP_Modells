"""Linearity / superposition test (CLAUDE.md §10, §11.3)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.superposition import combine_rasters, combine_receptor_tables


def test_combine_rasters_is_additive():
    a = np.array([[1.0, 2.0], [3.0, 4.0]])
    c = np.array([[0.5, 0.5], [0.5, 0.5]])
    out = combine_rasters(a, c)
    assert np.allclose(out["s_approved"], a)
    assert np.allclose(out["s_additional"], c)
    assert np.allclose(out["s_total"], a + c)


def test_combine_receptor_tables():
    a = pd.DataFrame(
        {"receptor_id": ["S1", "S2"], "time_years": [10, 10], "drawdown_m": [0.4, 0.2]}
    )
    c = pd.DataFrame(
        {"receptor_id": ["S1", "S2"], "time_years": [10, 10], "drawdown_m": [0.1, 0.05]}
    )
    out = combine_receptor_tables(a, c).set_index("receptor_id")
    assert np.isclose(out.loc["S1", "s_approved"], 0.4)
    assert np.isclose(out.loc["S1", "s_additional"], 0.1)
    assert np.isclose(out.loc["S1", "s_total"], 0.5)
    assert np.isclose(out.loc["S2", "s_total"], 0.25)

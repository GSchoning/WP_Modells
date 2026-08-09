"""Initial-condition source (assessment.ic_source / grid.read_head_surface).

The parent pre-development surface overlays the steady state where the
export covers the grid; uncovered cells keep the steady-state value.
Stacked multi-layer exports average per plan cell.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.grid import read_head_surface, synthetic_uniform_grid
from src.scenarios import resolve_initial_head


def _grid():
    return synthetic_uniform_grid(nrow=5, ncol=5, dx=100.0, dy=100.0)


def _xy(grid, r, c):
    return (grid.xorigin + (c + 0.5) * 100.0,
            grid.yorigin + (grid.nrow - r - 0.5) * 100.0)


def test_read_head_surface_maps_and_averages(tmp_path):
    grid = _grid()
    rows = []
    x, y = _xy(grid, 0, 0)
    rows.append({"X": x, "Y": y, "head_predev_m": 10.0})
    x, y = _xy(grid, 2, 3)                       # stacked duplicate (two layers)
    rows.append({"X": x, "Y": y, "head_predev_m": 20.0})
    rows.append({"X": x, "Y": y, "head_predev_m": 30.0})
    rows.append({"X": 1e9, "Y": 1e9, "head_predev_m": 99.0})   # off-grid: ignored
    p = tmp_path / "heads.csv"
    pd.DataFrame(rows).to_csv(p, index=False)

    surf = read_head_surface(p, grid)
    assert surf[0, 0] == 10.0
    assert surf[2, 3] == pytest.approx(25.0)     # averaged stack
    assert np.isnan(surf[4, 4])                  # uncovered
    assert np.isfinite(surf).sum() == 2

    with pytest.raises(ValueError):
        read_head_surface(p, grid, head_col="nope")


def _cfg(ic_source, csv=None):
    return SimpleNamespace(
        assessment=SimpleNamespace(ic_source=ic_source),
        inputs=SimpleNamespace(predev_heads_csv=csv),
    )


def test_resolve_initial_head(tmp_path):
    grid = _grid()
    ss = np.full((5, 5), 50.0)

    # steady_state: passthrough
    out = resolve_initial_head(_cfg("steady_state"), grid, ss)
    assert out is ss

    # parent_predev: overlay covered cells, SS fills the rest
    x, y = _xy(grid, 1, 1)
    p = tmp_path / "heads.csv"
    pd.DataFrame([{"X": x, "Y": y, "head_predev_m": 123.0}]).to_csv(p, index=False)
    out = resolve_initial_head(_cfg("parent_predev", p), grid, ss)
    assert out[1, 1] == 123.0
    assert out[0, 0] == 50.0

    # configured without a file: loud failure
    with pytest.raises(ValueError):
        resolve_initial_head(_cfg("parent_predev"), grid, ss)

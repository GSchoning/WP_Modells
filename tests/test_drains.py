"""Rejected-recharge drain construction and linearisation (src/drains.py)."""
from __future__ import annotations

import numpy as np
import pytest

from src.drains import (
    build_drain_cells,
    build_drain_cells_from_riv,
    count_reversals,
    estimate_conductance,
    linearise_drains,
    min_dem_per_cell,
)
from src.grid import synthetic_uniform_grid


@pytest.fixture
def grid_with_outcrop():
    g = synthetic_uniform_grid(nrow=4, ncol=4, dx=1000, dy=1000, K=2.0,
                               Ss=1e-5, thickness=100.0)
    g.outcrop_mask[:] = False
    g.outcrop_mask[0, 0] = True
    g.outcrop_mask[1, 2] = True
    return g


@pytest.fixture
def dem_path(tmp_path, grid_with_outcrop):
    """250 m DEM covering the 4 km grid, elevation = 60 + row-gradient,
    with one conspicuous low point inside cell (0, 0)."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    g = grid_with_outcrop
    res = 250.0
    height = width = int(4000 / res)
    dem = np.full((height, width), 60.0, dtype="float32")
    dem += np.arange(height, dtype="float32")[:, None]  # gradient, rows increase southward
    # Low point inside model cell (0, 0): DEM rows 0..3, cols 0..3.
    dem[1, 1] = 12.5
    transform = from_origin(g.xorigin, g.yorigin + float(g.delc.sum()), res, res)
    path = tmp_path / "dem.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", transform=transform, crs="EPSG:28355", nodata=-9999.0,
    ) as dst:
        dst.write(dem, 1)
    return path


def test_min_dem_per_cell_picks_lowest_point(grid_with_outcrop, dem_path):
    g = grid_with_outcrop
    mins = min_dem_per_cell(g, dem_path, g.outcrop_mask)
    # Cell (0, 0) contains the engineered 12.5 m low point.
    assert mins[0, 0] == pytest.approx(12.5)
    # Non-outcrop cells were not computed.
    assert np.isnan(mins[3, 3])
    # The other outcrop cell has a valid (gradient) minimum.
    assert np.isfinite(mins[1, 2])


def test_build_drain_cells_conductance(grid_with_outcrop, dem_path):
    from src.drains import DRAIN_COND_MAX

    g = grid_with_outcrop
    cells = build_drain_cells(g, dem_path)
    assert len(cells) == 2                       # one per outcrop cell
    (l, r, c, elev, cond) = cells[0]
    assert (r, c) == (0, 0) and elev == pytest.approx(12.5)
    # Raw estimate C = K*A/b = 2.0 * 1e6 / 100 = 2e4 — clamped to the cap
    # (unclamped values reached ~7e7 m²/d on the real grid and drove the
    # steady-state solve to floating overflow).
    assert cond == pytest.approx(estimate_conductance(g, 0, 0)) == pytest.approx(DRAIN_COND_MAX)

    fixed = build_drain_cells(g, dem_path, conductance=123.0)
    assert all(cd == 123.0 for (*_, cd) in fixed)


def test_build_drain_cells_from_riv(grid_with_outcrop, tmp_path):
    """Parent-model RIV export: IROW/ICOL mapping, same-cell merge (min
    stage, summed conductance), inactive and out-of-bounds rows dropped."""
    import pandas as pd

    g = grid_with_outcrop
    g.idomain[0, 3, 3] = 0
    riv = tmp_path / "riv_cells.csv"
    pd.DataFrame({
        "ILAY": [24, 24, 24, 24, 24],
        "INODE": [1, 2, 3, 4, 5],
        "IROW": [1, 2, 2, 4, 99],       # (0,0); (1,2) twice; inactive; out of bounds
        "ICOL": [1, 3, 3, 4, 1],
        "X": [0, 0, 0, 0, 0], "Y": [0, 0, 0, 0, 0],
        "stage_m": [250.0, 300.0, 295.0, 100.0, 100.0],
        "cond_m2_per_day": [5000.0, 2000.0, 3000.0, 5000.0, 5000.0],
        "rbot_m": [250.0, 300.0, 295.0, 100.0, 100.0],
    }).to_csv(riv, index=False)

    cells = build_drain_cells_from_riv(g, riv)
    assert {(r, c) for (_l, r, c, _e, _cd) in cells} == {(0, 0), (1, 2)}
    by_cell = {(r, c): (e, cd) for (_l, r, c, e, cd) in cells}
    assert by_cell[(0, 0)] == (250.0, 5000.0)
    # merged reaches: minimum stage, summed conductance
    assert by_cell[(1, 2)] == (295.0, 5000.0)

    fixed = build_drain_cells_from_riv(g, riv, conductance=123.0)
    assert all(cd == 123.0 for (*_, cd) in fixed)
    scaled = build_drain_cells_from_riv(g, riv, conductance_scale=0.5)
    assert {cd for (*_, cd) in scaled} == {2500.0}


def test_linearise_and_reversals(grid_with_outcrop, dem_path):
    g = grid_with_outcrop
    cells = build_drain_cells(g, dem_path)

    # Steady head above both drain elevations → both linearised.
    h_ss = np.full((4, 4), 90.0)
    ghb = linearise_drains(cells, h_ss)
    assert len(ghb) == 2
    # GHB head equals drain elevation.
    assert {round(h, 1) for (_l, _r, _c, h, _cd) in ghb} == {12.5, round(cells[1][3], 1)}

    # Steady head below one drain elevation → that drain is dry, dropped.
    h_dry = h_ss.copy()
    h_dry[1, 2] = cells[1][3] - 5.0
    assert len(linearise_drains(cells, h_dry)) == 1

    # Reversal counting: pumped head below the drain elevation at one cell.
    h_pumped = np.full((4, 4), 90.0)
    assert count_reversals(ghb, h_pumped) == 0
    h_pumped[0, 0] = 10.0                       # below the 12.5 m drain
    assert count_reversals(ghb, h_pumped) == 1

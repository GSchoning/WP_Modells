"""Rejected-recharge drain construction and linearisation (src/drains.py)."""
from __future__ import annotations

import numpy as np
import pytest

from src.drains import (
    build_drain_cells,
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

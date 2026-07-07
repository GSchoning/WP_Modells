"""Tiny synthetic end-to-end run (CLAUDE.md §11.1).

Exercises the real pipeline pieces — run_scenario for A and C, the
no-pump-twin reuse, output-year alignment, and superposition — on a
10 km × 10 km uniform grid with 2 existing bores and 1 proposed bore.
"""
from __future__ import annotations

import shutil

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from src.config import Config
from src.grid import synthetic_uniform_grid
from src.io_layer import Inputs
from src.model_builder import YEAR_DAYS, boundary_chd_cells
from src.scenarios import build_perioddata, run_scenario
from src.superposition import combine_receptor_tables

CRS = "EPSG:28355"
THICKNESS = 100.0


def _make_config() -> Config:
    return Config.model_validate({
        "project": {"name": "synthetic_e2e", "crs": CRS},
        "inputs": {
            "formation_extent": "unused.shp",
            "outcrop": "unused.shp",
            "properties_csv": "unused.csv",
            "water_use": {
                "path": "unused.csv", "source_crs": CRS,
                "lon_col": "x", "lat_col": "y", "id_col": "id",
                "rate_col": "rate", "rate_units": "m3/day",
            },
            "proposed_bore": {
                "bore_id": "PROP_1",
                "x": 6750.0, "y": 6750.0, "rate_ML_per_year": 400.0,
            },
        },
        "time": {
            "total_years": 10, "nstp": 8, "tsmult": 1.2,
            "fine_period_years": 2, "output_years": [2, 5, 10],
        },
        "solver": {"complexity": "MODERATE"},
        "assessment": {"spring_complex_col": "complex_na", "spring_id_col": "site_no"},
    })


def _make_inputs() -> Inputs:
    bores = gpd.GeoDataFrame(
        {
            "bore_id": ["EX_1", "EX_2"],
            "rate_m3_per_day": [600.0, 400.0],
        },
        geometry=[Point(3250, 3250), Point(7250, 3250)],
        crs=CRS,
    )
    springs = gpd.GeoDataFrame(
        {
            "site_no": ["SP1", "SP2", "SP3"],
            "complex_na": ["North", "North", "South"],
        },
        geometry=[Point(5250, 7250), Point(5750, 7250), Point(5250, 2250)],
        crs=CRS,
    )
    extent = gpd.GeoDataFrame(geometry=[box(0, 0, 10500, 10500)], crs=CRS)
    return Inputs(
        formation_extent=extent,
        outcrop=extent.copy(),
        properties=pd.DataFrame(),
        pumping_bores=bores,
        receptor_bores=bores.copy(),
        springs=springs,
    )


def test_perioddata_ends_on_output_years():
    cfg = _make_config()
    perioddata = build_perioddata(cfg)
    ends = np.cumsum([p[0] for p in perioddata]) / YEAR_DAYS
    for y in cfg.time.output_years:
        assert np.any(np.isclose(ends, y)), f"no stress period ends at year {y}: {ends}"


@pytest.mark.skipif(shutil.which("mf6") is None, reason="mf6 binary not on PATH")
def test_synthetic_pipeline_runs(tmp_path):
    cfg = _make_config()
    inputs = _make_inputs()
    grid = synthetic_uniform_grid(nrow=21, ncol=21, dx=500, dy=500,
                                  K=2.0, Ss=1e-5, thickness=THICKNESS, crs=CRS)
    chd = boundary_chd_cells(grid, head=THICKNESS)
    ic = np.full((grid.nrow, grid.ncol), THICKNESS)

    a = run_scenario(cfg, grid, inputs, "A", ic, tmp_path / "A", chd_cells=chd)
    assert a.heads_nopump is not None

    # Scenario C reusing A's no-pump twin (the production path) …
    c = run_scenario(cfg, grid, inputs, "C", ic, tmp_path / "C", chd_cells=chd,
                     nopump_twin=(a.times_days, a.heads_nopump))
    # … must agree with C computed from its own twin.
    c_own = run_scenario(cfg, grid, inputs, "C", ic, tmp_path / "C_own", chd_cells=chd)
    pd.testing.assert_frame_equal(
        c.receptors_df.sort_values(["receptor_id", "time_years"]).reset_index(drop=True),
        c_own.receptors_df.sort_values(["receptor_id", "time_years"]).reset_index(drop=True),
        atol=1e-9,
    )

    # Sampled times land exactly on the requested output years.
    for y in cfg.time.output_years:
        assert np.any(np.isclose(a.times_days / YEAR_DAYS, y, atol=1e-6)), \
            f"no timestep at exactly {y} yr"

    # Receptor tables cover every complex × output year with drawdown > 0.
    for result in (a, c):
        assert set(result.receptors_df["time_years"]) == {2.0, 5.0, 10.0}
        assert set(result.receptors_df["receptor_id"]) == {"North", "South"}
        assert (result.receptors_df["drawdown_m"] > 0).all()

    # Drawdown must grow with time at every receptor (constant pumping).
    for _, g in a.receptors_df.groupby("receptor_id"):
        dd = g.sort_values("time_years")["drawdown_m"].to_numpy()
        assert np.all(np.diff(dd) > 0)

    # Superposition combination produces the three reporting layers.
    combined = combine_receptor_tables(a.receptors_df, c.receptors_df)
    assert np.allclose(
        combined["s_total"], combined["s_approved"] + combined["s_additional"],
    )

    # QA metrics populated and sane on a healthy little model.
    assert a.max_pct_discrepancy < 1.0
    assert c.max_pct_discrepancy < 1.0
    assert c.chd_max_drawdown_m >= 0.0

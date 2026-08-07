"""Theis analytical sanity test (CLAUDE.md §10, §11.2).

Single well in a uniform-K, uniform-Ss confined aquifer, with constant-head
boundary far from the well. Modelled drawdown must agree with the Theis
analytical solution to ~5 % at distances > 2 cells.
"""
from __future__ import annotations

import math
import shutil

import flopy
import numpy as np
import pytest
from scipy.special import exp1

from src.grid import synthetic_uniform_grid
from src.model_builder import build_transient, boundary_chd_cells


def _theis(Q: float, T: float, S: float, r: float, t: float) -> float:
    u = r * r * S / (4 * T * t)
    return Q / (4 * math.pi * T) * exp1(u)


@pytest.mark.skipif(shutil.which("mf6") is None, reason="mf6 binary not on PATH")
def test_single_well_matches_theis(tmp_path):
    K = 1.0           # m/d
    Ss = 1e-5         # 1/m
    b = 100.0         # m thickness
    Q = 1000.0        # m^3/d extraction
    nrow = ncol = 81
    dx = dy = 500.0   # 40 km square — far from any radius of influence at t<=100d

    grid = synthetic_uniform_grid(nrow=nrow, ncol=ncol, dx=dx, dy=dy, K=K, Ss=Ss, thickness=b)

    well_r, well_c = nrow // 2, ncol // 2
    wells = [(0, well_r, well_c, -Q)]

    chd = boundary_chd_cells(grid, head=b)

    total_days = 100.0
    perioddata = [(total_days, 50, 1.0)]   # 50 equal time steps

    sim = build_transient(
        grid,
        tmp_path,
        name="theis",
        wells=wells,
        initial_head=b,
        perioddata=perioddata,
        chd_cells=chd,
        recharge=False,
        complexity="MODERATE",
    )
    sim.write_simulation(silent=True)
    success, _ = sim.run_simulation(silent=True)
    assert success, "MF6 run failed"

    hds = flopy.utils.HeadFile(str(tmp_path / "theis.hds"))
    h_final = hds.get_data(totim=total_days)[0]      # (nrow, ncol)
    s_modelled = b - h_final

    T = K * b
    S = Ss * b

    # Sample at r = 4 dx, 8 dx, 16 dx — all >> 2 cells from the well.
    rel_errors = {}
    for ndx in (4, 8, 16):
        r = ndx * dx
        s_theis = _theis(Q, T, S, r, total_days)
        s_model = float(s_modelled[well_r, well_c + ndx])
        rel_errors[r] = abs(s_model - s_theis) / s_theis

    for r, err in rel_errors.items():
        assert err < 0.05, f"r={r:.0f} m: relative error {err:.3f} exceeds 5%"


def test_theis_cumulative_equals_sum_of_single_wells():
    import geopandas as gpd
    from shapely.geometry import Point

    from src.grid import synthetic_uniform_grid
    from src.theis import theis_at_springs, theis_cumulative_at_springs

    grid = synthetic_uniform_grid(nrow=21, ncol=21, dx=500.0, dy=500.0)
    springs = gpd.GeoDataFrame(
        {"site_no": ["S1", "S2", "S3"], "complex_na": ["North", "North", "South"]},
        geometry=[Point(3000, 8000), Point(3500, 8200), Point(7000, 2000)],
        crs="EPSG:28355",
    )
    wells = [(5000.0, 5000.0, 800.0), (6500.0, 4000.0, 300.0)]
    T, S = 50.0, 5.0e-4
    years = [10.0, 100.0]

    cum = theis_cumulative_at_springs(
        grid, springs, "site_no", wells, years, T=T, S=S, complex_col="complex_na",
    ).set_index(["receptor_id", "time_years"])

    # Per-spring sum of the single-well solution, then max over members.
    per_spring = None
    for (x, y, q) in wells:
        df = theis_at_springs(grid, springs, "site_no", x, y, q, years, T=T, S=S)
        df = df.set_index(["receptor_id", "time_years"])["drawdown_m_theis"]
        per_spring = df if per_spring is None else per_spring + df
    expected = (per_spring.reset_index()
                .assign(complex=lambda d: d.receptor_id.map(
                    dict(zip(springs.site_no, springs.complex_na))))
                .groupby(["complex", "time_years"])["drawdown_m_theis"].max())

    for (cx, yr), want in expected.items():
        got = float(cum.loc[(cx, yr), "drawdown_m_theis"])
        assert abs(got - want) < 1e-9, (cx, yr, got, want)
    # Sanity: with T=50, S=5e-4, drawdown grows with time.
    assert cum.loc[("North", 100.0), "drawdown_m_theis"] > cum.loc[("North", 10.0), "drawdown_m_theis"]

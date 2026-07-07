"""Linearity / superposition tests (CLAUDE.md §10, §11.3).

The pandas-level tests check the combination arithmetic. The MF6 test is
the load-bearing one: it verifies that drawdown from wells A and wells C
run *separately* sums to the drawdown of a single run containing both —
i.e. that the confined-linear system really is linear at the numerical
tolerance of the solver. Everything the tool reports rests on this.
"""
from __future__ import annotations

import shutil

import numpy as np
import pandas as pd
import pytest

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


# ---------------------------------------------------------------------------
# MF6 superposition test: ‖(s_A + s_C) − s_B‖ must be within solver noise.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("mf6") is None, reason="mf6 binary not on PATH")
def test_mf6_scenario_superposition(tmp_path):
    import flopy

    from src.grid import synthetic_uniform_grid
    from src.model_builder import boundary_chd_cells, build_transient

    K = 2.0            # m/d
    Ss = 1e-5          # 1/m
    b = 100.0          # m thickness
    nrow = ncol = 41
    dx = dy = 500.0

    grid = synthetic_uniform_grid(nrow=nrow, ncol=ncol, dx=dx, dy=dy, K=K, Ss=Ss, thickness=b)
    chd = boundary_chd_cells(grid, head=b)
    perioddata = [(3650.0, 25, 1.2)]        # 10 years

    wells_a = [(0, 14, 14, -800.0), (0, 26, 22, -500.0)]   # "existing"
    wells_c = [(0, 20, 27, -1000.0)]                       # "proposed"

    def run(name, wells):
        ws = tmp_path / name
        sim = build_transient(
            grid, ws, name=name, wells=wells, initial_head=b,
            perioddata=perioddata, chd_cells=chd, recharge=False,
            complexity="MODERATE",
        )
        sim.write_simulation(silent=True)
        ok, _ = sim.run_simulation(silent=True)
        assert ok, f"MF6 run {name} failed"
        hds = flopy.utils.HeadFile(str(ws / f"{name}.hds"))
        h = hds.get_data(totim=3650.0)[0]
        return b - h                          # drawdown vs uniform IC

    s_a = run("scen_a", wells_a)
    s_c = run("scen_c", wells_c)
    s_b = run("scen_b", wells_a + wells_c)   # directly-modelled combined run

    err = np.abs((s_a + s_c) - s_b)
    # Solver tolerance noise only — the peak drawdown is O(1 m), so 1 mm
    # of disagreement would already indicate a non-linearity.
    assert float(err.max()) < 1e-3, (
        f"superposition violated: max|A+C-B| = {err.max():.6f} m "
        f"(peak drawdown {s_b.max():.3f} m)"
    )

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


def test_combine_receptor_tables_with_licensed_layer():
    a = pd.DataFrame(
        {"receptor_id": ["S1", "S2"], "time_years": [10, 10],
         "drawdown_m": [0.4, 0.2], "n_springs": [1, 1]}
    )
    c = pd.DataFrame(
        {"receptor_id": ["S1", "S2"], "time_years": [10, 10], "drawdown_m": [0.1, 0.05]}
    )
    # Licensed take is a subset of A, so s_licensed <= s_approved.
    lic = pd.DataFrame(
        {"receptor_id": ["S1", "S2"], "time_years": [10, 10], "drawdown_m": [0.3, 0.15]}
    )
    out = combine_receptor_tables(a, c, scen_l=lic)
    assert "s_licensed" in out.columns
    assert (out["s_licensed"] <= out["s_approved"] + 1e-12).all()
    out = out.set_index("receptor_id")
    assert np.isclose(out.loc["S1", "s_licensed"], 0.3)
    # s_total still A + C, unaffected by the licensed layer.
    assert np.isclose(out.loc["S1", "s_total"], 0.5)


def test_combine_receptor_tables_without_licensed_is_backcompat():
    a = pd.DataFrame({"receptor_id": ["S1"], "time_years": [10], "drawdown_m": [0.4]})
    c = pd.DataFrame({"receptor_id": ["S1"], "time_years": [10], "drawdown_m": [0.1]})
    out = combine_receptor_tables(a, c)
    assert "s_licensed" not in out.columns


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


# ---------------------------------------------------------------------------
# Superposition with the *tweaked* model machinery on (CLAUDE.md §10).
#
# The bare test above only exercises CHD + wells. The model has since grown
# three linear devices that the drawdown answer now leans on, and each one
# is a fresh chance to break A+C=B:
#   - negative-Sy outcrop storage decoded to a per-cell Ss,
#   - recharge over the outcrop,
#   - far-field boundary GHBs, and — the load-bearing one —
#   - rejected-recharge drains that are *real DRN* in the steady-state
#     pre-run but *linearised GHBs* in the transient runs.
#
# That DRN→GHB swap means the IC (h_initial) is NOT the exact steady state
# of the transient system, so h_initial−h(t) leaks the drift into drawdown.
# The pipeline dodges this with the twin-run form s = h_nopump(t)−h_pump(t)
# (scenarios.run_scenario), which cancels anything common to both runs. This
# test mirrors that path and asserts superposition survives to solver
# precision — with the IMS head-close pinned tight so the residual reflects
# linearity, not the MODERATE preset's noise floor.
# ---------------------------------------------------------------------------

def _tighten_ims(sim):
    ims = sim.get_package("ims")
    ims.inner_dvclose = 1e-9
    ims.outer_dvclose = 1e-9
    ims.inner_rclose = 1e-9
    return sim


@pytest.mark.skipif(shutil.which("mf6") is None, reason="mf6 binary not on PATH")
def test_mf6_superposition_with_tweaked_machinery(tmp_path):
    import flopy

    from src.drains import count_reversals, linearise_drains
    from src.grid import synthetic_uniform_grid
    from src.model_builder import (
        build_steady_state,
        build_transient,
        truncation_face_ghb_cells,
    )

    K, Ss_conf, b = 2.0, 1e-5, 100.0
    nrow = ncol = 41
    dx = dy = 500.0

    grid = synthetic_uniform_grid(nrow=nrow, ncol=ncol, dx=dx, dy=dy,
                                  K=K, Ss=Ss_conf, thickness=b)
    grid.top[:] = 100.0
    grid.botm[:] = 0.0

    # Outcrop band on the west columns: recharge + water-table storage.
    outcrop = np.zeros((nrow, ncol), dtype=bool)
    outcrop[:, :6] = True
    grid.outcrop_mask = outcrop
    grid.rch = np.where(outcrop, 3e-4, 0.0)                 # m/d
    grid.ss[0][outcrop] = 0.02 / b                          # Sy=0.02 decoded to Ss

    # Far-field boundary GHBs on the western truncation face.
    ghb_boundary = truncation_face_ghb_cells(grid, faces=("W",), head_source="ntop")
    # Rejected-recharge drains across the outcrop, below top so recharge
    # mounds above them and they flow at steady state.
    drn_cells = [(0, int(r), int(c), 92.0, 5000.0) for r, c in zip(*np.where(outcrop))]

    # Steady-state pre-run with REAL drains -> initial head.
    ss_sim = _tighten_ims(build_steady_state(
        grid, tmp_path / "ss", name="ss",
        drn_cells=drn_cells, ghb_cells=ghb_boundary, initial_head=100.0,
    ))
    ss_sim.write_simulation(silent=True)
    ok, _ = ss_sim.run_simulation(silent=True)
    assert ok, "steady-state pre-run failed"
    h_ss = flopy.utils.HeadFile(str(tmp_path / "ss" / "ss.hds")).get_data()[0]

    # Linearise the flowing drains to GHBs for the transient runs.
    drn_ghb = linearise_drains(drn_cells, h_ss)
    assert drn_ghb, "no drains flowed at steady state — test setup is degenerate"
    ghb_all = ghb_boundary + drn_ghb

    perioddata = [(3650.0, 25, 1.2)]                        # 10 years
    wells_a = [(0, 14, 14, -800.0), (0, 26, 22, -500.0)]   # "existing"
    wells_c = [(0, 20, 27, -1000.0)]                        # "proposed"

    def run(name, wells):
        sim = _tighten_ims(build_transient(
            grid, tmp_path / name, name=name, wells=wells,
            initial_head=h_ss, perioddata=perioddata,
            ghb_cells=ghb_all, recharge=True,
        ))
        sim.write_simulation(silent=True)
        ok, _ = sim.run_simulation(silent=True)
        assert ok, f"transient {name} failed"
        return flopy.utils.HeadFile(str(tmp_path / name / f"{name}.hds")).get_data(totim=3650.0)[0]

    h0 = run("nopump", [])                    # twin, no wells
    h_a = run("scen_a", wells_a)
    h_c = run("scen_c", wells_c)
    h_b = run("scen_b", wells_a + wells_c)

    # Twin-run drawdown, exactly as scenarios.run_scenario computes it.
    s_a, s_c, s_b = h0 - h_a, h0 - h_c, h0 - h_b

    err = np.abs((s_a + s_c) - s_b)
    # With dvclose=1e-9 the residual is at solver precision (~1e-9 m) and,
    # verified separately, independent of pumping magnitude — the signature
    # of a genuinely linear system. 1e-6 m leaves ~3 orders of headroom.
    assert float(err.max()) < 1e-6, (
        f"superposition violated with tweaked machinery: "
        f"max|A+C-B| = {err.max():.3e} m (peak drawdown {s_b.max():.3f} m)"
    )

    # The linearisation is only valid where drains stay flowing; if the
    # proposed well pulled a drain below its elevation the QA metric would
    # flag it. Here it must be clean, else the tolerance above is luck.
    assert count_reversals(drn_ghb, h_b) == 0

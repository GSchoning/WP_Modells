"""Transient drain treatment: real DRN vs legacy linearised GHB.

Regression for the Gubberamunda outcrop clamp: with the parent model's
drain blanket linearised to fixed-head GHBs, a bore pumped inside the
blanket showed almost no drawdown (the GHBs supply fictitious water once
the head drops below the drain elevation). Real DRN cells shut off
instead, so drawdown grows once the local discharge is captured.
"""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from src.drains import linearise_drains
from src.grid import synthetic_uniform_grid
from src.model_builder import boundary_chd_cells, build_steady_state, build_transient

pytestmark = pytest.mark.skipif(shutil.which("mf6") is None, reason="mf6 binary not on PATH")

B_THICK = 100.0
DRN_ELEV = 99.5
DRN_COND = 5000.0


def _setup(tmp_path):
    import flopy

    grid = synthetic_uniform_grid(nrow=31, ncol=31, dx=500.0, dy=500.0,
                                  K=2.0, Ss=1e-5, thickness=B_THICK)
    chd = boundary_chd_cells(grid, head=B_THICK)
    # A drain blanket over the central "outcrop" block, recharged so every
    # drain rejects recharge (flows) at steady state — the parent-model
    # configuration that produced the Gubberamunda clamp.
    blanket = np.zeros((grid.nrow, grid.ncol), dtype=bool)
    blanket[10:21, 10:21] = True
    grid.outcrop_mask = blanket
    grid.rch = np.where(blanket, 3e-4, 0.0)
    drn = [(0, r, c, DRN_ELEV, DRN_COND) for r, c in zip(*np.where(blanket))]

    ss = build_steady_state(grid, tmp_path / "ss", name="ss", chd_cells=chd,
                            drn_cells=drn, initial_head=B_THICK)
    ss.write_simulation(silent=True)
    ok, _ = ss.run_simulation(silent=True)
    assert ok
    h_ss = flopy.utils.HeadFile(str(tmp_path / "ss" / "ss.hds")).get_data()[0]
    return grid, chd, drn, h_ss


def _twin_drawdown(grid, tmp_path, tag, wells, *, chd, ghb=None, drn=None, ic=None):
    import flopy

    perioddata = [(3650.0, 20, 1.2)]
    heads = {}
    for name, w in (("np", []), ("p", wells)):
        sim = build_transient(
            grid, tmp_path / f"{tag}_{name}", name="m", wells=w,
            initial_head=ic, perioddata=perioddata,
            chd_cells=chd, ghb_cells=ghb, drn_cells=drn, recharge=True,
        )
        sim.write_simulation(silent=True)
        ok, _ = sim.run_simulation(silent=True)
        assert ok, f"{tag}_{name} failed"
        hf = flopy.utils.HeadFile(str(tmp_path / f"{tag}_{name}" / "m.hds"))
        heads[name] = hf.get_data(totim=3650.0)[0]
    return heads["np"] - heads["p"], heads["p"]


def test_drn_releases_the_outcrop_clamp(tmp_path):
    """A bore inside the drain blanket: linearised GHBs clamp its drawdown
    to ~Q/C; real DRN cells dry and let the cone develop."""
    grid, chd, drn, h_ss = _setup(tmp_path)
    br = bc = 15                                     # centre of the blanket
    wells = [(0, br, bc, -1500.0)]

    ghb = linearise_drains(drn, h_ss)
    assert len(ghb) == len(drn), "test setup: every drain should flow at SS"

    s_ghb, _ = _twin_drawdown(grid, tmp_path, "ghb", wells, chd=chd, ghb=ghb, ic=h_ss)
    s_drn, h_p = _twin_drawdown(grid, tmp_path, "drn", wells, chd=chd, drn=drn, ic=h_ss)

    # GHB clamp: drawdown at the bore bounded near Q/C (fictitious supply).
    assert s_ghb[br, bc] < 0.5, f"clamp not reproduced: {s_ghb[br, bc]:.3f} m"
    # Real DRN: local discharge captured, drains dry, cone develops.
    assert s_drn[br, bc] > 3.0 * s_ghb[br, bc]
    assert s_drn[br, bc] > 1.0
    # And the pumped DRN run really dried cells (head below drain elevation).
    dried = sum(1 for (_l, r, c, e, _cd) in drn if h_p[r, c] < e - 1e-3)
    assert dried > 0


def test_superposition_only_holds_away_from_dry_drains(tmp_path):
    """QA property in drn mode: A + C = B where no drain changes state, and
    B ≥ A + C (conservative direct run) where drains dry."""
    grid, chd, drn, h_ss = _setup(tmp_path)
    wells_a = [(0, 5, 5, -400.0)]                    # far from the blanket
    wells_c = [(0, 15, 15, -1500.0)]                 # inside the blanket

    s_a, _ = _twin_drawdown(grid, tmp_path, "a", wells_a, chd=chd, drn=drn, ic=h_ss)
    s_c, _ = _twin_drawdown(grid, tmp_path, "c", wells_c, chd=chd, drn=drn, ic=h_ss)
    s_b, _ = _twin_drawdown(grid, tmp_path, "b", wells_a + wells_c, chd=chd, drn=drn, ic=h_ss)

    err = s_b - (s_a + s_c)
    # Nonlinear drains: the direct B run never shows LESS drawdown than the
    # superposed parts (drains only lose capacity as more stress adds up).
    assert err.min() > -0.02
    # Far corner, no drain influence: superposition intact to ~mm.
    assert abs(err[2, 28]) < 5e-3
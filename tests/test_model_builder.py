"""Package-construction regression tests (no MF6 binary needed —
write_simulation() exercises FloPy's kwarg validation and file writers).

The RCH test exists because the recharge path was latent for months: with
an all-zero rch array `_add_rch` returns early, so the wrong package class
(list-based ModflowGwfrch instead of array-based ModflowGwfrcha) only blew
up on the first startup after real recharge data arrived.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("flopy")

from src.grid import synthetic_uniform_grid
from src.model_builder import (
    boundary_chd_cells,
    build_steady_state,
    build_transient,
    truncation_face_ghb_cells,
)


@pytest.fixture
def grid_with_recharge():
    g = synthetic_uniform_grid(nrow=5, ncol=5, dx=500, dy=500,
                               K=1.0, Ss=1e-5, thickness=100.0)
    g.outcrop_mask[0, :] = True
    g.rch[0, :] = 5.6e-5
    return g


def test_steady_state_with_recharge_writes(tmp_path, grid_with_recharge):
    g = grid_with_recharge
    sim = build_steady_state(
        g, tmp_path / "ss", name="ss",
        chd_cells=boundary_chd_cells(g, head=100.0),
    )
    sim.write_simulation(silent=True)
    assert (tmp_path / "ss" / "ss.rcha").exists()


def test_transient_with_all_packages_writes(tmp_path, grid_with_recharge):
    g = grid_with_recharge
    ghb = truncation_face_ghb_cells(g, ["W"], head_source="ntop")
    sim = build_transient(
        g, tmp_path / "tr", name="tr",
        wells=[(0, 2, 2, -100.0)],
        initial_head=100.0,
        perioddata=[(365.25, 5, 1.2)],
        chd_cells=boundary_chd_cells(g, head=100.0),
        ghb_cells=ghb,
    )
    sim.write_simulation(silent=True)
    ws = tmp_path / "tr"
    for ext in ("rcha", "ghb", "wel", "chd"):
        assert (ws / f"tr.{ext}").exists(), f"tr.{ext} not written"

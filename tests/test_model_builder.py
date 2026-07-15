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


def test_anchor_ghb_cells_covers_orphan_islands():
    """Active islands with no BC get exactly one weak anchor GHB; components
    that already carry a BC get none."""
    import numpy as np
    from src.model_builder import anchor_ghb_cells

    g = synthetic_uniform_grid(nrow=7, ncol=7, dx=500, dy=500,
                               K=1.0, Ss=1e-5, thickness=100.0)
    # Carve the domain into a main block and an isolated 2x2 island.
    g.idomain[0, :, :] = 0
    g.idomain[0, 0:3, 0:3] = 1          # main block (rows 0-2, cols 0-2)
    g.idomain[0, 5:7, 5:7] = 1          # island (rows 5-6, cols 5-6)
    g.top[5, 5] = 120.0                 # highest cell of the island

    # Main block has a BC cell; the island has none.
    anchors = anchor_ghb_cells(g, bc_cells={(0, 0)})
    assert len(anchors) == 1
    (_l, r, c, head, cond) = anchors[0]
    assert (r, c) == (5, 5)             # highest-NTOP cell of the orphan
    assert head == pytest.approx(120.0)
    assert cond == pytest.approx(1.0)

    # If the island also has a BC, no anchors are needed.
    assert anchor_ghb_cells(g, bc_cells={(0, 0), (6, 6)}) == []

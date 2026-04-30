"""Grid reconstruction from properties.csv (CLAUDE.md §6.2)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.grid import build_grid_from_properties, cell_of


def _toy_properties(dx: float = 1500.0) -> pd.DataFrame:
    rows = []
    for ic in range(1, 4):
        for ir in range(1, 4):
            rows.append(
                {
                    "ICOL": ic,
                    "IROW": ir,
                    "ILAY": 1,
                    "INODE": ic * ir,
                    "IBOUND": 1,
                    "NTOP": 100.0,
                    "NBOT": 0.0,
                    "X": 500_000 + (ic - 1) * dx,
                    "Y": 7_000_000 + (ir - 1) * dx,
                    "THICKNESS": 100.0,
                    "OUTCROP": "Y" if ir == 1 else "N",
                    "Depth": 50.0,
                    "kx": 1.0,
                    "SS": 1e-5,
                    "rch": 1e-4,
                }
            )
    return pd.DataFrame(rows)


def test_build_grid_shape():
    props = _toy_properties()
    g = build_grid_from_properties(props, "EPSG:28355")
    assert g.nrow == 3 and g.ncol == 3 and g.nlay == 1
    assert np.isclose(g.delr[0], 1500.0)
    assert np.isclose(g.delc[0], 1500.0)


def test_recharge_masked_to_outcrop():
    props = _toy_properties()
    g = build_grid_from_properties(props, "EPSG:28355")
    # Only IROW=1 cells were marked OUTCROP=Y.
    assert (g.rch[0, :] > 0).all()
    assert (g.rch[1:, :] == 0).all()


def test_cell_of_round_trips():
    props = _toy_properties()
    g = build_grid_from_properties(props, "EPSG:28355")
    rc = cell_of(g, 500_000, 7_000_000)
    assert rc is not None

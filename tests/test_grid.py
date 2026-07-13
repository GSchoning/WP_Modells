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
                    # Real CSV convention: the Precipice is ILAY=24 (the
                    # builder filters other layers out by default).
                    "ILAY": 24,
                    "INODE": ic * ir,
                    "IBOUND": 1,
                    "NTOP": 100.0,
                    "NBOT": 0.0,
                    "X": 500_000 + (ic - 1) * dx,
                    # IROW 1 is the northernmost row (max Y) — verified
                    # against the real CSV: corr(IROW, Y) = -1.
                    "Y": 7_000_000 - (ir - 1) * dx,
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


def test_negative_ss_decoded_as_dimensionless_storativity():
    """The parent-model export stores outcrop Sy as a NEGATIVE, dimensionless
    value in the SS column (UWIR 2025 convention, e.g. -1.6293e-2). The
    builder must convert it so that Ss·b == |SS| exactly."""
    props = _toy_properties()
    sy = 1.6293e-2
    props.loc[(props.IROW == 1) & (props.ICOL == 1), "SS"] = -sy
    g = build_grid_from_properties(props, "EPSG:28355")
    thickness = float(g.top[0, 0] - g.botm[0, 0, 0])
    S = float(g.ss[0, 0, 0]) * thickness
    assert np.isclose(S, sy)
    # Positive-SS cells stay as specific storage (1/m).
    assert np.isclose(float(g.ss[0, 1, 1]), 1e-5)


def test_recharge_fallback_applied_when_rch_empty():
    props = _toy_properties()
    props["rch"] = np.nan                       # the delivered export is empty
    g = build_grid_from_properties(props, "EPSG:28355",
                                   recharge_fallback_m_per_day=5.63e-5)
    # Outcrop row (IROW=1 → array row 0) gets the fallback, others zero.
    assert np.allclose(g.rch[0, :], 5.63e-5)
    assert np.all(g.rch[1:, :] == 0)
    # Without a fallback the field stays zero (and RCH is simply omitted).
    g2 = build_grid_from_properties(props, "EPSG:28355")
    assert not np.any(g2.rch > 0)

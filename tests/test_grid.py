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
                    "INODE": (ir - 1) * 3 + ic,     # unique per (row, col)
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


def test_negative_ss_decoded_by_magnitude():
    """The parent-model export marks water-table cells with negative SS but
    carries two magnitudes: Sy-like values (>1e-3, dimensionless — decode
    so Ss·b == |SS|) and Ss-like values (within the formation's specific-
    storage bounds — keep as 1/m via abs())."""
    props = _toy_properties()
    sy = 1.6293e-2
    ss_small = 1.63e-6
    props.loc[(props.IROW == 1) & (props.ICOL == 1), "SS"] = -sy       # Sy-like
    props.loc[(props.IROW == 1) & (props.ICOL == 2), "SS"] = -ss_small # Ss-like
    g = build_grid_from_properties(props, "EPSG:28355")
    thickness = float(g.top[0, 0] - g.botm[0, 0, 0])
    # Sy-like: storativity equals the exported value exactly.
    assert np.isclose(float(g.ss[0, 0, 0]) * thickness, sy)
    # Ss-like: kept as specific storage (1/m), NOT divided by thickness —
    # decoding it as dimensionless gave S ~ 2e-6 (near-zero storage) and
    # made spring drawdowns explode.
    assert np.isclose(float(g.ss[0, 0, 1]), ss_small)
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


def test_per_cell_recharge_by_inode_takes_precedence():
    """The INODE-keyed OGIA field is applied at exactly its cells (and wins
    over the uniform fallback) when the rch column is empty."""
    props = _toy_properties()
    props["rch"] = np.nan
    # Recharge on two specific cells only, keyed by their INODE.
    r1 = props.loc[(props.IROW == 1) & (props.ICOL == 1), "INODE"].iloc[0]
    r2 = props.loc[(props.IROW == 1) & (props.ICOL == 2), "INODE"].iloc[0]
    rbi = {int(r1): 3.0e-5, int(r2): 7.0e-5}
    g = build_grid_from_properties(props, "EPSG:28355",
                                   recharge_by_inode=rbi,
                                   recharge_fallback_m_per_day=5.63e-5)
    assert np.isclose(g.rch[0, 0], 3.0e-5)      # (IROW1,ICOL1) → row0,col0
    assert np.isclose(g.rch[0, 1], 7.0e-5)
    # Every other cell — including other outcrop cells — is zero: the
    # per-cell field defines the recharge footprint, not the fallback.
    mask = np.zeros_like(g.rch, dtype=bool); mask[0, 0] = mask[0, 1] = True
    assert np.all(g.rch[~mask] == 0)

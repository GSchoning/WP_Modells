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


def test_two_layer_merge():
    """Hutton-style split aquifer (ILAY 19 + 20) merges to one layer per cell:
    T-weighted kx, summed thickness, negative-Sy passthrough, INODE-keyed
    recharge summed across both layers' nodes."""
    rows = []
    for ilay, inode0, ntop, nbot, kx, ss in [
        (19, 100, 100.0, 40.0, 2.0, 1e-5),      # upper: b=60
        (20, 200, 40.0, 0.0, 8.0, 2e-5),        # lower: b=40
    ]:
        for ic in range(1, 3):
            rows.append({
                "ICOL": ic, "IROW": 1, "ILAY": ilay, "INODE": inode0 + ic,
                "IBOUND": 1, "NTOP": ntop, "NBOT": nbot,
                "X": 500_000 + (ic - 1) * 1500.0, "Y": 7_000_000,
                "THICKNESS": ntop - nbot,
                # cell (1,1): upper layer is water-table (negative Sy)
                "OUTCROP": "Y" if (ilay == 19 and ic == 1) else "N",
                "Depth": 50.0, "kx": kx,
                "SS": -5.878e-3 if (ilay == 19 and ic == 1) else ss,
                "rch": "",
            })
    # a cell where ONLY the lower layer exists (pinched upper)
    rows.append({
        "ICOL": 3, "IROW": 1, "ILAY": 20, "INODE": 203, "IBOUND": 1,
        "NTOP": 40.0, "NBOT": 0.0, "X": 500_000 + 2 * 1500.0, "Y": 7_000_000,
        "THICKNESS": 40.0, "OUTCROP": "Y", "Depth": 20.0, "kx": 8.0,
        "SS": -5.878e-3, "rch": "",
    })
    props = pd.DataFrame(rows)
    rch_by_inode = {101: 1e-4, 201: 2e-4, 203: 3e-4}   # both layers of cell 1 + L20-only cell

    g = build_grid_from_properties(props, "EPSG:28355", layer=[19, 20],
                                   recharge_by_inode=rch_by_inode)
    assert g.nrow == 1 and g.ncol == 3
    # geometry: top of upper, bottom of lower
    assert g.top[0, 1] == 100.0 and g.botm[0, 0, 1] == 0.0
    # T-weighted kx: (2*60 + 8*40) / 100 = 4.4
    assert np.isclose(g.k[0, 0, 1], 4.4)
    # plain cell: thickness-weighted Ss = (1e-5*60 + 2e-5*40)/100
    assert np.isclose(g.ss[0, 0, 1], (1e-5 * 60 + 2e-5 * 40) / 100)
    # water-table cell: Sy decodes so Ss*b == Sy over the MERGED thickness
    assert np.isclose(g.ss[0, 0, 0] * 100.0, 5.878e-3)
    assert g.outcrop_mask[0, 0] and not g.outcrop_mask[0, 1]
    # recharge summed across both layers' INODEs; L20-only cell keeps its own
    assert np.isclose(g.rch[0, 0], 3e-4)
    assert np.isclose(g.rch[0, 2], 3e-4)
    # single-layer cell inherits the lower layer's geometry
    assert g.top[0, 2] == 40.0 and np.isclose(g.ss[0, 0, 2] * 40.0, 5.878e-3)


def test_cell_of_round_trips():
    props = _toy_properties()
    g = build_grid_from_properties(props, "EPSG:28355")
    rc = cell_of(g, 500_000, 7_000_000)
    assert rc is not None


def test_negative_ss_default_gives_formation_sy_everywhere():
    """Default outcrop_storage='formation_sy': EVERY negative-marked
    (water-table) cell carries the formation-wide outcrop Sy as its
    storativity, per UWIR 2025 Table B.2-2 — including cells whose export
    magnitude was Ss-like."""
    props = _toy_properties()
    sy = 1.6293e-2
    ss_small = 1.63e-6
    props.loc[(props.IROW == 1) & (props.ICOL == 1), "SS"] = -sy       # Sy-like
    props.loc[(props.IROW == 1) & (props.ICOL == 2), "SS"] = -ss_small # Ss-like
    g = build_grid_from_properties(props, "EPSG:28355")
    thickness = float(g.top[0, 0] - g.botm[0, 0, 0])
    # Both water-table cells end up with S = formation Sy exactly.
    assert np.isclose(float(g.ss[0, 0, 0]) * thickness, sy)
    assert np.isclose(float(g.ss[0, 0, 1]) * thickness, sy)
    # Positive-SS cells stay as specific storage (1/m).
    assert np.isclose(float(g.ss[0, 1, 1]), 1e-5)


def test_negative_ss_as_exported_mode_keeps_ss_magnitudes():
    """outcrop_storage='as_exported': Sy-like negatives decode to Sy;
    Ss-like negatives are kept as specific storage (1/m)."""
    props = _toy_properties()
    sy = 1.6293e-2
    ss_small = 1.63e-6
    props.loc[(props.IROW == 1) & (props.ICOL == 1), "SS"] = -sy
    props.loc[(props.IROW == 1) & (props.ICOL == 2), "SS"] = -ss_small
    g = build_grid_from_properties(props, "EPSG:28355", outcrop_storage="as_exported")
    thickness = float(g.top[0, 0] - g.botm[0, 0, 0])
    assert np.isclose(float(g.ss[0, 0, 0]) * thickness, sy)
    assert np.isclose(float(g.ss[0, 0, 1]), ss_small)   # NOT divided by b


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

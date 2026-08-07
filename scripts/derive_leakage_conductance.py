"""Derive the per-cell Precipice leakage conductance from the parent Kz.

Vertical (quasi-3D) conductance between the Precipice (layer 24) and the
overlying Hutton aquifer (layers 19-20) through the Evergreen sequence
(layers 21-23), per plan-view cell:

    1/C_area = (b24/2)/Kz24 + b'/Kz_evg + (b20/2)/Kz20
    C_cell   = C_area * cell area

- b'    = Hutton NBOT (L20) - Precipice NTOP (L24), floored at 0 (the
          intervening Evergreen-sequence thickness per cell).
- Kz24 / Kz20 come per cell from the parent's calibrated Kz flat file
  (UWIRGen5_usg._kz, one value per USG node, indexed INODE-1).
- Kz_evg: the layers 21-23 node BLOCK's harmonic-mean Kz. Their nodes
  cannot be mapped to plan cells without allnodes.xyz, but the block is
  cleanly delimited by the known L20/L24 INODE ranges, so a formation-
  wide harmonic mean is used across the per-cell b'. The end-member
  half-cell terms use true per-cell values and dominate exactly where
  b' -> 0, so the thin-cover subcrop edge (the only place the
  conductance is material) is resolved by calibrated per-cell data.

Cells without Hutton cover (outside its extent, incl. the Precipice
outcrop belt) get no row - the outcrop already has recharge and drains.

Writes Data/Properties_recharge/leakage_conductance.csv
(INODE, ICOL, IROW, X, Y, b_intervening_m, cond_m2_per_day).

Run locally (needs only repo Data/ + the ._kz file):
    python scripts/derive_leakage_conductance.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
KZ_FILE = REPO / "Data" / "Properties_recharge" / "UWIRGen5_usg._kz"
PREC = REPO / "Data" / "Properties_recharge" / "properties.csv"
HUT = REPO / "Data" / "Hutton" / "properties.csv"
OUT = REPO / "Data" / "Properties_recharge" / "leakage_conductance.csv"

CELL_AREA = 1500.0 * 1500.0
KZ_FLOOR = 1e-12


def main() -> None:
    kz = pd.read_csv(KZ_FILE, sep=r"\s+", header=None, dtype=float).to_numpy().ravel()
    prec = pd.read_csv(PREC, usecols=["INODE", "ILAY", "IROW", "ICOL", "NTOP",
                                      "NBOT", "IBOUND", "X", "Y"])
    hut = pd.read_csv(HUT, usecols=["INODE", "ILAY", "IROW", "ICOL", "NTOP", "NBOT"])

    p24 = prec[prec.ILAY == 24].set_index(["IROW", "ICOL"])
    h20 = hut[hut.ILAY == 20].set_index(["IROW", "ICOL"])
    l20_max = int(hut[hut.ILAY == 20].INODE.max())
    l24_min = int(prec[prec.ILAY == 24].INODE.min())
    evg = kz[l20_max:l24_min - 1]                      # layers 21-23 block
    kz_evg = 1.0 / np.mean(1.0 / np.maximum(evg, KZ_FLOOR))
    print(f"Evergreen-sequence block (INODE {l20_max + 1:,}-{l24_min - 1:,}, "
          f"n={len(evg):,}): harmonic-mean Kz = {kz_evg:.3e} m/d")

    m = p24.join(h20, rsuffix="_h20", how="inner")     # covered cells only
    m = m[m.IBOUND == 1]
    kz24 = kz[m.INODE.values.astype(int) - 1]
    kz20 = kz[m.INODE_h20.values.astype(int) - 1]
    b24 = (m.NTOP - m.NBOT).values
    b20 = (m.NTOP_h20 - m.NBOT_h20).values
    bp = np.maximum(m.NBOT_h20.values - m.NTOP.values, 0.0)

    resist = ((b24 / 2) / np.maximum(kz24, KZ_FLOOR)
              + bp / kz_evg
              + (b20 / 2) / np.maximum(kz20, KZ_FLOOR))
    cond = CELL_AREA / resist

    out = pd.DataFrame({
        "INODE": m.INODE.values.astype(int),
        "ICOL": m.index.get_level_values("ICOL"),
        "IROW": m.index.get_level_values("IROW"),
        "X": m.X.values.astype(int),
        "Y": m.Y.values.astype(int),
        "b_intervening_m": np.round(bp, 2),
        "cond_m2_per_day": cond,
    })
    out.to_csv(OUT, index=False)
    print(f"{OUT}: {len(out):,} covered active cells "
          f"(of {int((prec[prec.ILAY == 24].IBOUND == 1).sum()):,} active)")
    print(f"  Kv/b' (1/d): p50 {np.percentile(cond / CELL_AREA, 50):.2e}  "
          f"p95 {np.percentile(cond / CELL_AREA, 95):.2e}  max {(cond / CELL_AREA).max():.2e}")
    print(f"  C (m2/d):    p50 {np.percentile(cond, 50):.2e}  max {cond.max():.2f}  "
          f"sum {cond.sum():.1f}")


if __name__ == "__main__":
    main()

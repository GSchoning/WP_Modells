"""Build the MODFLOW 6 structured grid from per-cell properties (CLAUDE.md §6.2).

The properties CSV already encodes a structured grid (ICOL, IROW, X, Y,
IBOUND, kx, SS, rch, NTOP, NBOT, THICKNESS, OUTCROP). We reconstruct the
DIS arrays directly from it instead of resampling rasters.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Grid:
    nrow: int
    ncol: int
    nlay: int
    xorigin: float
    yorigin: float
    delr: np.ndarray
    delc: np.ndarray
    top: np.ndarray            # (nrow, ncol)
    botm: np.ndarray           # (nlay, nrow, ncol)
    idomain: np.ndarray        # (nlay, nrow, ncol)
    k: np.ndarray              # (nlay, nrow, ncol)
    ss: np.ndarray             # (nlay, nrow, ncol)
    rch: np.ndarray            # (nrow, ncol) — masked to outcrop
    outcrop_mask: np.ndarray   # (nrow, ncol) bool
    crs: str


def build_grid_from_properties(properties: pd.DataFrame, crs: str) -> Grid:
    """Reconstruct a single-layer Grid from the per-cell properties table.

    Assumes ICOL / IROW are 1-based and X/Y are cell centres in project CRS.
    """
    df = properties.copy()
    df["ICOL"] = df["ICOL"].astype(int)
    df["IROW"] = df["IROW"].astype(int)

    nrow = int(df["IROW"].max())
    ncol = int(df["ICOL"].max())

    xs = np.sort(df["X"].unique())
    ys = np.sort(df["Y"].unique())
    dx = float(np.median(np.diff(xs))) if len(xs) > 1 else 1500.0
    dy = float(np.median(np.diff(ys))) if len(ys) > 1 else 1500.0

    xorigin = float(xs.min() - dx / 2)
    yorigin = float(ys.min() - dy / 2)

    delr = np.full(ncol, dx)
    delc = np.full(nrow, dy)

    def _to_array(col: str, fill: float = 0.0) -> np.ndarray:
        a = np.full((nrow, ncol), fill, dtype=float)
        r = df["IROW"].to_numpy() - 1
        c = df["ICOL"].to_numpy() - 1
        a[r, c] = pd.to_numeric(df[col], errors="coerce").fillna(fill).to_numpy()
        return a

    top = _to_array("NTOP")
    bot = _to_array("NBOT")
    k = _to_array("kx", fill=1e-6)
    ss = _to_array("SS", fill=1e-6)
    rch = _to_array("rch", fill=0.0)

    ibound = np.zeros((nrow, ncol), dtype=int)
    r = df["IROW"].to_numpy() - 1
    c = df["ICOL"].to_numpy() - 1
    ibound[r, c] = df["IBOUND"].astype(int).to_numpy()

    outcrop_mask = np.zeros((nrow, ncol), dtype=bool)
    outcrop_mask[r, c] = (df["OUTCROP"].astype(str).str.upper() == "Y").to_numpy()

    rch = np.where(outcrop_mask, rch, 0.0)

    return Grid(
        nrow=nrow,
        ncol=ncol,
        nlay=1,
        xorigin=xorigin,
        yorigin=yorigin,
        delr=delr,
        delc=delc,
        top=top,
        botm=bot[np.newaxis, :, :],
        idomain=ibound[np.newaxis, :, :],
        k=k[np.newaxis, :, :],
        ss=ss[np.newaxis, :, :],
        rch=rch,
        outcrop_mask=outcrop_mask,
        crs=crs,
    )


def cell_of(grid: Grid, x: float, y: float) -> tuple[int, int] | None:
    """Return (row, col) for a project-CRS coordinate, or None if off-grid."""
    col = int((x - grid.xorigin) // grid.delr[0])
    row = int((grid.yorigin + grid.delc.sum() - y) // grid.delc[0])
    if 0 <= row < grid.nrow and 0 <= col < grid.ncol:
        return row, col
    return None

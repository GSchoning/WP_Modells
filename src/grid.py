"""Build the MODFLOW 6 structured grid from per-cell properties (CLAUDE.md §6.2).

The properties CSV already encodes a structured grid (ICOL, IROW, X, Y,
IBOUND, kx, SS, rch, NTOP, NBOT, THICKNESS, OUTCROP). We reconstruct the
DIS arrays directly from it instead of resampling rasters.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# Defaults applied when a value is non-physical (negative / zero / NaN) in an
# active cell. These won't kill MF6 and are obvious to spot in outputs.
DEFAULT_K_M_PER_DAY = 1e-3
DEFAULT_SS_1_PER_M = 1e-5
MIN_THICKNESS_M = 1.0


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


def build_grid_from_properties(
    properties: pd.DataFrame, crs: str, *, layer: int = 24,
    recharge_by_inode: dict[int, float] | None = None,
    recharge_fallback_m_per_day: float | None = None,
    outcrop_storage: str = "formation_sy",
) -> Grid:
    """Reconstruct a single-layer Grid from the per-cell properties table.

    The source CSV is exported from a multi-layer regional model; rows for
    layers other than the Precipice (ILAY=24 by default) are filtered out so
    we don't overlay properties from other formations onto the same (row, col).

    Assumes ICOL / IROW are 1-based and X/Y are cell centres in project CRS.

    Recharge precedence: (1) `recharge_by_inode` (per-cell steady-state
    field keyed by INODE — the authoritative OGIA export) if supplied and
    the properties `rch` column is empty; (2) the `rch` column if it has
    values; (3) a uniform `recharge_fallback_m_per_day` over outcrop.
    """
    df = properties.copy()
    if "ILAY" in df.columns:
        n_before = len(df)
        df = df[pd.to_numeric(df["ILAY"], errors="coerce").astype("Int64") == layer]
        n_dropped = n_before - len(df)
        if n_dropped:
            import sys
            print(
                f"[grid] filtered {n_dropped} rows with ILAY != {layer}; "
                f"kept {len(df)} rows",
                file=sys.stderr,
            )
    if df.empty:
        raise ValueError(
            f"build_grid_from_properties: no rows with ILAY == {layer}. "
            "Check grid.properties_layer against the properties CSV."
        )
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
    k = _to_array("kx", fill=DEFAULT_K_M_PER_DAY)
    ss = _to_array("SS", fill=DEFAULT_SS_1_PER_M)
    rch = _to_array("rch", fill=0.0)

    ibound = np.zeros((nrow, ncol), dtype=int)
    r = df["IROW"].to_numpy() - 1
    c = df["ICOL"].to_numpy() - 1
    ibound[r, c] = df["IBOUND"].astype(int).to_numpy()

    outcrop_mask = np.zeros((nrow, ncol), dtype=bool)
    outcrop_mask[r, c] = (df["OUTCROP"].astype(str).str.upper() == "Y").to_numpy()

    rch = np.where(outcrop_mask, rch, 0.0)

    # If the properties `rch` column is empty (the delivered export is),
    # prefer the per-cell steady-state recharge field keyed by INODE — the
    # authoritative OGIA product. It is applied at exactly the cells OGIA
    # recharges (the northern exposed-outcrop belt), zero elsewhere.
    import sys
    if not np.any(rch > 0) and recharge_by_inode and "INODE" in df.columns:
        inodes = df["INODE"].to_numpy()
        rr = df["IROW"].to_numpy() - 1
        cc = df["ICOL"].to_numpy() - 1
        rch = np.zeros((nrow, ncol), dtype=float)
        n_applied = 0
        for ino, r_, c_ in zip(inodes, rr, cc):
            val = recharge_by_inode.get(int(ino))
            if val is not None and val > 0:
                rch[r_, c_] = float(val)
                n_applied += 1
        print(
            f"[grid] applied per-cell steady-state recharge on {n_applied} cells "
            f"from INODE-keyed field (total "
            f"{rch.sum()*float(delr[0])*float(delc[0])*365.25/1000:,.0f} ML/yr)",
            file=sys.stderr,
        )
    elif not np.any(rch > 0) and recharge_fallback_m_per_day:
        # Last resort: uniform rate over outcrop (only if no per-cell field).
        rch = np.where(outcrop_mask, float(recharge_fallback_m_per_day), 0.0)
        print(
            f"[grid] rch column empty and no per-cell field — applied uniform "
            f"fallback recharge {recharge_fallback_m_per_day:.3g} m/d over "
            f"{int(outcrop_mask.sum())} outcrop cells",
            file=sys.stderr,
        )

    # Sanitise / decode entries in active cells.
    # - K: take abs(); replace zeros with a default so MF6 doesn't choke.
    # - Negative SS: the parent-model export marks water-table (outcrop)
    #   cells with a negative sign, but carries TWO kinds of magnitude:
    #     * Sy-like (338 cells, exactly 1.6293e-2 — the formation-wide
    #       outcrop Sy annotated on UWIR 2025 Fig G.4-30): a DIMENSIONLESS
    #       storativity. Convert so Ss·b = |SS| exactly: ss = |SS|/b.
    #     * Ss-like (209 cells, exactly 1.63e-6 — within the UWIR Ss
    #       bounds 5.58e-7..1.33e-5 1/m): a plain specific storage that
    #       happens to carry the water-table sign. abs() only. Decoding
    #       these as dimensionless (the previous behaviour) produced
    #       storativities of ~2e-6 — near-zero storage through half the
    #       outcrop belt, which made drawdown at springs explode.
    #   The discriminator is magnitude: dimensionless Sy is >1e-3;
    #   physical Ss for this formation is <1.33e-5.
    # - Thickness: enforce a small minimum so DIS top > bot.
    SY_DECODE_THRESHOLD = 1e-3
    active = ibound == 1
    neg_k = active & (k < 0)
    neg_ss = active & (ss < 0)
    k_abs = np.abs(k)
    ss_abs = np.abs(ss)
    zero_k = active & (k_abs == 0)
    zero_ss = active & (ss_abs == 0)
    k_abs[zero_k] = DEFAULT_K_M_PER_DAY
    ss_abs[zero_ss] = DEFAULT_SS_1_PER_M
    k = k_abs
    bad_thickness = active & ~((top - bot) >= MIN_THICKNESS_M)
    if bad_thickness.any():
        bot[bad_thickness] = top[bad_thickness] - MIN_THICKNESS_M

    thickness = np.maximum(top - bot, MIN_THICKNESS_M)
    neg_sy = neg_ss & (ss_abs > SY_DECODE_THRESHOLD)

    # `outcrop_storage` decides what the Ss-magnitude negatives get:
    #   - "formation_sy" (default): every negative-marked (water-table)
    #     cell carries the formation-wide outcrop Sy, matching UWIR 2025
    #     Table B.2-2 where outcrop Sy is a single formation-wide
    #     parameter. The mixed magnitudes in the export don't correlate
    #     with exposure, burial depth or recharge, so they're treated as
    #     an export artefact rather than a storage signal.
    #   - "as_exported": Sy-magnitude negatives decode to Sy; Ss-magnitude
    #     negatives stay as specific storage (1/m).
    formation_sy = float(np.median(ss_abs[neg_sy])) if neg_sy.any() else None
    n_promoted = 0
    if outcrop_storage == "formation_sy" and formation_sy is not None:
        n_promoted = int((neg_ss & ~neg_sy).sum())
        ss = np.where(neg_ss, formation_sy / thickness, ss_abs)
    else:
        ss = np.where(neg_sy, ss_abs / thickness, ss_abs)

    sanitised = {
        "k_negative_abs_taken": int(neg_k.sum()),
        "ss_negative_sy_decoded_as_dimensionless": int(neg_sy.sum()),
        "ss_negative_promoted_to_formation_sy": n_promoted,
        "ss_negative_kept_as_specific_storage": int((neg_ss & ~neg_sy).sum()) - n_promoted,
        "k_zero_set_to_default": int(zero_k.sum()),
        "ss_zero_set_to_default": int(zero_ss.sum()),
        "thickness_too_small": int(bad_thickness.sum()),
    }
    if any(sanitised.values()):
        import sys
        print(
            f"[grid] sanitised/decoded properties in active cells: {sanitised}",
            file=sys.stderr,
        )

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


def synthetic_uniform_grid(
    nrow: int = 51,
    ncol: int = 51,
    dx: float = 500.0,
    dy: float = 500.0,
    K: float = 1.0,
    Ss: float = 1e-5,
    thickness: float = 100.0,
    crs: str = "EPSG:28355",
) -> Grid:
    """Build a uniform single-layer Grid for analytical-solution testing."""
    top = np.full((nrow, ncol), thickness)
    botm = np.zeros((1, nrow, ncol))
    idomain = np.ones((1, nrow, ncol), dtype=int)
    k = np.full((1, nrow, ncol), K)
    ss = np.full((1, nrow, ncol), Ss)
    rch = np.zeros((nrow, ncol))
    outcrop = np.zeros((nrow, ncol), dtype=bool)
    return Grid(
        nrow=nrow,
        ncol=ncol,
        nlay=1,
        xorigin=0.0,
        yorigin=0.0,
        delr=np.full(ncol, dx),
        delc=np.full(nrow, dy),
        top=top,
        botm=botm,
        idomain=idomain,
        k=k,
        ss=ss,
        rch=rch,
        outcrop_mask=outcrop,
        crs=crs,
    )

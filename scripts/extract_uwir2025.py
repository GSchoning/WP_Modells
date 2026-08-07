"""Extract per-aquifer screening-tool inputs from the UWIR 2025 regional model.

Reads the OGIA UWIR 2025 MODFLOW-USG model files on the espogia01 share and
produces, for each target aquifer, the file set described in
docs/data_request_hutton_gubberamunda.md:

    properties.csv    - per-cell ICOL,IROW,ILAY,INODE,IBOUND,NTOP,NBOT,X,Y,
                        THICKNESS,OUTCROP,Depth,kx,SS,rch (same schema as the
                        OGIA Precipice export in Data/Properties_recharge/)
    recharge_SS.csv   - INODE,X,Y,RCH_SS_m_per_day (stress period 1 rates)
    ghb_cells.csv     - calibrated GHB cells: head AND conductance per cell
    predev_heads.csv  - pre-development (steady-state) starting heads
    outcrop.shp       - dissolved outcrop polygon (cells with negative SS)

Before writing anything it regenerates the Precipice (layer 24) export from
the same sources and diffs it against the repo copy - every column must match
or the run aborts.

Conventions verified against the model files / Precipice export:
    - properties.csv rows = ALL nodes of the layer; CSV index = INODE-1.
    - SS is copied verbatim from UWIRGen5_usg._ss_cal_adj; negative values
      mark water-table (outcrop) cells and carry the formation-wide Sy.
    - OUTCROP = 'Y' exactly where SS < 0 (547/547 match on the Precipice).
    - Depth = (top of the uppermost existing node in the same grid column)
      - (cell midpoint elevation).
    - Grid: 1500 m, GDA94 / MGA zone 55 (EPSG:28355), IROW 1 = north.

After the validated core export, a best-effort "benchmark / leakage extras"
pass pulls what the screening tool needs to TEST itself against the parent
model (each item skips loudly if its source file is absent):
    stress_periods.csv + wel_history.csv - the parent's staged pumping
        schedule per aquifer (benchmarks the all-on-at-once assumption)
    wel_sp_totals.csv / layer_catalogue.csv - shared context (Data/UWIR2025_shared/)
    adjacent_L*_properties/heads(+sy/kz).csv - sandwich layers for a
        quasi-3D leakage term through the confining beds
    parent_heads.npz - the base run's binary heads at the aquifer's nodes,
        if a *.hds output is on the share (the direct drawdown benchmark)

Run (no arguments; paths are constants below):
    conda run -n OGIApy python scripts/extract_uwir2025.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- constants

SHARE_ROOT = Path(r"\\espogia01\scratchHDD\UWIR2025")
MODEL_DIR = SHARE_ROOT / "Base_models" / "Groundwater" / "Base"
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "Data"

ALLNODES = SHARE_ROOT / "allnodes.xyz"
DISU = MODEL_DIR / "UWIRGen5_usg_TR_Pred.disu"
BAS = MODEL_DIR / "UWIRGen5_usg_dualss.bas"
KX_FILE = MODEL_DIR / "UWIRGen5_usg._kx"
SS_FILE = MODEL_DIR / "UWIRGen5_usg._ss_cal_adj"
SSHDS_FILE = MODEL_DIR / "UWIRGen5_usg._sshds"
GHB_FILE = MODEL_DIR / "UWIRGen5_usg_dualss.ghb"
RCH_FILE = MODEL_DIR / "UWIRGen5_usg_TR_Pred.rch"
RCH_NODES = MODEL_DIR / "rch_node.ref"
RIV_FILE = MODEL_DIR / "UWIRGen5_usg_TR_Pred.riv"

PRECIPICE_PROPS = DATA_DIR / "Properties_recharge" / "properties.csv"
PRECIPICE_RCH = DATA_DIR / "Properties_recharge" / "Precipice_L24_SS_recharge.csv"

# --- benchmark / leakage extras (best-effort; each guarded in main) --------
WEL_FILE = MODEL_DIR / "UWIRGen5_usg_TR_Pred.wel"
# Directories globbed for USG binary head output (*.hds) of the base run.
HDS_SEARCH_DIRS = [MODEL_DIR, MODEL_DIR.parent]
SHARED_DIR = DATA_DIR / "UWIR2025_shared"

CELL = 1500.0
CRS = "EPSG:28355"  # GDA94 / MGA zone 55
NLAY = 35

# aquifer -> (model layers, output folder)
AQUIFERS = {
    "Gubberamunda": ([7], DATA_DIR / "Gubberamunda"),
    "Hutton": ([19, 20], DATA_DIR / "Hutton"),
}
# Precipice: only the new files (calibrated GHB + predev heads); the existing
# OGIA properties/recharge exports in Properties_recharge/ are left untouched.
PRECIPICE_LAYERS = [24]
PRECIPICE_DIR = DATA_DIR / "Properties_recharge"

PROP_COLUMNS = ["ICOL", "IROW", "ILAY", "INODE", "IBOUND", "NTOP", "NBOT",
                "X", "Y", "THICKNESS", "OUTCROP", "Depth", "kx", "SS", "rch"]


# ------------------------------------------------------------- file parsers

def _label_of(control_line: str) -> tuple[str, str, float | None]:
    """Parse a USG array control card -> (kind, normalised label, const value).

    INTERNAL/EXTERNAL cards: 'INTERNAL 1.0 (FREE) -1 Top of layer 1'
    CONSTANT cards:          'Constant 0.010 ALPHA for Layer 12'
    The label is lowercased with whitespace collapsed, e.g. 'top of layer 1'.
    """
    tok = control_line.split()
    kind = tok[0].lower()
    if kind == "constant":
        return kind, re.sub(r"\s+", " ", " ".join(tok[2:])).lower(), float(tok[1])
    return kind, re.sub(r"\s+", " ", " ".join(tok[4:])).lower(), None


def read_labelled_arrays(path: Path, wanted: dict[str, int]) -> dict[str, np.ndarray]:
    """Read labelled arrays from a free-format USG package file.

    `wanted` maps a normalised control-card label (see _label_of) to the array
    length. Reading stops once every wanted array is collected. Assumes no
    unwanted INTERNAL array appears before the last wanted one (true for the
    DISU and BAS layouts handled here); bare numeric lines before the first
    control card (header items like LAYCBD) are skipped.
    """
    out: dict[str, np.ndarray] = {}
    pending: str | None = None
    need = 0
    buf: list[str] = []
    with open(path) as f:
        f.readline()  # header line (NODES NLAY ... / BAS options)
        for line in f:
            if pending is not None:
                buf.extend(line.split())
                if len(buf) >= need:
                    out[pending] = np.asarray(buf[:need], dtype=float)
                    pending = None
                    if all(k in out for k in wanted):
                        return out
                continue
            first = line.split()[0].lower() if line.split() else ""
            if first in ("internal", "external", "constant"):
                kind, label, const = _label_of(line)
                if label not in wanted:
                    raise ValueError(f"{path.name}: unexpected array card {line.rstrip()!r}")
                if kind == "constant":
                    out[label] = np.full(wanted[label], const)
                    if all(k in out for k in wanted):
                        return out
                elif kind == "external":
                    raise ValueError(f"{path.name}: EXTERNAL not supported: {line.rstrip()!r}")
                else:
                    pending, need, buf = label, wanted[label], []
            # else: bare numeric header line (e.g. LAYCBD) - skip
    missing = [k for k in wanted if k not in out]
    raise ValueError(f"{path.name}: arrays not found: {missing}")


def read_disu(path: Path) -> tuple[np.ndarray, dict[int, np.ndarray], dict[int, np.ndarray]]:
    """Return (nodelay[NLAY], tops{layer: array}, bots{layer: array})."""
    with open(path) as f:
        nodes, nlay = (int(v) for v in f.readline().split()[:2])
    assert nlay == NLAY, f"expected {NLAY} layers, DISU says {nlay}"

    nodelay = read_labelled_arrays(path, {"nodelay": nlay})["nodelay"].astype(int)
    assert nodelay.sum() == nodes, "NODELAY does not sum to NODES"

    wanted: dict[str, int] = {"nodelay": nlay}
    for k in range(1, nlay + 1):
        wanted[f"top of layer {k}"] = int(nodelay[k - 1])
        wanted[f"bottom of layer {k}"] = int(nodelay[k - 1])
    arrs = read_labelled_arrays(path, wanted)
    tops = {k: arrs[f"top of layer {k}"] for k in range(1, nlay + 1)}
    bots = {k: arrs[f"bottom of layer {k}"] for k in range(1, nlay + 1)}
    return nodelay, tops, bots


def read_bas_ibound(path: Path, nodelay: np.ndarray) -> dict[int, np.ndarray]:
    wanted = {f"ibound for layer {k}": int(nodelay[k - 1]) for k in range(1, NLAY + 1)}
    arrs = read_labelled_arrays(path, wanted)
    return {k: arrs[f"ibound for layer {k}"].astype(int) for k in range(1, NLAY + 1)}


def read_flat_array(path: Path, n_expected: int) -> np.ndarray:
    """One-value-per-line property file (._kx, ._ss_cal_adj, ._sshds)."""
    vals = pd.read_csv(path, sep=r"\s+", header=None, dtype=float).to_numpy().ravel()
    assert len(vals) == n_expected, f"{path.name}: {len(vals)} values, expected {n_expected}"
    return vals


def read_ghb(path: Path) -> pd.DataFrame:
    with open(path) as f:
        f.readline()                       # MXACTB IGHBCB
        n = int(f.readline().split()[0])   # ITMP for stress period 1
        rows = [f.readline().split()[:3] for _ in range(n)]
    df = pd.DataFrame(rows, columns=["INODE", "head_m", "cond_m2_per_day"])
    return df.astype({"INODE": int, "head_m": float, "cond_m2_per_day": float})


def read_recharge_ss(rch_path: Path, nodes_path: Path) -> pd.DataFrame:
    """Steady-state recharge: the first stress-period array labelled
    '(steady-state 1995)' (SP 337+ of the prediction run; SPs 1-336 are the
    transient 1995-2023 monthly history). Verified to match the OGIA
    Precipice_L24_SS_recharge.csv export exactly."""
    nodes = np.asarray(nodes_path.read_text().split(), dtype=int)
    with open(rch_path) as f:
        f.readline()                       # NRCHOP IRCHCB
        n = int(f.readline().split()[0])   # MXNDRCH
        assert len(nodes) == n, f"rch_node.ref has {len(nodes)} nodes, RCH expects {n}"
        while True:
            sp_line = f.readline()
            if not sp_line:
                raise ValueError("no '(steady-state 1995)' recharge array found")
            inrech, inirch = (int(t) for t in sp_line.split()[:2])
            ctrl = ""
            if inrech >= 0:
                ctrl = f.readline()
                assert "internal" in ctrl.lower(), f"unexpected RCH control line: {ctrl!r}"
                vals: list[str] = []
                while len(vals) < n:
                    vals.extend(f.readline().split())
            if inirch >= 0:
                f.readline()               # IRCH OPEN/CLOSE 'rch_node.ref' card
            if "steady-state" in ctrl.lower():
                break
    rates = np.asarray(vals[:n], dtype=float)
    return pd.DataFrame({"INODE": nodes, "RCH_SS_m_per_day": rates})


def read_riv_sp1(path: Path) -> pd.DataFrame:
    """Stress-period-1 RIV cells. The UWIR 2025 model uses RIV for the
    surficial drains (stage == rbot, so reaches only ever remove water);
    conductances are the calibrated drain conductances."""
    with open(path) as f:
        f.readline()                       # MXACTR IRIVCB
        n = int(f.readline().split()[0])   # ITMP for stress period 1
        rows = [f.readline().split()[:4] for _ in range(n)]
    df = pd.DataFrame(rows, columns=["INODE", "stage_m", "cond_m2_per_day", "rbot_m"])
    return df.astype({"INODE": int, "stage_m": float,
                      "cond_m2_per_day": float, "rbot_m": float})


# ---------------------------------------------------- benchmark extraction
#
# Everything the screening tool needs from the parent model to TEST itself
# (rather than just to be built from it):
#   stress_periods.csv   - PERLEN/NSTP/TSMULT/SS-TR per SP + cumulative time,
#                          so the WEL history can be put on a calendar.
#   wel_history.csv      - per-aquifer, per-SP well cells and rates: the
#                          parent's actual staged pumping schedule. Feeding
#                          this to our model gives the like-for-like
#                          benchmark of the all-on-at-once assumption.
#   wel_sp_totals.csv    - all-layer per-SP totals (shared) - shows the
#                          staging and which layers carry the stress.
#   adjacent_L*_...csv   - properties + predev heads for the layers directly
#                          above/below each aquifer: the inputs for a
#                          quasi-3D leakage term (aquitard Kv*A/b' GHBs).
#   layer_catalogue.csv  - one-line inventory per model layer (n nodes,
#                          active, thickness/kx stats) to map layer numbers
#                          onto stratigraphy without guessing.
#   flat-file inventory  - lists UWIRGen5_usg._* property files present
#                          (looking for ._sy / ._kz / ._kv: Sy confirms our
#                          conversion parameter, Kz drives the leakage term);
#                          any found are exported per aquifer + neighbours.
#   parent_heads_L*.npz  - if a binary head file of the base run is on the
#                          share: the parent's own head time series for the
#                          aquifer's nodes - the direct drawdown benchmark.


def read_stress_periods(path: Path) -> pd.DataFrame:
    """PERLEN NSTP TSMULT SS/TR lines from the tail of the DISU file."""
    with open(path) as f:
        header = f.readline().split()
    nper = int(header[4])                      # NODES NLAY NJAG IVSD NPER ...
    lines = [ln.split() for ln in path.read_text().splitlines() if ln.strip()]
    rows = []
    for tok in lines[-nper:]:
        perlen, nstp, tsmult = float(tok[0]), int(tok[1]), float(tok[2])
        flag = tok[3].upper() if len(tok) > 3 else "TR"
        rows.append((perlen, nstp, tsmult, flag))
    df = pd.DataFrame(rows, columns=["perlen_days", "nstp", "tsmult", "sstr"])
    if not ((df.perlen_days > 0).all() and df.sstr.isin(["SS", "TR"]).all()):
        raise ValueError(f"{path.name}: last {nper} lines do not parse as stress periods")
    df.insert(0, "iper", np.arange(1, nper + 1))
    df["t_end_days"] = df.perlen_days.cumsum()
    df["t_start_days"] = df.t_end_days - df.perlen_days
    return df


def read_wel_history(path: Path, nper: int) -> pd.DataFrame:
    """All stress periods of the WEL package -> (iper, INODE, q_m3_per_day).

    ITMP < 0 reuses the previous period's list (standard MODFLOW semantics);
    reused periods are materialised so every SP is explicit in the output.
    """
    records: list[tuple[int, int, float]] = []
    prev: list[tuple[int, float]] = []
    with open(path) as f:
        f.readline()                           # MXACTW IWELCB
        for sp in range(1, nper + 1):
            line = f.readline()
            if not line:                       # WEL file ends early: remaining
                break                          # SPs implicitly reuse... stop.
            itmp = int(line.split()[0])
            if itmp < 0:
                cells = prev
            else:
                cells = []
                for _ in range(itmp):
                    tok = f.readline().split()
                    cells.append((int(tok[0]), float(tok[1])))
                prev = cells
            records.extend((sp, node, q) for node, q in cells)
    return pd.DataFrame(records, columns=["iper", "INODE", "q_m3_per_day"])


def export_wel_history(model: "Model", sps: pd.DataFrame) -> pd.DataFrame | None:
    if not WEL_FILE.exists():
        print(f"  [skip] WEL file not found: {WEL_FILE}")
        return None
    print(f"reading WEL history ({WEL_FILE.name}) ...", flush=True)
    wel = read_wel_history(WEL_FILE, nper=len(sps))
    lay_of = model.nodes.set_index("INODE").ILAY
    wel["ILAY"] = lay_of.loc[wel.INODE].values

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    sps.to_csv(SHARED_DIR / "stress_periods.csv", index=False)
    totals = (wel.groupby(["iper", "ILAY"], as_index=False)
                 .agg(q_m3_per_day=("q_m3_per_day", "sum"),
                      n_wells=("INODE", "count")))
    totals.to_csv(SHARED_DIR / "wel_sp_totals.csv", index=False)
    print(f"  {SHARED_DIR/'stress_periods.csv'}  ({len(sps)} SPs, "
          f"{sps.t_end_days.iloc[-1]:.0f} model days)")
    print(f"  {SHARED_DIR/'wel_sp_totals.csv'}  ({len(totals)} rows, "
          f"{wel.INODE.nunique()} distinct well nodes, all layers)")
    return wel


def export_wel_for_aquifer(name: str, layers: list[int], out_dir: Path,
                           model: "Model", wel: pd.DataFrame) -> None:
    sub = wel[wel.ILAY.isin(layers)].copy()
    if sub.empty:
        print(f"  wel_history.csv   no WEL cells on layers {layers}")
        return
    nodes = model.nodes.set_index("INODE")
    for c in ("ICOL", "IROW", "X", "Y"):
        sub[c] = nodes.loc[sub.INODE, c].astype(int).values
    sub[["iper", "ILAY", "INODE", "ICOL", "IROW", "X", "Y", "q_m3_per_day"]] \
        .to_csv(out_dir / "wel_history.csv", index=False)
    print(f"  wel_history.csv   {len(sub):>7} rows "
          f"({sub.INODE.nunique()} wells, SPs {sub.iper.min()}-{sub.iper.max()})")


def flat_file_inventory() -> dict[str, Path]:
    """List UWIRGen5_usg._* one-value-per-node property files on the share."""
    found = {p.name.split("._", 1)[1]: p
             for p in sorted(MODEL_DIR.glob("UWIRGen5_usg._*"))}
    print("\nflat property files on the share:")
    for suffix, p in found.items():
        print(f"  ._{suffix:<16} {p.stat().st_size/1e6:8.1f} MB")
    for want in ("sy", "kz", "kv"):
        if want not in found:
            print(f"  [note] no ._{want} file - check LPF/UPW package for the "
                  f"{'Sy' if want == 'sy' else 'vertical-K'} arrays instead")
    return found


ADJACENT_LAYERS = {
    "Gubberamunda": [6, 8],
    "Hutton": [18, 21],
    "Precipice": [23, 25],
}


def export_adjacent_layers(name: str, out_dir: Path, model: "Model",
                           flats: dict[str, Path]) -> None:
    """Properties + predev heads (+ any Sy/Kz) for the sandwich layers —
    the raw inputs for a quasi-3D leakage term."""
    n_nodes = len(model.nodes)
    extra_cols = {}
    for suffix in ("sy", "kz", "kv"):
        if suffix in flats:
            extra_cols[suffix] = read_flat_array(flats[suffix], n_nodes)
    base = name.split(" ")[0]
    for lay in ADJACENT_LAYERS.get(base, []):
        props = model.properties([lay])
        heads = model.layer_slice([lay])[["INODE", "X", "Y", "head_predev_m"]]
        props.to_csv(out_dir / f"adjacent_L{lay}_properties.csv")
        heads.to_csv(out_dir / f"adjacent_L{lay}_heads.csv", index=False)
        msg = f"  adjacent_L{lay}_*  {len(props):>7} nodes"
        if extra_cols:
            idx = model.layer_slice([lay]).INODE.values - 1
            ex = pd.DataFrame({"INODE": idx + 1,
                               **{s: v[idx] for s, v in extra_cols.items()}})
            ex.to_csv(out_dir / f"adjacent_L{lay}_{'_'.join(extra_cols)}.csv", index=False)
            msg += f" (+ {', '.join(extra_cols)})"
        print(msg)


def export_layer_catalogue(model: "Model") -> None:
    rows = []
    for lay, grp in model.nodes.groupby("ILAY"):
        act = grp.IBOUND == 1
        thick = (grp.NTOP - grp.NBOT)[act]
        rows.append({
            "ILAY": lay, "n_nodes": len(grp), "n_active": int(act.sum()),
            "thickness_p50_m": round(float(thick.median()), 1) if act.any() else np.nan,
            "kx_p50_m_per_day": round(float(grp.kx[act].abs().median()), 4) if act.any() else np.nan,
            "n_watertable_marked": int((grp.SS < 0).sum()),
        })
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(SHARED_DIR / "layer_catalogue.csv", index=False)
    print(f"  {SHARED_DIR/'layer_catalogue.csv'}  (35-layer inventory)")


def export_parent_heads(name: str, layers: list[int], out_dir: Path,
                        model: "Model") -> None:
    """If the base run's binary heads are on the share, pull the aquifer's
    node heads at every saved time — the direct benchmark target."""
    hds = None
    for d in HDS_SEARCH_DIRS:
        cands = sorted(d.glob("*.hds")) + sorted(d.glob("*.HDS"))
        if cands:
            hds = cands[0]
            break
    if hds is None:
        print("  [skip] no *.hds head output found on the share "
              f"(searched {', '.join(str(d) for d in HDS_SEARCH_DIRS)})")
        return
    try:
        import flopy
        hf = flopy.utils.HeadUFile(str(hds))
        times = hf.get_times()
        node_idx = model.layer_slice(layers).INODE.values - 1
        # HeadUFile returns a list of per-layer arrays; concatenate to the
        # global node vector, then take this aquifer's nodes.
        series = np.empty((len(times), len(node_idx)), dtype=np.float32)
        for i, t in enumerate(times):
            full = np.concatenate([np.atleast_1d(a) for a in hf.get_data(totim=t)])
            series[i] = full[node_idx]
        np.savez_compressed(
            out_dir / "parent_heads.npz",
            times_days=np.asarray(times), inode=node_idx + 1, heads=series,
            source=str(hds),
        )
        print(f"  parent_heads.npz  {len(times)} times x {len(node_idx)} nodes "
              f"(from {hds.name})")
    except Exception as exc:
        print(f"  [skip] parent heads extraction failed: {type(exc).__name__}: {exc}")


# ------------------------------------------------------------ model globals

class Model:
    """All per-node arrays assembled into one frame, plus layer slices."""

    def __init__(self) -> None:
        print("reading DISU (grid geometry) ...", flush=True)
        self.nodelay, tops, bots = read_disu(DISU)

        print("reading allnodes.xyz (cell centres) ...", flush=True)
        xyz = pd.read_csv(ALLNODES, sep=r"\s+", header=None,
                          names=["INODE", "X", "Y", "Z10", "LAY"],
                          dtype={"INODE": int, "X": float, "Y": float, "LAY": int})
        n_nodes = int(self.nodelay.sum())
        assert len(xyz) == n_nodes and (xyz.INODE.values == np.arange(1, n_nodes + 1)).all()

        print("reading BAS (IBOUND), kx, SS, starting heads ...", flush=True)
        ibound = read_bas_ibound(BAS, self.nodelay)
        kx = read_flat_array(KX_FILE, n_nodes)
        ss = read_flat_array(SS_FILE, n_nodes)
        sshds = read_flat_array(SSHDS_FILE, n_nodes)

        df = xyz.drop(columns=["Z10"]).rename(columns={"LAY": "ILAY"})
        df["NTOP"] = np.concatenate([tops[k] for k in range(1, NLAY + 1)])
        df["NBOT"] = np.concatenate([bots[k] for k in range(1, NLAY + 1)])
        df["IBOUND"] = np.concatenate([ibound[k] for k in range(1, NLAY + 1)])
        df["kx"] = kx
        df["SS"] = ss
        df["head_predev_m"] = sshds

        # layer sanity: allnodes' layer column must agree with NODELAY ranges
        expect = np.repeat(np.arange(1, NLAY + 1), self.nodelay)
        assert (df.ILAY.values == expect).all(), "allnodes.xyz layer column mismatch"

        # 1500 m grid indices; IROW 1 = northernmost row
        x0, y0 = df.X.min(), df.Y.max()
        icol = (df.X - x0) / CELL + 1
        irow = (y0 - df.Y) / CELL + 1
        assert np.allclose(icol, np.round(icol)) and np.allclose(irow, np.round(irow))
        df["ICOL"] = icol.round().astype(int)
        df["IROW"] = irow.round().astype(int)

        # ground surface per grid column = top of the uppermost existing node
        surf = (df.sort_values("ILAY")
                  .drop_duplicates(["ICOL", "IROW"])
                  .set_index(["ICOL", "IROW"])["NTOP"])
        df["SURFACE"] = surf.loc[pd.MultiIndex.from_frame(df[["ICOL", "IROW"]])].values

        self.nodes = df

    def layer_slice(self, layers: list[int]) -> pd.DataFrame:
        return self.nodes[self.nodes.ILAY.isin(layers)].copy()

    def properties(self, layers: list[int]) -> pd.DataFrame:
        df = self.layer_slice(layers)
        df["THICKNESS"] = df.NTOP - df.NBOT
        df["OUTCROP"] = np.where(df.SS < 0, "Y", "N")
        df["Depth"] = df.SURFACE - (df.NTOP + df.NBOT) / 2.0
        df["X"] = df.X.astype(int)
        df["Y"] = df.Y.astype(int)
        df["rch"] = ""
        out = df[PROP_COLUMNS].copy()
        out.index = (df.INODE - 1).values  # matches the OGIA Precipice export
        out.index.name = None
        return out


# --------------------------------------------------------------- validation

def validate_against_precipice(model: Model, rch: pd.DataFrame, ghb: pd.DataFrame) -> None:
    """Rebuild the Precipice L24 export from the share and diff the repo copy."""
    print("\n=== validation: regenerate Precipice L24 and diff repo export ===")
    ours = model.properties(PRECIPICE_LAYERS).sort_index()
    ref = pd.read_csv(PRECIPICE_PROPS, index_col=0).sort_index()
    assert len(ours) == len(ref), f"row count {len(ours)} vs {len(ref)}"
    assert (ours.index == ref.index).all(), "row index (INODE-1) mismatch"

    failures = []
    for col in PROP_COLUMNS:
        if col == "rch":  # empty in the reference export
            ok = bool(ref[col].isna().all())
            detail = "reference column empty" if ok else "reference rch not empty!"
        elif col == "OUTCROP":
            ok = bool((ours[col] == ref[col]).all())
            detail = f"{(ours[col] != ref[col]).sum()} mismatches"
        elif col in ("ICOL", "IROW", "ILAY", "INODE", "IBOUND", "X", "Y"):
            ok = bool((ours[col].values == ref[col].values).all())
            detail = f"{(ours[col].values != ref[col].values).sum()} mismatches"
        elif col == "Depth":
            # OGIA's ground surface comes from their topo DEM; ours is the
            # uppermost model-node top. They deviate by <2 m on ~80 of 37207
            # Precipice cells - immaterial for screening-level depths.
            diff = np.abs(ours[col].values - ref[col].values)
            ok = bool((diff <= 2.0).all())
            detail = (f"max abs diff {np.nanmax(diff):.3f} m, "
                      f"{int((diff > 1e-3).sum())} cells > 1 mm (tol 2 m)")
        else:
            diff = np.abs(ours[col].values - ref[col].values)
            scale = np.maximum(np.abs(ref[col].values), 1e-12)
            ok = bool((diff <= 1e-4 * scale + 1e-6).all())
            detail = f"max abs diff {np.nanmax(diff):.3e}"
        print(f"  {col:<10} {'OK ' if ok else 'FAIL'}  ({detail})")
        if not ok:
            failures.append(col)

    # recharge: every OGIA export row must appear in ours with an identical
    # steady-state rate. Ours is a superset - OGIA post-filtered the Precipice
    # export to 215 of the 547 water-table cells by a rule not reproducible
    # from the model files (their outcrop mapping); we ship all cells.
    ref_rch = pd.read_csv(PRECIPICE_RCH)
    lay = model.nodes.set_index("INODE").ILAY
    ours_rch = rch[lay.loc[rch.INODE].values == 24]
    merged = ref_rch.merge(ours_rch, on="INODE", how="left", suffixes=("_ref", "_new"))
    n_missing = int(merged.RCH_SS_m_per_day_new.isna().sum())
    rate_diff = np.abs(merged.RCH_SS_m_per_day_ref - merged.RCH_SS_m_per_day_new).max()
    ok = n_missing == 0 and rate_diff <= 1e-9
    print(f"  recharge   {'OK ' if ok else 'FAIL'}  ({len(ref_rch)} ref rows all present, "
          f"max rate diff {rate_diff:.3e}; we export {len(ours_rch)} rows - superset)")
    if not ok:
        failures.append("recharge")

    n_ghb24 = int((lay.loc[ghb.INODE].values == 24).sum())
    print(f"  (info) Precipice GHB cells found: {n_ghb24}")

    if failures:
        raise SystemExit(f"VALIDATION FAILED for columns: {failures} - nothing written.")
    print("  all checks passed.")


# ------------------------------------------------------------------ outputs

def write_cells_shapefile(cells: pd.DataFrame, path: Path) -> int:
    """Dissolve 1500 m cells (X/Y centres) into a polygon shapefile."""
    import geopandas as gpd
    from shapely.geometry import box
    from shapely.ops import unary_union

    cells = cells[["X", "Y"]].drop_duplicates()
    if cells.empty:
        return 0
    h = CELL / 2.0
    # union across layers (Hutton L19+L20 may overlap in plan view)
    geom = unary_union([box(x - h, y - h, x + h, y + h) for x, y in cells.values])
    gdf = gpd.GeoDataFrame({"formation": [path.parent.name]}, geometry=[geom], crs=CRS)
    gdf.to_file(path)
    return len(cells)


NOTES_TEMPLATE = """# {name} - UWIR 2025 model extraction notes

Extracted by scripts/extract_uwir2025.py from the OGIA UWIR 2025 regional
model (MODFLOW-USG) at {model_dir}
Model layers: {layers}. Grid 1500 m, GDA94 / MGA zone 55 (EPSG:28355),
IROW 1 = north. INODE = global USG node number.

- properties.csv: all nodes of the layer(s); same schema as the OGIA
  Precipice export. SS is verbatim from UWIRGen5_usg._ss_cal_adj; negative
  SS marks water-table (outcrop) cells and its magnitude is the
  DIMENSIONLESS formation-wide Sy (divide by cell thickness for a 1/m Ss).
  OUTCROP = 'Y' where SS < 0. Depth = uppermost-model-node top minus cell
  midpoint (OGIA's own export used their topo DEM; differs by < 2 m on
  ~0.2 % of cells). kx in m/day from UWIRGen5_usg._kx.
- recharge_SS.csv: steady-state recharge (the '(steady-state 1995)' arrays,
  SP 337+ of the prediction run) at ALL of the aquifer's recharge nodes.
  NOTE: for the Precipice, OGIA's export kept only 215 of the 547
  water-table cells (filter not reproducible from the model files, rates
  match exactly on those 215) - confirm OGIA's cell filter if it matters.
- ghb_cells.csv: the model's actual GHB cells with CALIBRATED head and
  conductance per cell (supersedes transcribing Appendix F/G figures).
- riv_cells.csv: RIV package cells on these layers (stress period 1). The
  UWIR 2025 model implements surficial drains via RIV (stage == rbot);
  cond is the calibrated drain conductance (m2/day).
- predev_heads.csv: steady-state starting heads (UWIRGen5_usg._sshds) =
  pre-development potentiometric surface.
- outcrop.shp: dissolved 1500 m cells with SS < 0 (union across layers).
- extent.shp: dissolved active (IBOUND=1) cells - the model-derived
  formation extent (stands in for an official OGIA extent polygon).

Validation: the same code regenerates the OGIA Precipice layer-24
properties.csv byte-equivalent numerically (all columns; Depth within 2 m
on 80/37207 cells) and its recharge rates exactly, before anything is
written.
"""


def export_aquifer(name: str, layers: list[int], out_dir: Path,
                   model: Model, rch: pd.DataFrame, ghb: pd.DataFrame,
                   riv: pd.DataFrame, properties: bool = True) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes = model.layer_slice(layers).set_index("INODE")
    lay_of = model.nodes.set_index("INODE").ILAY
    print(f"\n--- {name} (layers {layers}) -> {out_dir}")

    if properties:
        props = model.properties(layers)
        props.to_csv(out_dir / "properties.csv")
        print(f"  properties.csv    {len(props):>7} rows "
              f"(outcrop {(props.OUTCROP == 'Y').sum()}, IBOUND!=1: {(props.IBOUND != 1).sum()})")

        r = rch[lay_of.loc[rch.INODE].isin(layers).values].copy()
        r["X"] = nodes.loc[r.INODE, "X"].astype(int).values
        r["Y"] = nodes.loc[r.INODE, "Y"].astype(int).values
        r[["INODE", "X", "Y", "RCH_SS_m_per_day"]].to_csv(out_dir / "recharge_SS.csv", index=False)
        print(f"  recharge_SS.csv   {len(r):>7} rows")

        n_cells = write_cells_shapefile(props[props.OUTCROP == "Y"], out_dir / "outcrop.shp")
        print(f"  outcrop.shp       {n_cells:>7} cells dissolved")

        n_ext = write_cells_shapefile(props[props.IBOUND == 1], out_dir / "extent.shp")
        print(f"  extent.shp        {n_ext:>7} active cells dissolved")

    g = ghb[lay_of.loc[ghb.INODE].isin(layers).values].copy()
    g["ILAY"] = lay_of.loc[g.INODE].values
    for c in ("ICOL", "IROW", "X", "Y"):
        g[c] = nodes.loc[g.INODE, c].astype(int).values
    g[["ILAY", "INODE", "ICOL", "IROW", "X", "Y", "head_m", "cond_m2_per_day"]] \
        .to_csv(out_dir / "ghb_cells.csv", index=False)
    print(f"  ghb_cells.csv     {len(g):>7} rows")

    v = riv[lay_of.loc[riv.INODE].isin(layers).values].copy()
    v["ILAY"] = lay_of.loc[v.INODE].values
    for c in ("ICOL", "IROW", "X", "Y"):
        v[c] = nodes.loc[v.INODE, c].astype(int).values
    v[["ILAY", "INODE", "ICOL", "IROW", "X", "Y", "stage_m", "cond_m2_per_day", "rbot_m"]] \
        .to_csv(out_dir / "riv_cells.csv", index=False)
    print(f"  riv_cells.csv     {len(v):>7} rows (surficial drains)")

    h = nodes.reset_index()[["INODE", "X", "Y", "ILAY", "head_predev_m"]].copy()
    h[["X", "Y"]] = h[["X", "Y"]].astype(int)
    h.to_csv(out_dir / "predev_heads.csv", index=False)
    print(f"  predev_heads.csv  {len(h):>7} rows")

    notes_name = "extraction_notes.md" if not properties else "notes.md"
    (out_dir / notes_name).write_text(
        NOTES_TEMPLATE.format(name=name, model_dir=MODEL_DIR, layers=layers))
    print(f"  {notes_name}")


def main() -> None:
    model = Model()
    print("reading GHB, RIV (surficial drains) and steady-state recharge ...", flush=True)
    ghb = read_ghb(GHB_FILE)
    riv = read_riv_sp1(RIV_FILE)
    rch = read_recharge_ss(RCH_FILE, RCH_NODES)

    validate_against_precipice(model, rch, ghb)

    for name, (layers, out_dir) in AQUIFERS.items():
        export_aquifer(name, layers, out_dir, model, rch, ghb, riv)
    # Precipice upgrade: calibrated GHB/RIV cells + predev heads only
    export_aquifer("Precipice (upgrade files only)", PRECIPICE_LAYERS,
                   PRECIPICE_DIR, model, rch, ghb, riv, properties=False)

    # ---- benchmark / leakage extras (best-effort: a failure here must not
    # invalidate the validated core export above) -------------------------
    print("\n=== benchmark / leakage extras ===")
    targets = {**{n: (l, d) for n, (l, d) in AQUIFERS.items()},
               "Precipice": (PRECIPICE_LAYERS, PRECIPICE_DIR)}

    def _guard(label, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            print(f"  [skip] {label}: {type(exc).__name__}: {exc}")
            return None

    flats = _guard("flat-file inventory", flat_file_inventory) or {}
    _guard("layer catalogue", export_layer_catalogue, model)
    sps = _guard("stress periods", read_stress_periods, DISU)
    wel = _guard("WEL history", export_wel_history, model, sps) if sps is not None else None
    for name, (layers, out_dir) in targets.items():
        print(f"\n--- {name} extras -> {out_dir}")
        if wel is not None:
            _guard("wel_history", export_wel_for_aquifer, name, layers, out_dir, model, wel)
        _guard("adjacent layers", export_adjacent_layers, name, out_dir, model, flats)
        _guard("parent heads", export_parent_heads, name, layers, out_dir, model)

    print("\ndone.")


if __name__ == "__main__":
    sys.exit(main())

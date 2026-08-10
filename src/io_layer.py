"""Ingest + validate user-supplied inputs (CLAUDE.md §6.1).

Reads shapefiles, the per-cell properties CSV, and the OGIA water-use CSV;
reprojects everything to the project CRS; emits a validation report.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

from .config import Config


@dataclass
class Inputs:
    formation_extent: gpd.GeoDataFrame    # always polygon(s) in project CRS
    outcrop: gpd.GeoDataFrame
    properties: pd.DataFrame              # per-cell grid + properties
    pumping_bores: gpd.GeoDataFrame       # all bores with extraction (Scenario A)
    receptor_bores: gpd.GeoDataFrame      # non-S&D subset for impact reporting
    licensed_bores: gpd.GeoDataFrame      # entitlement (auth + non-S&D) subset — s_licensed
    springs: gpd.GeoDataFrame | None      # may be None until shapefile supplied


ML_PER_YEAR_TO_M3_PER_DAY = 1000.0 / 365.25

# Springs beyond assessment.spring_outcrop_buffer_m from the outcrop are
# dropped at ingest — see config.AssessmentCfg. (The old module constant
# SPRINGS_OUTCROP_BUFFER_M was 1 km; the knob default is 3 km.)


def _read_water_use(cfg: Config) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return (pumping_bores, receptor_bores, licensed_bores) in project CRS.

    Pumping bores = all rows with a positive rate in the configured rate column,
    optionally filtered to the configured formation. Receptor bores = pumping
    bores excluding the values listed in `receptor_filter.exclude_values`
    (e.g. Stock_Domestic). Licensed bores = the entitlement subset per
    `licensed_filter` (holds an authority number AND non-S&D); used for the
    separate s_licensed impact layer. Falls back to the receptor set when
    `licensed_filter` is unset.
    """
    wu_cfg = cfg.inputs.water_use
    df = pd.read_csv(wu_cfg.path)

    if wu_cfg.formation_col and wu_cfg.formation_value:
        df = df[df[wu_cfg.formation_col] == wu_cfg.formation_value]

    df = df[pd.to_numeric(df[wu_cfg.rate_col], errors="coerce").fillna(0) > 0].copy()
    if wu_cfg.rate_units == "ML/year":
        df["rate_m3_per_day"] = df[wu_cfg.rate_col] * ML_PER_YEAR_TO_M3_PER_DAY
    else:
        df["rate_m3_per_day"] = df[wu_cfg.rate_col]

    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df[wu_cfg.lon_col], df[wu_cfg.lat_col]),
        crs=wu_cfg.source_crs,
    ).to_crs(cfg.project.crs)

    pumping = gdf.rename(columns={wu_cfg.id_col: "bore_id"})

    receptors = pumping
    if wu_cfg.receptor_filter:
        col = wu_cfg.receptor_filter["column"]
        excl = wu_cfg.receptor_filter.get("exclude_values", [])
        receptors = pumping[~pumping[col].isin(excl)].copy()

    licensed = _filter_licensed(pumping, wu_cfg.licensed_filter, receptors)

    return pumping, receptors, licensed


def _filter_licensed(
    pumping: gpd.GeoDataFrame,
    licensed_filter: dict | None,
    receptors: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Entitlement subset of `pumping`: holds an authority number AND is not
    in the excluded (Stock&Domestic) classes. Falls back to `receptors`
    (the non-S&D set) when no licensed_filter is configured."""
    if not licensed_filter:
        return receptors.copy()
    auth_col = licensed_filter.get("auth_col")
    has_auth = pd.Series(True, index=pumping.index)
    if auth_col and auth_col in pumping.columns:
        s = pumping[auth_col].astype(str).str.strip()
        has_auth = pumping[auth_col].notna() & ~s.isin(("", "nan", "None"))
    keep = has_auth
    excl_col = licensed_filter.get("exclude_column")
    excl_vals = licensed_filter.get("exclude_values", [])
    if excl_col and excl_col in pumping.columns and excl_vals:
        keep = keep & ~pumping[excl_col].isin(excl_vals)
    return pumping[keep].copy()


def load_recharge_by_inode(cfg: Config) -> dict[int, float] | None:
    """Per-cell steady-state recharge (m/day) keyed by INODE, or None.

    Reads cfg.inputs.recharge_csv — an OGIA export with columns
    INODE and RCH_SS_m_per_day (the steady-state recharge field for the
    Precipice outcrop). Joined to the grid by INODE in grid.py.
    """
    path = getattr(cfg.inputs, "recharge_csv", None)
    if path is None or not Path(path).exists():
        return None
    df = pd.read_csv(path)
    rate_col = next((c for c in df.columns if "rch" in c.lower() or "rech" in c.lower()), None)
    if "INODE" not in df.columns or rate_col is None:
        raise ValueError(
            f"recharge_csv {path} must have INODE and a recharge-rate column; "
            f"found {list(df.columns)}"
        )
    return {int(i): float(r) for i, r in zip(df["INODE"], df[rate_col]) if pd.notna(r)}


def _read_springs(cfg: Config) -> gpd.GeoDataFrame | None:
    p = cfg.inputs.springs
    if p is None or not Path(p).exists():
        return None
    gdf = gpd.read_file(p)
    if gdf.crs is None:
        raise ValueError(f"springs shapefile {p} has no CRS")
    gdf = gdf.to_crs(cfg.project.crs)

    # Optional attribute filter — the shared springs layer attributes each
    # spring to a source aquifer (e.g. source_aqu contains "Hutton").
    flt = getattr(cfg.inputs, "springs_attr_filter", None)
    if flt:
        col, needle = flt["column"], str(flt["contains"])
        if col not in gdf.columns:
            raise ValueError(f"springs shapefile {p} has no column {col!r}")
        keep = gdf[col].astype(str).str.contains(needle, case=False, na=False)
        import sys
        print(
            f"[springs] attribute filter {col} ~ {needle!r}: kept "
            f"{int(keep.sum())} of {len(gdf)} springs",
            file=sys.stderr,
        )
        gdf = gdf[keep].copy()
    return gdf


def _polygonize_extent(gdf: gpd.GeoDataFrame, properties: pd.DataFrame, crs: str) -> gpd.GeoDataFrame:
    """Ensure the formation-extent layer is polygonal.

    The supplied "Edge around Precipice Sandstone" shapefile is a polyline,
    so we close it into polygons. If polygonization yields nothing usable
    (line not closed), fall back to the convex hull of active cells from
    the properties CSV — that's what the model actually uses as its domain.
    """
    geom_types = set(gdf.geom_type.unique())
    if geom_types <= {"Polygon", "MultiPolygon"}:
        return gdf

    merged = unary_union(gdf.geometry.tolist())
    polys = list(polygonize([merged]))
    if polys:
        union = unary_union(polys)
        if isinstance(union, Polygon):
            union = MultiPolygon([union])
        return gpd.GeoDataFrame(geometry=[union], crs=crs)

    active = properties[properties["IBOUND"].astype(int) == 1]
    hull = gpd.GeoSeries(gpd.points_from_xy(active["X"], active["Y"]), crs=crs).unary_union.convex_hull
    return gpd.GeoDataFrame(geometry=[hull], crs=crs)


def augment_inputs_with_approved(
    inputs: Inputs, wells: list[dict], crs: str,
) -> Inputs:
    """Fold the legislative ledger (active approved decisions) into the
    bore sets, returning a new Inputs (the raw CSV frames are untouched).

    Every ledger well joins `pumping_bores` (rates keep their sign, so a
    trade's source reduction nets off against the original bore at the
    same cell). Positive-rate wells are entitlement take by definition —
    they also join `licensed_bores` and `receptor_bores`, so subsequent
    assessments both count their impact and report impacts ON them.
    """
    if not wells:
        return inputs
    from dataclasses import replace
    from shapely.geometry import Point

    rows = gpd.GeoDataFrame(
        {
            "bore_id": [f"{w['label']} [{w['decision_id']}]" for w in wells],
            "rate_m3_per_day": [w["rate_ML_per_year"] * ML_PER_YEAR_TO_M3_PER_DAY
                                for w in wells],
            "approved": True,
        },
        geometry=[Point(w["x"], w["y"]) for w in wells],
        crs=crs,
    )

    def _cat(base: gpd.GeoDataFrame, extra: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        out = pd.concat([base, extra], ignore_index=True)
        if "approved" in out.columns:
            out["approved"] = out["approved"].astype("boolean").fillna(False).astype(bool)
        return gpd.GeoDataFrame(out, geometry="geometry", crs=base.crs or crs)

    positive = rows[rows.rate_m3_per_day > 0]
    return replace(
        inputs,
        pumping_bores=_cat(inputs.pumping_bores, rows),
        licensed_bores=_cat(inputs.licensed_bores, positive),
        receptor_bores=_cat(inputs.receptor_bores, positive),
    )


def load_inputs(cfg: Config) -> Inputs:
    formation_raw = gpd.read_file(cfg.inputs.formation_extent).to_crs(cfg.project.crs)
    outcrop = gpd.read_file(cfg.inputs.outcrop).to_crs(cfg.project.crs)
    properties = pd.read_csv(cfg.inputs.properties_csv)
    formation = _polygonize_extent(formation_raw, properties, cfg.project.crs)
    pumping, receptors, licensed = _read_water_use(cfg)
    springs = _read_springs(cfg)

    if springs is not None and len(springs):
        buffer_m = float(cfg.assessment.spring_outcrop_buffer_m)
        outcrop_buffered = outcrop.union_all().buffer(buffer_m)
        near_outcrop = springs.within(outcrop_buffered)
        n_dropped = int((~near_outcrop).sum())
        if n_dropped:
            import sys
            print(
                f"[springs] dropped {n_dropped} of {len(springs)} springs "
                f">{buffer_m:.0f} m from outcrop; "
                f"kept {int(near_outcrop.sum())}",
                file=sys.stderr,
            )
        springs = springs[near_outcrop].copy()

        # Normalise the complex name and drop springs without one. Spring
        # complexes are the regulatory unit of analysis; an unaffiliated
        # spring would have to become its own complex which complicates
        # downstream reporting for little gain.
        complex_col = cfg.assessment.spring_complex_col
        if complex_col in springs.columns:
            springs[complex_col] = springs[complex_col].astype(str).str.strip()
            blanks = springs[complex_col].isin(("", "nan", "None")) | springs[complex_col].isna()
            n_blank = int(blanks.sum())
            if n_blank:
                import sys
                print(
                    f"[springs] dropped {n_blank} springs with no {complex_col}",
                    file=sys.stderr,
                )
            springs = springs[~blanks].copy()

    return Inputs(
        formation_extent=formation,
        outcrop=outcrop,
        properties=properties,
        pumping_bores=pumping,
        receptor_bores=receptors,
        licensed_bores=licensed,
        springs=springs,
    )


def _in_active_domain(gdf: gpd.GeoDataFrame, grid) -> pd.Series:
    """Vectorised per-point check that (x, y) lands in an active (IBOUND=1) cell."""
    import numpy as np

    xs = gdf.geometry.x.to_numpy()
    ys = gdf.geometry.y.to_numpy()
    cols = ((xs - grid.xorigin) // grid.delr[0]).astype(int)
    # Row 0 is the top of the grid in MF6 convention; Y decreases as row index grows.
    y_top = grid.yorigin + grid.delc.sum()
    rows = ((y_top - ys) // grid.delc[0]).astype(int)

    in_bounds = (rows >= 0) & (rows < grid.nrow) & (cols >= 0) & (cols < grid.ncol)
    inside = np.zeros(len(gdf), dtype=bool)
    valid = np.where(in_bounds)[0]
    if valid.size:
        inside[valid] = grid.idomain[0, rows[valid], cols[valid]] == 1
    return pd.Series(inside, index=gdf.index)


def validate(inputs: Inputs, cfg: Config, grid=None) -> list[str]:
    """Return a list of human-readable validation findings. Empty list = clean.

    If `grid` is provided, points are checked against the IBOUND active-cell
    mask (the model's actual domain). Otherwise the formation_extent polygon
    is used.
    """
    findings: list[str] = []

    if grid is not None:
        pb_inside = _in_active_domain(inputs.pumping_bores, grid)
        if (~pb_inside).any():
            findings.append(
                f"{(~pb_inside).sum()} of {len(inputs.pumping_bores)} pumping bores fall "
                "outside the active model domain (IBOUND=1)."
            )
        if inputs.springs is not None:
            sp_inside = _in_active_domain(inputs.springs, grid)
            if (~sp_inside).any():
                from .grid import resolve_receptor_cells

                snap_m = float(getattr(cfg.assessment, "spring_snap_max_m", 0.0))
                cells = resolve_receptor_cells(
                    inputs.springs.geometry.x, inputs.springs.geometry.y, grid,
                    snap_max_m=snap_m,
                )
                n_snap = sum(1 for t in cells if t is not None and t[2] > 0)
                n_excl = sum(1 for t in cells if t is None)
                findings.append(
                    f"{(~sp_inside).sum()} of {len(inputs.springs)} springs fall outside "
                    f"the active model domain (IBOUND=1): {n_snap} will be sampled at the "
                    f"nearest active cell (≤{snap_m:.0f} m), {n_excl} are beyond snap "
                    "range and excluded."
                )
    else:
        extent = inputs.formation_extent.unary_union
        inside = inputs.pumping_bores.within(extent)
        if (~inside).any():
            findings.append(
                f"{(~inside).sum()} of {len(inputs.pumping_bores)} pumping bores fall "
                "outside the formation extent polygon."
            )
        if inputs.springs is not None:
            s_inside = inputs.springs.within(extent)
            if (~s_inside).any():
                findings.append(
                    f"{(~s_inside).sum()} of {len(inputs.springs)} springs fall outside "
                    "the formation extent polygon."
                )

    if inputs.springs is None:
        findings.append("Springs shapefile not present — spring reporting will be skipped.")

    pb = cfg.inputs.proposed_bore
    if pb.x is None or pb.y is None or pb.rate_ML_per_year is None:
        findings.append(
            "Proposed bore (Scenario C) is unset — set inputs.proposed_bore.{x,y,rate_ML_per_year} "
            "before running Scenario C."
        )

    return findings

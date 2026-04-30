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
    springs: gpd.GeoDataFrame | None      # may be None until shapefile supplied


ML_PER_YEAR_TO_M3_PER_DAY = 1000.0 / 365.25

# Springs >1 km from the outcrop are not hydrologically connected to the
# Precipice in a way this single-layer model can represent, so we drop them
# at ingest rather than reporting drawdown that's structurally meaningless.
SPRINGS_OUTCROP_BUFFER_M = 1000.0


def _read_water_use(cfg: Config) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return (pumping_bores, receptor_bores) as GeoDataFrames in project CRS.

    Pumping bores = all rows with a positive rate in the configured rate column,
    optionally filtered to the configured formation. Receptor bores = pumping
    bores excluding the values listed in `receptor_filter.exclude_values`
    (e.g. Stock_Domestic).
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

    return pumping, receptors


def _read_springs(cfg: Config) -> gpd.GeoDataFrame | None:
    p = cfg.inputs.springs
    if p is None or not Path(p).exists():
        return None
    gdf = gpd.read_file(p)
    if gdf.crs is None:
        raise ValueError(f"springs shapefile {p} has no CRS")
    return gdf.to_crs(cfg.project.crs)


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


def load_inputs(cfg: Config) -> Inputs:
    formation_raw = gpd.read_file(cfg.inputs.formation_extent).to_crs(cfg.project.crs)
    outcrop = gpd.read_file(cfg.inputs.outcrop).to_crs(cfg.project.crs)
    properties = pd.read_csv(cfg.inputs.properties_csv)
    formation = _polygonize_extent(formation_raw, properties, cfg.project.crs)
    pumping, receptors = _read_water_use(cfg)
    springs = _read_springs(cfg)

    if springs is not None and len(springs):
        outcrop_buffered = outcrop.unary_union.buffer(SPRINGS_OUTCROP_BUFFER_M)
        near_outcrop = springs.within(outcrop_buffered)
        n_dropped = int((~near_outcrop).sum())
        if n_dropped:
            import sys
            print(
                f"[springs] dropped {n_dropped} of {len(springs)} springs "
                f">{SPRINGS_OUTCROP_BUFFER_M:.0f} m from outcrop; "
                f"kept {int(near_outcrop.sum())}",
                file=sys.stderr,
            )
        springs = springs[near_outcrop].copy()

    return Inputs(
        formation_extent=formation,
        outcrop=outcrop,
        properties=properties,
        pumping_bores=pumping,
        receptor_bores=receptors,
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
                findings.append(
                    f"{(~sp_inside).sum()} of {len(inputs.springs)} springs fall outside "
                    "the active model domain (IBOUND=1)."
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

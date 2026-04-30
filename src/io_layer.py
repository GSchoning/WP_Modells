"""Ingest + validate user-supplied inputs (CLAUDE.md §6.1).

Reads shapefiles, the per-cell properties CSV, and the OGIA water-use CSV;
reprojects everything to the project CRS; emits a validation report.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .config import Config


@dataclass
class Inputs:
    formation_extent: gpd.GeoDataFrame
    outcrop: gpd.GeoDataFrame
    properties: pd.DataFrame              # per-cell grid + properties
    pumping_bores: gpd.GeoDataFrame       # all bores with extraction (Scenario A)
    receptor_bores: gpd.GeoDataFrame      # non-S&D subset for impact reporting
    springs: gpd.GeoDataFrame | None      # may be None until shapefile supplied


ML_PER_YEAR_TO_M3_PER_DAY = 1000.0 / 365.25


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


def load_inputs(cfg: Config) -> Inputs:
    formation = gpd.read_file(cfg.inputs.formation_extent).to_crs(cfg.project.crs)
    outcrop = gpd.read_file(cfg.inputs.outcrop).to_crs(cfg.project.crs)
    properties = pd.read_csv(cfg.inputs.properties_csv)
    pumping, receptors = _read_water_use(cfg)
    springs = _read_springs(cfg)

    return Inputs(
        formation_extent=formation,
        outcrop=outcrop,
        properties=properties,
        pumping_bores=pumping,
        receptor_bores=receptors,
        springs=springs,
    )


def validate(inputs: Inputs, cfg: Config) -> list[str]:
    """Return a list of human-readable validation findings. Empty list = clean."""
    findings: list[str] = []

    extent = inputs.formation_extent.unary_union
    inside = inputs.pumping_bores.within(extent)
    if (~inside).any():
        findings.append(
            f"{(~inside).sum()} of {len(inputs.pumping_bores)} pumping bores fall "
            "outside the formation extent."
        )

    if inputs.springs is not None:
        s_inside = inputs.springs.within(extent)
        if (~s_inside).any():
            findings.append(
                f"{(~s_inside).sum()} of {len(inputs.springs)} springs fall outside "
                "the formation extent."
            )
    else:
        findings.append("Springs shapefile not present — spring reporting will be skipped.")

    pb = cfg.inputs.proposed_bore
    if pb.x is None or pb.y is None or pb.rate_m3_per_day is None:
        findings.append(
            "Proposed bore (Scenario C) is unset — set inputs.proposed_bore.{x,y,rate_m3_per_day} "
            "before running Scenario C."
        )

    return findings

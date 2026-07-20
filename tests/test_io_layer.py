"""Ingest-layer tests — the licensed-take filter (CLAUDE.md §5)."""
from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from src.io_layer import _filter_licensed


def _bores() -> gpd.GeoDataFrame:
    # 4 bores: two entitlement (auth + non-S&D), one S&D with an auth ref,
    # one non-S&D but no auth ref.
    df = pd.DataFrame({
        "bore_id": ["ENT1", "ENT2", "SD_AUTH", "IRR_NOAUTH"],
        "AUTH_REFS": ["401001", "401002", "409999", None],
        "OGIA_P3": ["Irrigation", "Town Water Supply", "Stock_Domestic", "Irrigation"],
    })
    return gpd.GeoDataFrame(
        df, geometry=[Point(i, i) for i in range(len(df))], crs="EPSG:7855"
    )


LIC_FILTER = {
    "auth_col": "AUTH_REFS",
    "exclude_column": "OGIA_P3",
    "exclude_values": ["Stock_Domestic"],
}


def test_filter_licensed_requires_auth_and_non_sd():
    pumping = _bores()
    receptors = pumping[pumping.OGIA_P3 != "Stock_Domestic"].copy()
    licensed = _filter_licensed(pumping, LIC_FILTER, receptors)
    # ENT1/ENT2 have an auth ref AND are non-S&D; SD_AUTH excluded (S&D);
    # IRR_NOAUTH excluded (no auth ref).
    assert set(licensed.bore_id) == {"ENT1", "ENT2"}


def test_filter_licensed_treats_blank_auth_as_unlicensed():
    pumping = _bores()
    pumping.loc[pumping.bore_id == "ENT1", "AUTH_REFS"] = "   "   # whitespace only
    licensed = _filter_licensed(pumping, LIC_FILTER, pumping)
    assert set(licensed.bore_id) == {"ENT2"}


def test_filter_licensed_falls_back_to_receptors_when_unset():
    pumping = _bores()
    receptors = pumping[pumping.OGIA_P3 != "Stock_Domestic"].copy()
    licensed = _filter_licensed(pumping, None, receptors)
    # No licensed_filter → fall back to the non-S&D receptor set.
    assert set(licensed.bore_id) == set(receptors.bore_id)

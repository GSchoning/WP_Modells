"""Config loads cleanly against the repo's config.yaml."""
from __future__ import annotations

from pathlib import Path

from src.config import load_config


def test_repo_config_loads():
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    assert cfg.project.crs == "EPSG:28355"
    assert cfg.inputs.water_use.source_crs == "EPSG:4283"
    assert cfg.inputs.water_use.rate_col == "ML_Aquifer"
    assert "Stock_Domestic" in cfg.inputs.water_use.receptor_filter["exclude_values"]
    # Boundary-audit + UWIR 2019 Fig A1-14 outcome: closed pinch-outs,
    # GHBs only on the W/S truncation faces, drains as the SS outlet.
    assert cfg.assessment.boundary_mode == "uwir_ghb"
    assert cfg.assessment.ghb_faces == ["W", "S"]
    assert cfg.drains.enabled is True

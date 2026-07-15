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
    # Boundary-audit + UWIR 2025 Fig F.1-15 outcome: closed pinch-outs,
    # GHBs only on the WESTERN truncation face (2019 also had southern;
    # 2025 does not), calibrated pilot heads, drains as the SS outlet.
    assert cfg.assessment.boundary_mode == "uwir_ghb"
    assert cfg.assessment.ghb_faces == ["W"]
    assert cfg.assessment.ghb_heads == "uwir2025_pilot"
    assert cfg.drains.enabled is True
    # The delivered CSV's rch column is empty; the UWIR-balance fallback
    # (25,283 ML/yr over 1,231 km²) must be configured.
    assert cfg.inputs.recharge_fallback_m_per_day is not None


def test_cache_code_fingerprint_deterministic():
    """The baseline cache key includes a fingerprint of the model-building
    source files, so code changes invalidate cached baselines automatically."""
    from src.api.cache import _code_fingerprint
    a, b = _code_fingerprint(), _code_fingerprint()
    assert a == b and len(a) == 16 and all(ch in "0123456789abcdef" for ch in a)

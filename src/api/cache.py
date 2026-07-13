"""Disk cache for Scenario A.

Scenario A is invariant within an assessment cycle (existing licensed
extractions don't change while a regulator iterates on a proposed bore),
so we run it once and reuse the result. Cache key is a hash of the
inputs that drive Scenario A — config, properties CSV, water-use CSV.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config


CACHE_DIR = Path("outputs/cache")

# Bump when the cached receptors_df schema changes (column names, what
# rows mean) OR when the underlying simulation produces materially
# different numbers (boundary conditions, IC formulation, time stepping,
# etc.). v2 = per-complex aggregation with n_springs column. v3 =
# boundary CHD excludes outcrop cells. v4 = chd_quadrants. v5 = yearly
# fine-period stress block (fine_period_years). v6 = per-complex time
# series persisted alongside output-year aggregates. v7 = stress periods
# aligned to output years + cached no-pump twin heads. v8 = negative-SS
# decoded as dimensionless Sy, recharge fallback, UWIR-2025 W-only GHBs
# with calibrated pilot heads.
CACHE_SCHEMA_VERSION = "v8"


@dataclass
class BaselineCache:
    key: str
    receptors_df: pd.DataFrame                          # tidy springs table
    drawdown_by_year: dict[float, np.ndarray]           # for raster overlays
    complex_series_df: pd.DataFrame                      # long-form per-complex series
    # No-pump twin run, identical for every scenario with this config.
    # Persisting it means Scenario C requests only run MF6 once.
    nopump_times_days: np.ndarray | None = None
    nopump_heads: np.ndarray | None = None              # (nt, nrow, ncol)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def baseline_key(cfg: Config, config_path: Path) -> str:
    """Hash of every input that affects Scenario A's cached output."""
    parts = [
        CACHE_SCHEMA_VERSION,
        _file_sha256(Path(config_path)),
        _file_sha256(Path(cfg.inputs.properties_csv)),
        (_file_sha256(Path(cfg.inputs.recharge_csv))
         if cfg.inputs.recharge_csv and Path(cfg.inputs.recharge_csv).exists() else "no-rch-csv"),
        _file_sha256(Path(cfg.inputs.water_use.path)),
        f"rmult={cfg.assessment.recharge_multiplier:.6g}",
        f"bmode={cfg.assessment.boundary_mode}",
        f"ghbf={','.join(cfg.assessment.ghb_faces)}",
        f"ghbs={cfg.assessment.ghb_conductance_scale:.6g}",
        f"ghbh={cfg.assessment.ghb_heads}",
        f"rfall={cfg.inputs.recharge_fallback_m_per_day}",
        f"chdq={','.join(cfg.assessment.chd_quadrants or [])}",
    ]
    if cfg.inputs.springs is not None and Path(cfg.inputs.springs).exists():
        parts.append(_file_sha256(Path(cfg.inputs.springs)))
    if Path(cfg.inputs.outcrop).exists():
        parts.append(_file_sha256(Path(cfg.inputs.outcrop)))
    # Rejected-recharge drains derive elevations from the DEM, so the DEM
    # content becomes baseline-relevant once drains are enabled.
    if cfg.drains.enabled and cfg.inputs.dem is not None and Path(cfg.inputs.dem).exists():
        parts.append("drains")
        parts.append(_file_sha256(Path(cfg.inputs.dem)))
        parts.append(f"dcond={cfg.drains.conductance_m2_per_day}")
        parts.append(f"dscale={cfg.drains.conductance_scale:.6g}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def cache_paths(key: str) -> tuple[Path, Path, Path, Path, Path]:
    base = CACHE_DIR / key
    return (
        base / "receptors.parquet",
        base / "drawdown_by_year.npz",
        base / "manifest.json",
        base / "complex_series.parquet",
        base / "nopump.npz",
    )


def load(key: str) -> BaselineCache | None:
    receptors_p, drawdown_p, manifest_p, series_p, nopump_p = cache_paths(key)
    if not (receptors_p.exists() and drawdown_p.exists() and manifest_p.exists() and series_p.exists()):
        return None
    receptors = pd.read_parquet(receptors_p)
    npz = np.load(drawdown_p)
    drawdown = {float(name.removeprefix("y")): npz[name] for name in npz.files}
    series = pd.read_parquet(series_p)
    nopump_times = nopump_heads = None
    if nopump_p.exists():
        nz = np.load(nopump_p)
        nopump_times, nopump_heads = nz["times_days"], nz["heads"]
    return BaselineCache(
        key=key, receptors_df=receptors, drawdown_by_year=drawdown,
        complex_series_df=series,
        nopump_times_days=nopump_times, nopump_heads=nopump_heads,
    )


def save(cache: BaselineCache, cfg: Config, config_path: Path) -> None:
    receptors_p, drawdown_p, manifest_p, series_p, nopump_p = cache_paths(cache.key)
    receptors_p.parent.mkdir(parents=True, exist_ok=True)
    cache.receptors_df.to_parquet(receptors_p)
    cache.complex_series_df.to_parquet(series_p)
    np.savez_compressed(
        drawdown_p,
        **{f"y{y}": arr for y, arr in cache.drawdown_by_year.items()},
    )
    if cache.nopump_times_days is not None and cache.nopump_heads is not None:
        # float32 halves the file; drawdown differences at receptor scale
        # are well above float32 resolution (~1e-7 of head magnitude).
        np.savez_compressed(
            nopump_p,
            times_days=cache.nopump_times_days,
            heads=cache.nopump_heads.astype(np.float32),
        )
    manifest = {
        "key": cache.key,
        "config_path": str(config_path),
        "n_springs": int(cache.receptors_df["receptor_id"].nunique()),
        "output_years": sorted(cache.drawdown_by_year.keys()),
        "n_series_complexes": int(cache.complex_series_df["complex_id"].nunique())
            if len(cache.complex_series_df) else 0,
    }
    manifest_p.write_text(json.dumps(manifest, indent=2))

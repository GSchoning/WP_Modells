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
# fine-period stress block (fine_period_years).
CACHE_SCHEMA_VERSION = "v5"


@dataclass
class BaselineCache:
    key: str
    receptors_df: pd.DataFrame                          # tidy springs table
    drawdown_by_year: dict[float, np.ndarray]           # for raster overlays


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
        _file_sha256(Path(cfg.inputs.water_use.path)),
        f"rmult={cfg.assessment.recharge_multiplier:.6g}",
        f"chdq={','.join(cfg.assessment.chd_quadrants or [])}",
    ]
    if cfg.inputs.springs is not None and Path(cfg.inputs.springs).exists():
        parts.append(_file_sha256(Path(cfg.inputs.springs)))
    if Path(cfg.inputs.outcrop).exists():
        parts.append(_file_sha256(Path(cfg.inputs.outcrop)))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def cache_paths(key: str) -> tuple[Path, Path, Path]:
    base = CACHE_DIR / key
    return base / "receptors.parquet", base / "drawdown_by_year.npz", base / "manifest.json"


def load(key: str) -> BaselineCache | None:
    receptors_p, drawdown_p, manifest_p = cache_paths(key)
    if not (receptors_p.exists() and drawdown_p.exists() and manifest_p.exists()):
        return None
    receptors = pd.read_parquet(receptors_p)
    npz = np.load(drawdown_p)
    drawdown = {float(name.removeprefix("y")): npz[name] for name in npz.files}
    return BaselineCache(key=key, receptors_df=receptors, drawdown_by_year=drawdown)


def save(cache: BaselineCache, cfg: Config, config_path: Path) -> None:
    receptors_p, drawdown_p, manifest_p = cache_paths(cache.key)
    receptors_p.parent.mkdir(parents=True, exist_ok=True)
    cache.receptors_df.to_parquet(receptors_p)
    np.savez_compressed(
        drawdown_p,
        **{f"y{y}": arr for y, arr in cache.drawdown_by_year.items()},
    )
    manifest = {
        "key": cache.key,
        "config_path": str(config_path),
        "n_springs": int(cache.receptors_df["receptor_id"].nunique()),
        "output_years": sorted(cache.drawdown_by_year.keys()),
    }
    manifest_p.write_text(json.dumps(manifest, indent=2))

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
from datetime import datetime, timezone
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
# with calibrated pilot heads. v9 = cached licensed-take layer
# (s_licensed) alongside the Scenario A baseline. v10 = drawdown sampled
# at receptor water bores (bores_df / licensed_bores_df) cached alongside
# the spring-complex tables. v11 = real-DRN transients (drn transient
# mode): baselines run with head-dependent drains + capture accounting.
CACHE_SCHEMA_VERSION = "v11"


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
    # Licensed-take layer: Scenario A over the entitlement (auth + non-S&D)
    # subset. Invariant like A, so cached alongside it. Older caches lack
    # these — load() leaves them None and the API degrades gracefully.
    licensed_receptors_df: pd.DataFrame | None = None
    licensed_drawdown_by_year: dict[float, np.ndarray] | None = None
    # Drawdown at receptor water bores for A and L (tidy receptor_id /
    # time_years / drawdown_m), same shape as the spring tables.
    bores_df: pd.DataFrame | None = None
    licensed_bores_df: pd.DataFrame | None = None
    # DRN-mode capture accounting for the A baseline (persisted in the
    # manifest): existing take alone dries this many drains / captures this
    # much discharge. Per-request B runs subtract these to report the
    # proposal's marginal capture.
    a_n_drains_dried: int | None = None
    a_drain_capture_m3d: float | None = None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Source files whose changes alter the numbers a baseline produces. Their
# combined hash is part of the cache key, so a code change invalidates the
# cache automatically — CACHE_SCHEMA_VERSION alone relies on a human
# remembering to bump it, and the RCHA / island-anchor / conductance-clamp
# commits all changed the model under an unchanged key (only startup
# crashes prevented a stale baseline from being served).
_MODEL_SOURCE_FILES = (
    "config.py", "grid.py", "io_layer.py", "model_builder.py",
    "scenarios.py", "drains.py",
)


def _code_fingerprint() -> str:
    src_dir = Path(__file__).resolve().parents[1]     # src/
    h = hashlib.sha256()
    for name in _MODEL_SOURCE_FILES:
        p = src_dir / name
        if p.exists():
            h.update(name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def baseline_key(cfg: Config, config_path: Path) -> str:
    """Hash of every input that affects Scenario A's cached output."""
    parts = [
        CACHE_SCHEMA_VERSION,
        _code_fingerprint(),
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
        f"ostor={cfg.assessment.outcrop_storage}",
        f"rfall={cfg.inputs.recharge_fallback_m_per_day}",
        f"chdq={','.join(cfg.assessment.chd_quadrants or [])}",
        f"lic={sorted((cfg.inputs.water_use.licensed_filter or {}).items())}",
        f"dtm={cfg.drains.transient_mode}",
        f"stor={cfg.assessment.storage_mode}",
    ]
    # Vertical leakage changes every number a baseline produces: key on
    # the knobs AND the source-head file content.
    if cfg.leakage.enabled:
        parts.append(f"leak={cfg.leakage.kv_over_b_per_day}"
                     f"x{cfg.leakage.conductance_scale:.6g}|{cfg.leakage.head_col}")
        if cfg.leakage.source_heads_csv and Path(cfg.leakage.source_heads_csv).exists():
            parts.append(_file_sha256(Path(cfg.leakage.source_heads_csv)))
        if cfg.leakage.conductance_csv and Path(cfg.leakage.conductance_csv).exists():
            parts.append("leak-cond")
            parts.append(_file_sha256(Path(cfg.leakage.conductance_csv)))
    if cfg.inputs.springs is not None and Path(cfg.inputs.springs).exists():
        parts.append(_file_sha256(Path(cfg.inputs.springs)))
    if cfg.inputs.springs_attr_filter:
        parts.append(f"spfilt={sorted(cfg.inputs.springs_attr_filter.items())}")
    # Boundary GHBs read from the calibrated parent-model export when set.
    if cfg.inputs.ghb_cells_csv is not None and Path(cfg.inputs.ghb_cells_csv).exists():
        parts.append("ghb-file")
        parts.append(_file_sha256(Path(cfg.inputs.ghb_cells_csv)))
    if Path(cfg.inputs.outcrop).exists():
        parts.append(_file_sha256(Path(cfg.inputs.outcrop)))
    # Rejected-recharge drains derive elevations/conductances from the
    # parent-model RIV export (preferred) or the DEM, so whichever source
    # is active becomes baseline-relevant once drains are enabled. Mirrors
    # the priority in drains.drain_cells_for_config.
    if cfg.drains.enabled:
        riv = cfg.drains.riv_cells_csv
        if riv is not None and Path(riv).exists():
            parts.append("drains-riv")
            parts.append(_file_sha256(Path(riv)))
        elif cfg.inputs.dem is not None and Path(cfg.inputs.dem).exists():
            parts.append("drains-dem")
            parts.append(_file_sha256(Path(cfg.inputs.dem)))
        parts.append(f"dcond={cfg.drains.conductance_m2_per_day}")
        parts.append(f"dscale={cfg.drains.conductance_scale:.6g}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def cache_paths(key: str) -> tuple[Path, ...]:
    base = CACHE_DIR / key
    return (
        base / "receptors.parquet",
        base / "drawdown_by_year.npz",
        base / "manifest.json",
        base / "complex_series.parquet",
        base / "nopump.npz",
        base / "licensed_receptors.parquet",
        base / "licensed_drawdown_by_year.npz",
        base / "bores.parquet",
        base / "licensed_bores.parquet",
    )


def exists(key: str) -> bool:
    """Cheap on-disk check (no parquet/npz loading) that a complete
    baseline is cached under `key` — the same four files load() requires."""
    receptors_p, drawdown_p, manifest_p, series_p, *_rest = cache_paths(key)
    return (receptors_p.exists() and drawdown_p.exists()
            and manifest_p.exists() and series_p.exists())


def load(key: str) -> BaselineCache | None:
    (receptors_p, drawdown_p, manifest_p, series_p, nopump_p,
     lic_receptors_p, lic_drawdown_p, bores_p, lic_bores_p) = cache_paths(key)
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
    licensed_receptors = None
    if lic_receptors_p.exists():
        licensed_receptors = pd.read_parquet(lic_receptors_p)
    licensed_drawdown = None
    if lic_drawdown_p.exists():
        lz = np.load(lic_drawdown_p)
        licensed_drawdown = {float(name.removeprefix("y")): lz[name] for name in lz.files}
    bores = pd.read_parquet(bores_p) if bores_p.exists() else None
    licensed_bores = pd.read_parquet(lic_bores_p) if lic_bores_p.exists() else None
    manifest = json.loads(manifest_p.read_text())
    return BaselineCache(
        key=key, receptors_df=receptors, drawdown_by_year=drawdown,
        complex_series_df=series,
        nopump_times_days=nopump_times, nopump_heads=nopump_heads,
        licensed_receptors_df=licensed_receptors,
        licensed_drawdown_by_year=licensed_drawdown,
        bores_df=bores, licensed_bores_df=licensed_bores,
        a_n_drains_dried=manifest.get("a_n_drains_dried"),
        a_drain_capture_m3d=manifest.get("a_drain_capture_m3d"),
    )


def save(cache: BaselineCache, cfg: Config, config_path: Path) -> None:
    (receptors_p, drawdown_p, manifest_p, series_p, nopump_p,
     lic_receptors_p, lic_drawdown_p, bores_p, lic_bores_p) = cache_paths(cache.key)
    receptors_p.parent.mkdir(parents=True, exist_ok=True)
    cache.receptors_df.to_parquet(receptors_p)
    cache.complex_series_df.to_parquet(series_p)
    np.savez_compressed(
        drawdown_p,
        **{f"y{y}": arr for y, arr in cache.drawdown_by_year.items()},
    )
    if cache.licensed_receptors_df is not None:
        cache.licensed_receptors_df.to_parquet(lic_receptors_p)
    if cache.licensed_drawdown_by_year is not None:
        np.savez_compressed(
            lic_drawdown_p,
            **{f"y{y}": arr for y, arr in cache.licensed_drawdown_by_year.items()},
        )
    if cache.bores_df is not None:
        cache.bores_df.to_parquet(bores_p)
    if cache.licensed_bores_df is not None:
        cache.licensed_bores_df.to_parquet(lic_bores_p)
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
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_rev": _git_rev(),
        "code_fingerprint": _code_fingerprint(),
        "cache_schema": CACHE_SCHEMA_VERSION,
        "n_springs": int(cache.receptors_df["receptor_id"].nunique()),
        "output_years": sorted(cache.drawdown_by_year.keys()),
        "n_series_complexes": int(cache.complex_series_df["complex_id"].nunique())
            if len(cache.complex_series_df) else 0,
        "a_n_drains_dried": cache.a_n_drains_dried,
        "a_drain_capture_m3d": cache.a_drain_capture_m3d,
    }
    manifest_p.write_text(json.dumps(manifest, indent=2))


def _git_rev() -> str:
    import subprocess
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except OSError:
        return "unknown"

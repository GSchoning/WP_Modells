"""Markdown + JSON report generation (CLAUDE.md §6.6)."""
from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .grid import Grid
from .io_layer import Inputs, ML_PER_YEAR_TO_M3_PER_DAY


def write_validation_report(findings: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Input validation report", ""]
    if not findings:
        lines.append("All checks passed.")
    else:
        for f in findings:
            lines.append(f"- {f}")
    path.write_text("\n".join(lines) + "\n")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {"python": platform.python_version()}
    for mod in ("flopy", "numpy", "pandas", "geopandas", "rasterio", "shapely", "pyproj"):
        try:
            m = __import__(mod)
            versions[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            versions[mod] = "not installed"
    return versions


def _mf6_version() -> str:
    import shutil
    import subprocess
    if shutil.which("mf6") is None:
        return "mf6 not on PATH"
    try:
        out = subprocess.run(["mf6", "-v"], capture_output=True, text=True, timeout=5)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:
        return f"mf6 -v failed: {exc}"


def _top_n_table(combined: pd.DataFrame, time_years: float, n: int) -> pd.DataFrame:
    sub = combined[combined["time_years"] == time_years].copy()
    sub = sub.sort_values("s_total", ascending=False).head(n)
    return sub[["receptor_id", "s_approved", "s_additional", "s_total"]]


def _df_to_md(df: pd.DataFrame, float_cols: list[str], precision: int = 2) -> str:
    if df.empty:
        return "_(no rows)_"
    df = df.copy()
    for c in float_cols:
        df[c] = df[c].map(lambda v: f"{v:.{precision}f}")
    headers = list(df.columns)
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in headers) + " |")
    return "\n".join(lines)


def write_impact_report(
    *,
    cfg: Config,
    grid: Grid,
    inputs: Inputs,
    results: dict,
    combined: pd.DataFrame,
    config_path: Path,
    md_path: Path,
    json_path: Path,
    top_n: int = 10,
) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    output_years = sorted(combined["time_years"].unique().tolist())
    total_m3d = float(inputs.pumping_bores["rate_m3_per_day"].sum())
    total_ml_yr = total_m3d / ML_PER_YEAR_TO_M3_PER_DAY
    pb = cfg.inputs.proposed_bore
    pb_m3d = (pb.rate_ML_per_year * ML_PER_YEAR_TO_M3_PER_DAY) if pb.rate_ML_per_year else None

    active = grid.idomain[0] == 1
    k_active = grid.k[0][active]
    k_pcts = {p: float(np.percentile(k_active, p)) for p in (1, 5, 50, 95, 99)}

    metadata = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_file": str(config_path),
        "config_sha256_16": _file_sha256(Path(config_path)),
        "properties_csv_sha256_16": _file_sha256(Path(cfg.inputs.properties_csv)),
        "mf6_version": _mf6_version(),
        "package_versions": _package_versions(),
    }

    # Markdown report.
    lines: list[str] = []
    lines.append("# Precipice Sandstone — water licence impact assessment")
    lines.append("")
    lines.append(f"_{metadata['timestamp_utc']}_")
    lines.append("")
    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- config: `{metadata['config_file']}` (sha256[:16] = `{metadata['config_sha256_16']}`)")
    lines.append(f"- properties CSV sha256[:16] = `{metadata['properties_csv_sha256_16']}`")
    lines.append(f"- MF6: `{metadata['mf6_version']}`")
    lines.append(f"- package versions: " + ", ".join(
        f"{k}={v}" for k, v in metadata["package_versions"].items()
    ))
    lines.append("")

    lines.append("## Scenario inputs")
    lines.append("")
    lines.append(f"- Existing licensed bores (Scenario A): **{len(inputs.pumping_bores)}** bores, "
                 f"total **{total_ml_yr:,.0f} ML/yr** (= {total_m3d:,.0f} m³/d)")
    if pb.rate_ML_per_year is not None:
        lines.append(f"- Proposed bore (Scenario C): `{pb.bore_id}` at "
                     f"({pb.x:,.0f}, {pb.y:,.0f}), **{pb.rate_ML_per_year:,.1f} ML/yr** "
                     f"(= {pb_m3d:,.0f} m³/d)")
    else:
        lines.append("- Proposed bore (Scenario C): _not set_")
    n_springs_assessed = combined["receptor_id"].nunique() if len(combined) else 0
    lines.append(f"- Springs assessed: **{n_springs_assessed}** (within "
                 f"1 km of outcrop)")
    lines.append(f"- Output years: {', '.join(f'{y:.0f}' for y in output_years)}")
    lines.append("")

    lines.append("## Grid + properties summary")
    lines.append("")
    lines.append(f"- grid: {grid.nlay} × {grid.nrow} × {grid.ncol}, "
                 f"dx = {grid.delr[0]:.0f} m, dy = {grid.delc[0]:.0f} m")
    lines.append(f"- active cells (IBOUND=1): {int((grid.idomain == 1).sum()):,}")
    lines.append(f"- K (m/d) percentiles: " + ", ".join(
        f"p{p} = {v:.3g}" for p, v in k_pcts.items()
    ))
    lines.append("")

    for y in output_years:
        lines.append(f"## Top {top_n} most-impacted springs at t = {y:.0f} yr")
        lines.append("")
        top_df = _top_n_table(combined, y, top_n)
        top_df = top_df.rename(columns={
            "receptor_id": "spring_id",
            "s_approved": "s_approved (m)",
            "s_additional": "s_additional (m)",
            "s_total": "s_total (m)",
        })
        lines.append(_df_to_md(top_df, ["s_approved (m)", "s_additional (m)", "s_total (m)"]))
        lines.append("")

    fig_dir = Path("reports/figures")
    if fig_dir.exists():
        figs = sorted(fig_dir.glob("*.png"))
        if figs:
            lines.append("## Figures")
            lines.append("")
            for p in figs:
                rel = p.relative_to(md_path.parent) if p.is_relative_to(md_path.parent) else p
                lines.append(f"- `{rel}`")
            lines.append("")

    lines.append("## Limitations")
    lines.append("")
    lines.append("- Single confined layer: no leakage from over-/underlying formations, "
                 "so all extraction is drawn from one layer's storage. Real Precipice leaks "
                 "into the Hutton above and Bandanna/Rewan below — distributed leakage reduces "
                 "drawdown materially.")
    lines.append("- Confined-everywhere: outcrop cells use confined storage (Ss × b ≈ 1e-3) "
                 "rather than specific yield (Sy ≈ 0.1) needed to preserve linearity for "
                 "superposition. This inflates magnitudes in outcrop areas.")
    lines.append("- Pumping wells are treated as point sinks within finite cells. Drawdown "
                 "in the well cell itself and within ~2 cells is mesh-dependent; receptor "
                 "drawdown more than 2 cells away is reliable.")
    lines.append("- Boundary CHD pins head at the active-domain edge to NTOP, which provides "
                 "a regional sink for recharge. If the modelled drawdown reaches the boundary "
                 "ring at 100 yr, the boundary is too close and is suppressing drawdown.")
    lines.append("")

    md_path.write_text("\n".join(lines) + "\n")

    # JSON bundle (Phase 2 API response shape).
    receptors_payload: list[dict] = []
    for rid, group in combined.groupby("receptor_id"):
        per_year: dict[str, dict[str, float]] = {}
        for _, row in group.iterrows():
            per_year[f"{row['time_years']:.0f}"] = {
                "s_approved_m": float(row["s_approved"]),
                "s_additional_m": float(row["s_additional"]),
                "s_total_m": float(row["s_total"]),
            }
        receptors_payload.append({"id": str(rid), "drawdown_by_year": per_year})

    bundle = {
        "metadata": metadata,
        "scenarios": {
            "A": {
                "n_bores": int(len(inputs.pumping_bores)),
                "total_pumping_ml_yr": total_ml_yr,
                "total_pumping_m3_per_day": total_m3d,
            },
            "C": {
                "bore_id": pb.bore_id,
                "x": pb.x,
                "y": pb.y,
                "rate_ml_yr": pb.rate_ML_per_year,
                "rate_m3_per_day": pb_m3d,
            },
        },
        "output_years": [float(y) for y in output_years],
        "springs": receptors_payload,
    }
    json_path.write_text(json.dumps(bundle, indent=2))

"""Diagnostic figures for the Precipice POC.

Produces PNGs in reports/figures/ that help eyeball:
  - the active model domain + receptors + bores
  - K heterogeneity across the formation
  - drawdown rasters for each scenario at the headline time slices
  - top-impacted springs as a stacked bar chart (s_approved + s_additional)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import Config
from .grid import Grid
from .io_layer import Inputs


def _extent(grid: Grid) -> list[float]:
    return [
        grid.xorigin,
        grid.xorigin + grid.delr.sum(),
        grid.yorigin,
        grid.yorigin + grid.delc.sum(),
    ]


def _mask_inactive(arr2d: np.ndarray, grid: Grid) -> np.ndarray:
    return np.where(grid.idomain[0] == 1, arr2d, np.nan)


def _km_axes(ax) -> None:
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1000:.0f}"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y/1000:.0f}"))
    ax.set_xlabel("Easting (km, MGA Z55)")
    ax.set_ylabel("Northing (km, MGA Z55)")
    ax.set_aspect("equal")


def domain_overview(grid: Grid, inputs: Inputs, cfg: Config, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 11))
    extent = _extent(grid)

    # Active cells (light grey) + outcrop (green overlay).
    active = np.where(grid.idomain[0] == 1, 1.0, np.nan)
    ax.imshow(active, extent=extent, origin="upper", cmap="Greys", alpha=0.35, vmin=0, vmax=2)
    outcrop = np.where(grid.outcrop_mask, 1.0, np.nan)
    ax.imshow(outcrop, extent=extent, origin="upper", cmap="Greens", alpha=0.55, vmin=0, vmax=2)

    if len(inputs.pumping_bores):
        ax.scatter(
            inputs.pumping_bores.geometry.x,
            inputs.pumping_bores.geometry.y,
            s=3, c="red", alpha=0.5,
            label=f"pumping bores ({len(inputs.pumping_bores)})",
        )
    if inputs.springs is not None and len(inputs.springs):
        ax.scatter(
            inputs.springs.geometry.x,
            inputs.springs.geometry.y,
            s=12, c="dodgerblue", marker="^", edgecolor="white", linewidth=0.4,
            label=f"springs ({len(inputs.springs)})",
        )
    pb = cfg.inputs.proposed_bore
    if pb.x is not None and pb.y is not None:
        ax.scatter(
            [pb.x], [pb.y], s=300, c="gold", marker="*",
            edgecolor="black", linewidth=1.2,
            label=f"proposed bore ({pb.bore_id})",
        )

    ax.set_title("Precipice Sandstone — model domain overview\n"
                 "grey = active cells, green = outcrop")
    ax.legend(loc="lower left", framealpha=0.9)
    _km_axes(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def k_map(grid: Grid, out_path: Path) -> None:
    k = _mask_inactive(grid.k[0], grid)
    fig, ax = plt.subplots(figsize=(9, 11))
    img = ax.imshow(
        k, extent=_extent(grid), origin="upper",
        cmap="viridis", norm=mcolors.LogNorm(vmin=np.nanpercentile(k, 1), vmax=np.nanpercentile(k, 99)),
    )
    cbar = plt.colorbar(img, ax=ax, fraction=0.04, pad=0.02)
    cbar.set_label("Hydraulic conductivity K (m/d, log scale)")
    ax.set_title("Precipice K heterogeneity (active cells, log scale)")
    _km_axes(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def drawdown_map(
    drawdown_2d: np.ndarray,
    grid: Grid,
    title: str,
    out_path: Path,
    *,
    inputs: Inputs | None = None,
    cfg: Config | None = None,
    clip_pct: float = 99.0,
) -> None:
    """Plot a single drawdown grid (positive = head dropped)."""
    s = _mask_inactive(drawdown_2d, grid)
    vmax = float(np.nanpercentile(np.abs(s), clip_pct)) or 1.0
    fig, ax = plt.subplots(figsize=(9, 11))
    img = ax.imshow(
        s, extent=_extent(grid), origin="upper",
        cmap="RdBu_r", vmin=-vmax, vmax=vmax,
    )
    cbar = plt.colorbar(img, ax=ax, fraction=0.04, pad=0.02)
    cbar.set_label(f"Drawdown (m); colour clipped at {clip_pct:.0f}th pct = ±{vmax:.0f} m")
    if inputs is not None and inputs.springs is not None:
        ax.scatter(
            inputs.springs.geometry.x, inputs.springs.geometry.y,
            s=8, c="black", marker="^", alpha=0.6, label="springs",
        )
    if cfg is not None and cfg.inputs.proposed_bore.x is not None:
        ax.scatter(
            [cfg.inputs.proposed_bore.x], [cfg.inputs.proposed_bore.y],
            s=200, c="gold", marker="*", edgecolor="black", linewidth=1.2,
            label="proposed bore",
        )
    ax.set_title(title)
    if inputs is not None and (inputs.springs is not None or (cfg and cfg.inputs.proposed_bore.x)):
        ax.legend(loc="lower left", framealpha=0.9)
    _km_axes(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def top_springs_bar(
    receptors_a: pd.DataFrame,
    receptors_c: pd.DataFrame,
    time_years: float,
    out_path: Path,
    *,
    top_n: int = 20,
) -> None:
    a = receptors_a.query("time_years == @time_years").set_index("receptor_id")["drawdown_m"]
    c = receptors_c.query("time_years == @time_years").set_index("receptor_id")["drawdown_m"]
    common = a.index.intersection(c.index)
    df = pd.DataFrame({"s_approved": a.loc[common], "s_additional": c.loc[common]})
    df["s_total"] = df["s_approved"] + df["s_additional"]
    df = df.sort_values("s_total", ascending=False).head(top_n)

    fig, ax = plt.subplots(figsize=(11, 6))
    x = np.arange(len(df))
    ax.bar(x, df["s_approved"], color="steelblue", label="s_approved (Scenario A)")
    ax.bar(x, df["s_additional"], bottom=df["s_approved"],
           color="darkorange", label="s_additional (Scenario C)")
    ax.set_xticks(x)
    ax.set_xticklabels(df.index, rotation=60, ha="right", fontsize=8)
    ax.set_ylabel("Drawdown (m)")
    ax.set_title(f"Top {top_n} most-impacted springs at t = {time_years:.0f} yr "
                 f"(stacked: existing + proposed)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def make_all(
    grid: Grid,
    inputs: Inputs,
    cfg: Config,
    results: dict,                    # {scenario: ScenarioResult}
    out_dir: Path,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    p = out_dir / "01_domain_overview.png"
    domain_overview(grid, inputs, cfg, p); written.append(p)

    p = out_dir / "02_K_heterogeneity.png"
    k_map(grid, p); written.append(p)

    for scen, res in results.items():
        for y, arr in res.drawdown_at_output_years.items():
            p = out_dir / f"03_drawdown_{scen}_{int(y)}yr.png"
            drawdown_map(
                arr, grid,
                title=f"Scenario {scen} — drawdown at {int(y)} yr",
                out_path=p, inputs=inputs, cfg=cfg,
            )
            written.append(p)

    if "A" in results and "C" in results:
        p = out_dir / "04_top_springs_100yr.png"
        top_springs_bar(
            results["A"].receptors_df, results["C"].receptors_df,
            time_years=100.0, out_path=p,
        )
        written.append(p)

    return written

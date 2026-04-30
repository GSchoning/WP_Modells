"""Command-line entry point (CLAUDE.md §7).

Usage:
    python -m src.cli validate --config config.yaml
    python -m src.cli run      --config config.yaml
    python -m src.cli theis                            # synthetic Theis sanity check
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import typer

from .config import load_config
from .figures import make_all as make_figures
from .grid import build_grid_from_properties
from .io_layer import load_inputs, validate
from .model_builder import active_boundary_chd_cells
from .reporting import write_validation_report
from .scenarios import run_scenario, run_steady_state
from .superposition import combine_rasters, combine_receptor_tables

app = typer.Typer(add_completion=False, help="Precipice POC pipeline")


def _print_grid_summary(grid):
    n_active = int((grid.idomain == 1).sum())
    typer.echo(f"  grid: {grid.nlay} × {grid.nrow} × {grid.ncol}, "
               f"dx={grid.delr[0]:.0f} m, dy={grid.delc[0]:.0f} m")
    typer.echo(f"  active cells (IBOUND=1): {n_active}")
    typer.echo(f"  domain bounds (project CRS): "
               f"X {grid.xorigin:.0f}–{grid.xorigin + grid.delr.sum():.0f}, "
               f"Y {grid.yorigin:.0f}–{grid.yorigin + grid.delc.sum():.0f}")


@app.command("validate")
def validate_cmd(config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Load + validate inputs; write reports/validation.md."""
    cfg = load_config(config)
    inputs = load_inputs(cfg)
    grid = build_grid_from_properties(
        inputs.properties, cfg.project.crs, layer=cfg.grid.properties_layer
    )
    findings = validate(inputs, cfg, grid)
    out = Path("reports/validation.md")
    write_validation_report(findings, out)
    typer.echo(f"Validation report → {out}")
    for f in findings:
        typer.echo(f"  - {f}")


@app.command()
def run(
    config: Path = typer.Option("config.yaml", "--config", "-c"),
    skip_scenarios: bool = typer.Option(False, "--skip-scenarios",
                                        help="Run ingest + grid + validate only."),
    figures: bool = typer.Option(True, "--figures/--no-figures",
                                 help="Write diagnostic PNGs to reports/figures/."),
    proposed_x: float = typer.Option(None, "--proposed-x"),
    proposed_y: float = typer.Option(None, "--proposed-y"),
    proposed_rate: float = typer.Option(None, "--proposed-rate",
                                        help="Proposed bore extraction rate (m³/d)."),
):
    """Run the full pipeline: ingest → grid → scenarios A & C → superposition → report."""
    cfg = load_config(config)
    if proposed_x is not None:
        cfg.inputs.proposed_bore.x = proposed_x
    if proposed_y is not None:
        cfg.inputs.proposed_bore.y = proposed_y
    if proposed_rate is not None:
        cfg.inputs.proposed_bore.rate_m3_per_day = proposed_rate

    typer.echo(f"Loading inputs (project CRS: {cfg.project.crs})…")
    inputs = load_inputs(cfg)

    typer.echo(f"Building grid from properties.csv (ILAY={cfg.grid.properties_layer})…")
    grid = build_grid_from_properties(
        inputs.properties, cfg.project.crs, layer=cfg.grid.properties_layer
    )
    _print_grid_summary(grid)

    findings = validate(inputs, cfg, grid)
    write_validation_report(findings, Path("reports/validation.md"))
    if findings:
        typer.echo("Validation findings (see reports/validation.md):")
        for f in findings:
            typer.echo(f"  - {f}")

    typer.echo(f"  pumping bores: {len(inputs.pumping_bores)}")
    typer.echo(f"  receptor bores: {len(inputs.receptor_bores)}")
    typer.echo(f"  springs: {0 if inputs.springs is None else len(inputs.springs)}")

    if skip_scenarios:
        typer.echo("\nSkipping scenario execution (--skip-scenarios).")
        return

    workspace_root = Path(cfg.run.workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Far-field CHD on the active-domain boundary, head = NTOP. Provides a
    # sink for outcrop recharge so the steady-state pre-run converges, and
    # is reused unchanged in the transient runs so its contribution cancels
    # in drawdown = h_initial − h(t). Same CHD across A and C → superposition
    # holds.
    chd_cells = active_boundary_chd_cells(grid)
    typer.echo(f"\nBoundary CHD on {len(chd_cells)} active-edge cells (head = NTOP).")

    typer.echo("Running steady-state pre-run (no pumping, recharge on)…")
    try:
        ic_head = run_steady_state(cfg, grid, workspace_root / "ss", chd_cells=chd_cells)
    except RuntimeError as exc:
        typer.echo(f"  steady-state failed: {exc}")
        # Uniform IC fallback. A spatially-varying grid.top is a non-
        # equilibrium field — the transient solver would diffuse it toward
        # steady state and the relaxation would contaminate drawdown
        # = h_initial − h(t) with a domain-wide pattern unrelated to the
        # well. Uniform IC means h(t) = h_initial in the absence of
        # forcing, so drawdown isolates the well response.
        active = grid.idomain[0] == 1
        mean_top = float(np.nanmean(np.where(active, grid.top, np.nan)))
        typer.echo(f"  Falling back to uniform initial head = {mean_top:.1f} m (mean of active top).")
        ic_head = np.full_like(grid.top, mean_top)

    results = {}
    for scen in cfg.run.scenarios:
        typer.echo(f"\nRunning Scenario {scen}…")
        try:
            results[scen] = run_scenario(
                cfg, grid, inputs, scen, ic_head, workspace_root / f"scen_{scen}",
                chd_cells=chd_cells,
            )
            typer.echo(f"  done; {len(results[scen].times_days)} time steps saved.")
            recv_csv = out_dir / f"scenario_{scen}_springs.csv"
            results[scen].receptors_df.to_csv(recv_csv, index=False)
            n_springs = results[scen].receptors_df["receptor_id"].nunique() if len(results[scen].receptors_df) else 0
            typer.echo(f"  springs sampled: {n_springs} → {recv_csv}")
        except (RuntimeError, ValueError) as exc:
            typer.echo(f"  Scenario {scen} skipped: {exc}")

    if "A" in results and "C" in results:
        typer.echo("\nCombining via superposition (B = A + C)…")
        combined = combine_receptor_tables(
            results["A"].receptors_df,
            results["C"].receptors_df,
        )
        out_csv = out_dir / "receptors_combined.csv"
        combined.to_csv(out_csv, index=False)
        typer.echo(f"  combined receptor table → {out_csv}")

    if figures and results:
        typer.echo("\nWriting diagnostic figures…")
        fig_dir = Path("reports/figures")
        written = make_figures(grid, inputs, cfg, results, fig_dir)
        for p in written:
            typer.echo(f"  → {p}")


@app.command()
def theis():
    """Run the synthetic Theis sanity check (no real data needed)."""
    import shutil
    if shutil.which("mf6") is None:
        typer.echo("mf6 binary not on PATH; install via "
                   "`python -m flopy.utils.get_modflow $HOME/.local/bin --subset mf6`")
        raise typer.Exit(code=2)
    import pytest
    raise typer.Exit(code=pytest.main(["-q", "tests/test_theis.py"]))


if __name__ == "__main__":
    app()

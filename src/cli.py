"""Command-line entry point (CLAUDE.md §7).

Usage:
    python -m src.cli run --config config.yaml
    python -m src.cli validate --config config.yaml
"""
from __future__ import annotations

from pathlib import Path

import typer

from .config import load_config
from .io_layer import load_inputs, validate
from .grid import build_grid_from_properties
from .reporting import write_validation_report

app = typer.Typer(add_completion=False, help="Precipice POC pipeline")


@app.command("validate")
def validate_cmd(config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Load + validate inputs; write reports/validation.md."""
    cfg = load_config(config)
    inputs = load_inputs(cfg)
    grid = build_grid_from_properties(inputs.properties, cfg.project.crs)
    findings = validate(inputs, cfg, grid)
    out = Path("reports/validation.md")
    write_validation_report(findings, out)
    typer.echo(f"Validation report → {out}")
    for f in findings:
        typer.echo(f"  - {f}")


@app.command()
def run(config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Run the full pipeline: ingest → grid → scenarios A & C → superposition → report."""
    cfg = load_config(config)
    typer.echo(f"Loading inputs (project CRS: {cfg.project.crs})…")
    inputs = load_inputs(cfg)

    typer.echo("Building grid from properties.csv…")
    grid = build_grid_from_properties(inputs.properties, cfg.project.crs)
    typer.echo(f"  grid: {grid.nlay} × {grid.nrow} × {grid.ncol}, "
               f"dx={grid.delr[0]:.0f} m, dy={grid.delc[0]:.0f} m")
    n_active = int((grid.idomain == 1).sum())
    typer.echo(f"  active cells (IBOUND=1): {n_active}")
    typer.echo(f"  domain bounds (project CRS): "
               f"X {grid.xorigin:.0f}–{grid.xorigin + grid.delr.sum():.0f}, "
               f"Y {grid.yorigin:.0f}–{grid.yorigin + grid.delc.sum():.0f}")

    findings = validate(inputs, cfg, grid)
    write_validation_report(findings, Path("reports/validation.md"))
    if findings:
        typer.echo("Validation findings (see reports/validation.md):")
        for f in findings:
            typer.echo(f"  - {f}")

    typer.echo(f"  pumping bores: {len(inputs.pumping_bores)}")
    typer.echo(f"  receptor bores: {len(inputs.receptor_bores)}")
    typer.echo(f"  springs: {0 if inputs.springs is None else len(inputs.springs)}")

    typer.echo(
        "\nScenario execution (build_scenario) is not yet wired up — see "
        "src/model_builder.py. Codespace setup is complete: environment, "
        "ingest, grid construction, and validation are working."
    )


# Allow `python -m src.cli` to invoke the app directly.
if __name__ == "__main__":
    app()

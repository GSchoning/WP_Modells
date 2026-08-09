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
from .io_layer import load_inputs, validate, load_recharge_by_inode, ML_PER_YEAR_TO_M3_PER_DAY
from .model_builder import active_boundary_chd_cells
from .reporting import write_impact_report, write_validation_report
from .scenarios import resolve_initial_head, run_scenario, run_steady_state
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
        inputs.properties, cfg.project.crs, layer=cfg.grid.properties_layer,
        recharge_by_inode=load_recharge_by_inode(cfg),
        recharge_fallback_m_per_day=cfg.inputs.recharge_fallback_m_per_day,
        outcrop_storage=cfg.assessment.outcrop_storage,
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
                                        help="Proposed bore extraction rate (ML/year)."),
):
    """Run the full pipeline: ingest → grid → scenarios A & C → superposition → report."""
    cfg = load_config(config)
    if proposed_x is not None:
        cfg.inputs.proposed_bore.x = proposed_x
    if proposed_y is not None:
        cfg.inputs.proposed_bore.y = proposed_y
    if proposed_rate is not None:
        cfg.inputs.proposed_bore.rate_ML_per_year = proposed_rate

    typer.echo(f"Loading inputs (project CRS: {cfg.project.crs})…")
    inputs = load_inputs(cfg)

    typer.echo(f"Building grid from properties.csv (ILAY={cfg.grid.properties_layer})…")
    grid = build_grid_from_properties(
        inputs.properties, cfg.project.crs, layer=cfg.grid.properties_layer,
        recharge_by_inode=load_recharge_by_inode(cfg),
        recharge_fallback_m_per_day=cfg.inputs.recharge_fallback_m_per_day,
        outcrop_storage=cfg.assessment.outcrop_storage,
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

    total_m3d = float(inputs.pumping_bores["rate_m3_per_day"].sum())
    total_ml_yr = total_m3d / ML_PER_YEAR_TO_M3_PER_DAY
    typer.echo(f"  total existing pumping (Scenario A): "
               f"{total_m3d:,.0f} m³/d ({total_ml_yr:,.0f} ML/yr)")
    pb = cfg.inputs.proposed_bore
    if pb.rate_ML_per_year is not None and pb.x is not None and pb.y is not None:
        pb_m3d = pb.rate_ML_per_year * ML_PER_YEAR_TO_M3_PER_DAY
        typer.echo(f"  proposed bore (Scenario C): {pb.bore_id} @ "
                   f"({pb.x:.0f}, {pb.y:.0f}), {pb.rate_ML_per_year:,.1f} ML/yr "
                   f"({pb_m3d:,.0f} m³/d)")

    active = grid.idomain[0] == 1
    k_active = grid.k[0][active]
    pcts = [1, 5, 50, 95, 99]
    k_pcts = np.percentile(k_active, pcts)
    typer.echo("  K (m/d) over active cells: "
               + ", ".join(f"p{p}={v:.3g}" for p, v in zip(pcts, k_pcts)))

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
    # holds. Outcrop cells are excluded from the CHD set: the outcrop edge
    # is a recharge inflow, not a regional discharge — pinning heads there
    # would suppress the recharge response.
    mode = cfg.assessment.boundary_mode
    boundary_ghb = []
    if mode == "uwir_ghb":
        chd_cells = []
        from .model_builder import boundary_ghb_for_config
        boundary_ghb, ghb_source = boundary_ghb_for_config(cfg, grid)
        typer.echo(f"\nBoundary: no-flow pinch-outs + {len(boundary_ghb)} truncation-face GHBs "
                   f"({ghb_source}) — UWIR 2025 design.")
    elif mode == "no_flow":
        chd_cells = []
        typer.echo("\nBoundary: no-flow perimeter (pinch-out) — drains provide the steady-state outlet.")
    elif mode == "chd_quadrants":
        chd_cells = active_boundary_chd_cells(
            grid, exclude_mask=grid.outcrop_mask,
            quadrants=cfg.assessment.chd_quadrants,
        )
        typer.echo(f"\nBoundary CHD on {len(chd_cells)} active-edge cells (head = NTOP, outcrop excluded).")
    else:  # chd_all
        chd_cells = active_boundary_chd_cells(grid)
        typer.echo(f"\nBoundary CHD on all {len(chd_cells)} active-edge cells (head = NTOP).")

    # Rejected-recharge drains: parent-model RIV export when configured,
    # else min-DEM elevation in outcrop cells.
    drn_cells = []
    if cfg.drains.enabled:
        from .drains import drain_cells_for_config
        try:
            drn_cells, drn_source = drain_cells_for_config(cfg, grid)
            typer.echo(f"Rejected-recharge drains on {len(drn_cells)} cells ({drn_source}).")
        except Exception as exc:
            typer.echo(f"  drains disabled — failed to build: {exc}")

    # Quasi-3D vertical leakage (Hantush): per-cell GHBs riding in the
    # steady state and both transient twins. Disabled → [].
    from .leakage import leakage_ghb_cells
    leak, leak_desc = leakage_ghb_cells(cfg, grid)
    if leak:
        typer.echo(f"Vertical leakage: {leak_desc}.")

    # Weak anchors for active islands that carry no BC — without them the
    # closed-perimeter steady state is singular (see anchor_ghb_cells).
    from .model_builder import anchor_ghb_cells
    bc_cells = {(rec[1], rec[2]) for rec in chd_cells}
    bc_cells |= {(rec[1], rec[2]) for rec in boundary_ghb}
    bc_cells |= {(rec[1], rec[2]) for rec in drn_cells}
    # Only anchor-strength leakage cells (>= 1 m²/d) count as BCs for
    # island-anchor detection — see the note in api/app.py.
    bc_cells |= {(rec[1], rec[2]) for rec in leak if rec[4] >= 1.0}
    anchors = anchor_ghb_cells(grid, bc_cells)
    if anchors:
        typer.echo(f"  {len(anchors)} weak anchor GHBs for BC-less active islands.")
    boundary_ghb = list(boundary_ghb) + anchors + leak

    typer.echo("Running steady-state pre-run (no pumping, recharge on)…")
    try:
        ic_head = run_steady_state(cfg, grid, workspace_root / "ss", chd_cells=chd_cells,
                                   drn_cells=drn_cells, ghb_cells=boundary_ghb)
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
    ic_head = resolve_initial_head(cfg, grid, ic_head)

    # Transient drain treatment per drains.transient_mode: real DRN cells
    # (default — head-dependent, shut off below elevation; the combined
    # scenario B is run directly), or the legacy linearised-GHB form.
    drn_mode = bool(drn_cells) and cfg.drains.transient_mode == "drn"
    ghb_cells = []
    if drn_cells and not drn_mode:
        from .drains import linearise_drains
        ghb_cells = linearise_drains(drn_cells, ic_head)
        typer.echo(f"  {len(ghb_cells)} of {len(drn_cells)} drains flowing at steady state → GHB.")
    if drn_mode:
        run_kwargs = dict(chd_cells=chd_cells, ghb_cells=boundary_ghb, drn_cells=drn_cells)
        typer.echo("  transient drains: real DRN (head-dependent); per-request scenario is B, "
                   "s_additional = B − A.")
    else:
        run_kwargs = dict(chd_cells=chd_cells, ghb_cells=boundary_ghb + ghb_cells,
                          drain_ghb_cells=ghb_cells)

    # In drn mode the change-set scenario is run combined (B) instead of
    # alone (C); the additional layer is derived after the loop.
    scenario_list = [("B" if s == "C" and drn_mode else s) for s in cfg.run.scenarios]

    results = {}
    nopump_twin = None
    for scen in scenario_list:
        typer.echo(f"\nRunning Scenario {scen}…")
        try:
            results[scen] = run_scenario(
                cfg, grid, inputs, scen, ic_head, workspace_root / f"scen_{scen}",
                nopump_twin=nopump_twin,
                **run_kwargs,
            )
            # The no-pump twin is scenario-independent — reuse it so the
            # remaining scenarios only run MF6 once each.
            if nopump_twin is None and results[scen].heads_nopump is not None:
                nopump_twin = (results[scen].times_days, results[scen].heads_nopump)
            typer.echo(f"  done; {len(results[scen].times_days)} time steps saved.")
            if results[scen].max_pct_discrepancy >= 1.0:
                typer.echo(f"  WARNING: mass-balance discrepancy {results[scen].max_pct_discrepancy:.2f}%")
            if results[scen].n_drain_reversals:
                typer.echo(f"  WARNING: {results[scen].n_drain_reversals} linearised drain(s) "
                           "drew below drain level — near-outcrop drawdown under-predicted")
            recv_csv = out_dir / f"scenario_{scen}_springs.csv"
            results[scen].receptors_df.to_csv(recv_csv, index=False)
            n_springs = results[scen].receptors_df["receptor_id"].nunique() if len(results[scen].receptors_df) else 0
            typer.echo(f"  springs sampled: {n_springs} → {recv_csv}")
            if results[scen].bores_df is not None and len(results[scen].bores_df):
                bores_csv = out_dir / f"scenario_{scen}_bores.csv"
                results[scen].bores_df.to_csv(bores_csv, index=False)
                n_bores = results[scen].bores_df["receptor_id"].nunique()
                typer.echo(f"  receptor bores sampled: {n_bores} → {bores_csv}")
        except (RuntimeError, ValueError) as exc:
            typer.echo(f"  Scenario {scen} skipped: {exc}")

    # drn mode: derive the additional layer C = B − A so everything
    # downstream (superposition combine, figures, report) is unchanged and
    # reproduces total = A + (B − A) = B exactly.
    if drn_mode and "B" in results and "A" in results:
        import dataclasses
        from .superposition import subtract_receptor_tables
        a_res, b_res = results["A"], results["B"]
        rasters = {y: b_res.drawdown_at_output_years[y] - a_res.drawdown_at_output_years[y]
                   for y in b_res.drawdown_at_output_years
                   if y in a_res.drawdown_at_output_years}
        series = b_res.complex_series_df
        if len(series) and len(a_res.complex_series_df):
            _a = a_res.complex_series_df.rename(columns={"drawdown_m": "_a"})
            series = series.merge(_a, on=["complex_id", "time_days"], how="left").fillna({"_a": 0.0})
            series["drawdown_m"] = series["drawdown_m"] - series["_a"]
            series = series.drop(columns=["_a"])
        results["C"] = dataclasses.replace(
            b_res,
            receptors_df=subtract_receptor_tables(b_res.receptors_df, a_res.receptors_df),
            bores_df=(subtract_receptor_tables(b_res.bores_df, a_res.bores_df)
                      if b_res.bores_df is not None and a_res.bores_df is not None
                      else b_res.bores_df),
            drawdown_at_output_years=rasters,
            complex_series_df=series,
            n_drains_dried=max(0, b_res.n_drains_dried - a_res.n_drains_dried),
            drain_capture_m3d=b_res.drain_capture_m3d - a_res.drain_capture_m3d,
        )
        cap_ml = results["C"].drain_capture_m3d * 365.25 / 1000.0
        typer.echo(f"\nDerived additional layer (B − A). Proposal dries "
                   f"{results['C'].n_drains_dried} drain cell(s), captures "
                   f"{cap_ml:,.0f} ML/yr of rejected-recharge discharge.")

    combined = None
    combined_bores = None
    if "A" in results and "C" in results:
        typer.echo("\nCombining via superposition (B = A + C)…")
        combined = combine_receptor_tables(
            results["A"].receptors_df,
            results["C"].receptors_df,
            scen_l=results["L"].receptors_df if "L" in results else None,
        )
        out_csv = out_dir / "receptors_combined.csv"
        combined.to_csv(out_csv, index=False)
        typer.echo(f"  combined receptor table → {out_csv}")
        if results["A"].bores_df is not None and results["C"].bores_df is not None:
            combined_bores = combine_receptor_tables(
                results["A"].bores_df,
                results["C"].bores_df,
                scen_l=results["L"].bores_df if "L" in results else None,
            )
            bores_csv = out_dir / "bores_combined.csv"
            combined_bores.to_csv(bores_csv, index=False)
            typer.echo(f"  combined receptor-bore table → {bores_csv}")

    if figures and results:
        typer.echo("\nWriting diagnostic figures…")
        fig_dir = Path("reports/figures")
        written = make_figures(grid, inputs, cfg, results, fig_dir)
        for p in written:
            typer.echo(f"  → {p}")

    if combined is not None:
        typer.echo("\nWriting impact assessment report…")
        report_md = Path("reports/impact_assessment.md")
        report_json = out_dir / "impact_assessment.json"
        write_impact_report(
            cfg=cfg, grid=grid, inputs=inputs, results=results,
            combined=combined, config_path=config,
            md_path=report_md, json_path=report_json,
            combined_bores=combined_bores,
        )
        typer.echo(f"  → {report_md}")
        typer.echo(f"  → {report_json}")


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


@app.command()
def serve(
    config: Path = typer.Option("config.yaml", "--config", "-c"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload",
                                help="Auto-reload on code changes (dev only)."),
):
    """Run the FastAPI service for the regulator UI."""
    import os
    import uvicorn
    os.environ["PRECIPICE_CONFIG"] = str(config)
    typer.echo(f"PRECIPICE_CONFIG={config}; serving on http://{host}:{port}")
    uvicorn.run("src.api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()

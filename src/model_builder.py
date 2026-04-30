"""Build MF6 simulations for Scenarios A and C (CLAUDE.md §6.3).

This module is a stub: the full FloPy plumbing lands once the Theis sanity
test (CLAUDE.md §10) is wired up. The function signatures here define the
boundaries Phase 2 will rely on.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .grid import Grid


def build_scenario(
    cfg: Config,
    grid: Grid,
    scenario: str,
    workspace: Path,
    wells: list[tuple[int, int, int, float]],
) -> "object":
    """Construct an MFSimulation for the named scenario.

    Args:
        cfg:        Loaded run configuration.
        grid:       Structured grid built from properties.csv.
        scenario:   "A" (existing) or "C" (proposed only).
        workspace:  Directory where MF6 input files are written.
        wells:      List of (lay, row, col, rate_m3_per_day) tuples for WEL.

    Returns:
        flopy.mf6.MFSimulation ready for write_simulation()/run_simulation().
    """
    raise NotImplementedError(
        "model_builder.build_scenario is not yet implemented; coming with "
        "the Theis sanity test."
    )


def build_steady_state(cfg: Config, grid: Grid, workspace: Path) -> "object":
    """Pre-development steady-state run (no wells, recharge active)."""
    raise NotImplementedError(
        "model_builder.build_steady_state is not yet implemented."
    )

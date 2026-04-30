"""Run scenarios and sample drawdown at receptors (CLAUDE.md §6.4)."""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .grid import Grid
from .io_layer import Inputs


def run_scenario(cfg: Config, grid: Grid, inputs: Inputs, scenario: str, workspace: Path):
    """Build, run, and post-process a single scenario.

    Returns a dict with keys: 'receptors_df', 'drawdown_rasters_by_year'.
    """
    raise NotImplementedError(
        "scenarios.run_scenario is not yet implemented; depends on model_builder."
    )

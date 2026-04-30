"""Tiny synthetic end-to-end run (CLAUDE.md §11.1).

Skipped until model_builder + scenarios are implemented.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="awaits scenarios implementation (Phase 1.5)")
def test_synthetic_pipeline_runs():
    """10 km × 10 km synthetic case, 2 existing bores, 1 proposed bore."""
    pass

"""Theis analytical sanity test (CLAUDE.md §10, §11.2).

Skipped until model_builder.build_scenario is wired up.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="awaits model_builder implementation (Phase 1.5)")
def test_single_well_matches_theis():
    """Single well, uniform K & Ss, far boundary → drawdown matches Theis to ~5 %."""
    pass

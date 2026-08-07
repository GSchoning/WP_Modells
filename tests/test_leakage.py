"""Quasi-3D vertical leakage (leakage.LeakageCfg / src/leakage.py).

Three load-bearing properties:
  1. Leakage is the stabiliser: in a closed box, drawdown without it
     grows without bound (container draining); with it the cone
     approaches the Hantush steady state and late-time growth dies.
  2. The SOURCE HEAD cancels in twin-run drawdown (linear GHB) — only
     the conductance matters for drawdown, so an imperfect overlying
     head surface cannot corrupt the drawdown signal.
  3. The builder maps source points to cells by X/Y, covers active
     cells only, averages stacked duplicates, and fails loudly when
     configured incompletely.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import LeakageCfg
from src.grid import synthetic_uniform_grid
from src.leakage import leakage_ghb_cells
from src.model_builder import build_transient

mf6_missing = shutil.which("mf6") is None


def _grid():
    # 31x31 closed box, 500 m cells, T = 250 m2/d, S = 5e-4.
    return synthetic_uniform_grid(nrow=31, ncol=31, dx=500.0, dy=500.0,
                                  K=2.5, Ss=1e-5, thickness=100.0)


def _leak_records(grid, kv_over_b, source_head):
    dx = float(grid.delr.mean()); dy = float(grid.delc.mean())
    cond = kv_over_b * dx * dy
    return [(0, r, c, source_head, cond)
            for r in range(grid.nrow) for c in range(grid.ncol)]


def _drawdown(tmp_path, tag, grid, ghb_cells, t_days=36525.0):
    """Twin-run drawdown at the final time."""
    heads = {}
    for sub, wells in (("p", [(0, 15, 15, -2000.0)]), ("np", [])):
        sim = build_transient(
            grid, tmp_path / f"{tag}_{sub}", name="m", wells=wells,
            initial_head=150.0, perioddata=[(t_days / 2, 20, 1.2), (t_days / 2, 20, 1.2)],
            ghb_cells=ghb_cells or None, recharge=False,
        )
        sim.write_simulation(silent=True)
        ok, _ = sim.run_simulation(silent=True)
        assert ok, f"{tag}/{sub} failed"
        import flopy
        hf = flopy.utils.HeadFile(str(tmp_path / f"{tag}_{sub}" / "m.hds"))
        times = hf.get_times()
        heads[sub] = {t: hf.get_data(totim=t)[0] for t in (times[19], times[-1])}
        heads[f"{sub}_times"] = (times[19], times[-1])
    t_mid, t_end = heads["p_times"]
    s_mid = heads["np"][t_mid] - heads["p"][t_mid]
    s_end = heads["np"][t_end] - heads["p"][t_end]
    return s_mid, s_end


@pytest.mark.skipif(mf6_missing, reason="mf6 binary not on PATH")
def test_leakage_flattens_the_container(tmp_path):
    grid = _grid()
    s_mid0, s_end0 = _drawdown(tmp_path, "noleak", grid, [])
    leak = _leak_records(grid, kv_over_b=1e-5, source_head=150.0)
    s_midL, s_endL = _drawdown(tmp_path, "leak", grid, leak)

    well = (15, 15)
    # Closed box: drawdown keeps climbing between 50 and 100 yr.
    growth0 = s_end0[well] - s_mid0[well]
    growthL = s_endL[well] - s_midL[well]
    assert growth0 > 1.0                       # container draining
    assert growthL < 0.1 * growth0             # Hantush plateau
    assert s_endL[well] < s_end0[well]         # leakage supplies the well


@pytest.mark.skipif(mf6_missing, reason="mf6 binary not on PATH")
def test_source_head_cancels_in_drawdown(tmp_path):
    grid = _grid()
    a = _leak_records(grid, kv_over_b=1e-5, source_head=150.0)
    b = _leak_records(grid, kv_over_b=1e-5, source_head=170.0)   # +20 m everywhere
    _, s_a = _drawdown(tmp_path, "srcA", grid, a)
    _, s_b = _drawdown(tmp_path, "srcB", grid, b)
    assert np.abs(s_a - s_b).max() < 1e-8      # linear GHB: head cancels


def _cfg(**leak_kwargs):
    # leakage_ghb_cells only reads cfg.leakage.
    from types import SimpleNamespace
    return SimpleNamespace(leakage=LeakageCfg(**leak_kwargs))


def test_builder_contract(tmp_path):
    grid = _grid()
    grid.idomain[0, 0, :] = 0                  # deactivate the top row

    # source file: one point per cell of rows 0-2, plus a duplicate stack
    rows = []
    for r in range(3):
        for c in range(grid.ncol):
            x = grid.xorigin + (c + 0.5) * 500.0
            y = grid.yorigin + (grid.nrow - r - 0.5) * 500.0
            rows.append({"INODE": r * grid.ncol + c + 1, "X": x, "Y": y,
                         "head_predev_m": 100.0 + r})
    rows.append({**rows[-1], "head_predev_m": 104.0})   # stacked duplicate
    src = tmp_path / "heads.csv"
    pd.DataFrame(rows).to_csv(src, index=False)

    cfg = _cfg(enabled=True, source_heads_csv=str(src), kv_over_b_per_day=2e-6)
    recs, desc = leakage_ghb_cells(cfg, grid)
    # top row inactive -> rows 1 and 2 only
    assert len(recs) == 2 * grid.ncol
    conds = {round(rec[4], 9) for rec in recs}
    assert conds == {round(2e-6 * 500.0 * 500.0, 9)}    # Kv/b' * cell area
    # duplicate stack averaged: last cell of row 2 got (102 + 104) / 2
    dup = [rec for rec in recs if rec[1] == 2 and rec[2] == grid.ncol - 1]
    assert dup and dup[0][3] == pytest.approx(103.0)

    assert leakage_ghb_cells(_cfg(enabled=False), grid) == ([], "disabled")
    with pytest.raises(ValueError):
        leakage_ghb_cells(_cfg(enabled=True, kv_over_b_per_day=1e-6), grid)
    with pytest.raises(FileNotFoundError):
        leakage_ghb_cells(_cfg(enabled=True, kv_over_b_per_day=1e-6,
                               source_heads_csv=str(tmp_path / "nope.csv")), grid)

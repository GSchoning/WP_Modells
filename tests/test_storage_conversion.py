"""Convertible storage (assessment.storage_mode: "convertible").

The parent UWIR model switches cell storage from elastic Ss to Sy when
head falls below the cell top (desaturation). We reproduce that with MF6
STO iconvert=1 while keeping icelltype=0, which preserves constant
transmissivity — MF6's storage conversion keys off head vs the cell top
independently of the NPF cell type.

Two properties are load-bearing:
  1. While heads stay above every cell top, convertible == static to
     solver precision (the mode is a strict no-op in the confined regime).
  2. Once pumping pulls heads below cell tops, conversion engages Sy and
     drawdown all but stalls, where static storage would keep draining
     elastic Ss toward physically impossible depths.
"""
from __future__ import annotations

import dataclasses
import shutil

import numpy as np
import pytest

from src.grid import synthetic_uniform_grid
from src.model_builder import build_transient

mf6_missing = shutil.which("mf6") is None
pytestmark = pytest.mark.skipif(mf6_missing, reason="mf6 binary not on PATH")

SY = 0.1


def _grid():
    # 21x21 closed box, 100 m cells, 50 m thick (top=50, bot=0).
    g = synthetic_uniform_grid(nrow=21, ncol=21, dx=100.0, dy=100.0,
                               K=5.0, Ss=1e-6, thickness=50.0)
    return dataclasses.replace(g, formation_sy=SY)


def _run(tmp_path, tag, *, convertible, q_m3d, strt, t_days=2000.0):
    grid = _grid()
    sim = build_transient(
        grid, tmp_path / tag, name=tag,
        wells=[(0, 10, 10, -q_m3d)],
        initial_head=strt,
        perioddata=[(t_days, 30, 1.2)],
        recharge=False,
        storage_convertible=convertible,
    )
    sim.write_simulation(silent=True)
    ok, _ = sim.run_simulation(silent=True)
    assert ok, f"{tag} failed to converge"
    import flopy
    h = flopy.utils.HeadFile(str(tmp_path / tag / f"{tag}.hds")).get_data()[0]
    return strt - h                                            # drawdown


def test_noop_while_confined(tmp_path):
    # Start 100 m above the cell top; pump gently so heads stay well
    # above it. Conversion must not engage: the convertible run has to
    # reproduce pure elastic-storage behaviour. It runs with a tightened
    # outer closure (see _make_sim tight_outer) while static uses the
    # loose MODERATE preset, so the two agree to solver slack, and the
    # convertible result is pinned to the analytical elastic mass
    # balance exactly.
    s_static = _run(tmp_path, "conf_static", convertible=False, q_m3d=5.0, strt=150.0)
    s_conv = _run(tmp_path, "conf_conv", convertible=True, q_m3d=5.0, strt=150.0)
    assert s_static.max() < 90.0            # confined throughout (head > top=50 needs s < 100)

    v = 5.0 * 2000.0                        # pumped volume, m3
    area = 21 * 21 * 100.0 * 100.0
    s_elastic_mean = v / (1e-6 * 50.0 * area)
    assert s_conv.mean() == pytest.approx(s_elastic_mean, rel=1e-3)
    # Static's loose preset leaks ~1.4% of the budget on this stiff toy
    # problem, so like-for-like agreement is bounded by that slack.
    assert np.abs(s_static - s_conv).max() < 0.02 * s_static.max()


def test_conversion_engages_sy_below_cell_top(tmp_path):
    # Closed box, hard pumping: elastic storage alone cannot supply the
    # well without absurd drawdown; with conversion the water table forms
    # just below the top and Sy (1e5x the elastic storativity per metre)
    # takes over.
    strt, q = 100.0, 400.0                  # 50 m of confined margin
    s_static = _run(tmp_path, "hard_static", convertible=False, q_m3d=q, strt=strt)
    s_conv = _run(tmp_path, "hard_conv", convertible=True, q_m3d=q, strt=strt)

    # Static runaway: mean drawdown from elastic storage only.
    v = q * 2000.0                                       # pumped volume m3
    area = 21 * 21 * 100.0 * 100.0
    s_elastic_mean = v / (1e-6 * 50.0 * area)            # ~ 800 m
    assert s_static.mean() == pytest.approx(s_elastic_mean, rel=0.05)

    # Converted: stalls just past the 50 m confined margin.
    margin = strt - 50.0                                 # head above cell top
    sy_tail = (v - 1e-6 * 50.0 * area * margin) / (SY * area)
    assert s_conv.mean() == pytest.approx(margin + sy_tail, rel=0.15)
    assert s_conv.max() < 0.15 * s_static.max()


def test_water_table_cells_use_elastic_ss_not_sy_decode(tmp_path):
    # In convertible mode a water-table-marked cell must NOT keep its
    # Ss = Sy/b decode: the mixed formulation would then count the yield
    # twice (Sy at the moving table + Sy-scale "elastic" storage below
    # it). ss_elastic carries the real elastic value.
    g = _grid()
    b = 50.0
    ss_decoded = np.full_like(g.ss, SY / b)              # the static-mode fake
    ss_elastic = np.full_like(g.ss, 1e-6)
    g2 = dataclasses.replace(g, ss=ss_decoded, ss_elastic=ss_elastic,
                             wt_mask=np.ones((g.nrow, g.ncol), bool))
    sim = build_transient(
        g2, tmp_path / "wt", name="wt",
        wells=[(0, 10, 10, -400.0)],
        initial_head=100.0,
        perioddata=[(2000.0, 30, 1.2)],
        recharge=False,
        storage_convertible=True,
    )
    sim.write_simulation(silent=True)
    ok, _ = sim.run_simulation(silent=True)
    assert ok
    import flopy
    h = flopy.utils.HeadFile(str(tmp_path / "wt" / "wt.hds")).get_data()[0]
    s = 100.0 - h
    # With the elastic Ss in force, the confined leg drains fast (tiny S)
    # and the Sy leg matches the mass balance from the previous test. If
    # the Sy/b decode leaked through, the confined leg alone would hold
    # mean drawdown near ~16 m and never reach the cell top.
    v = 400.0 * 2000.0
    area = 21 * 21 * 100.0 * 100.0
    margin = 50.0
    sy_tail = (v - 1e-6 * b * area * margin) / (SY * area)
    assert s.mean() == pytest.approx(margin + sy_tail, rel=0.15)

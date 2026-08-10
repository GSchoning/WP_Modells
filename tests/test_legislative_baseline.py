"""Legislative baseline: active approved decisions fold into the take.

The decision audit trail doubles as the ledger — every ACTIVE approve
decision's well change-set (single bores, multi-bores, trades with their
signed source reductions) joins the existing water use for subsequent
assessments; rollbacks remove it again.
"""
from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from src.api import decisions as dmod
from src.io_layer import Inputs, ML_PER_YEAR_TO_M3_PER_DAY, augment_inputs_with_approved

CRS = "EPSG:28355"


def _record(path, decision, wells, seq_note=""):
    return dmod.record_decision(
        path, decision=decision, regulator="test",
        scenario={"scenario_type": "multi", "wells_run": wells,
                  "from_bore_id": None, "bore_label": seq_note},
        summary={"regulatory_threshold_m": 0.4},
    )


def test_active_approved_wells_folds_ledger(tmp_path):
    p = tmp_path / "events.jsonl"
    d1 = _record(p, "approve", [{"label": "NEW_A", "x": 1000.0, "y": 2000.0,
                                 "rate_ML_per_year": 500.0}])
    _record(p, "reject", [{"label": "NOPE", "x": 0.0, "y": 0.0,
                           "rate_ML_per_year": 999.0}])
    # a trade: +300 at the destination, -300 at the source
    _record(p, "approve", [
        {"label": "to TRADE_DST", "x": 3000.0, "y": 4000.0, "rate_ML_per_year": 300.0},
        {"label": "from 107775", "x": 5000.0, "y": 6000.0, "rate_ML_per_year": -300.0},
    ])
    d3 = _record(p, "approve", [{"label": "NEW_B", "x": 7000.0, "y": 8000.0,
                                 "rate_ML_per_year": 100.0}])

    wells = dmod.active_approved_wells(p)
    assert [w["label"] for w in wells] == ["NEW_A", "to TRADE_DST", "from 107775", "NEW_B"]
    assert wells[2]["rate_ML_per_year"] == -300.0          # trade sign preserved
    fp_all = dmod.approved_wells_fingerprint(wells)

    # Roll back to d1: the trade and NEW_B leave the legislative state.
    dmod.rollback_to(p, d1["id"], "test")
    wells2 = dmod.active_approved_wells(p)
    assert [w["label"] for w in wells2] == ["NEW_A"]
    assert dmod.approved_wells_fingerprint(wells2) != fp_all


def test_reverse_and_clear(tmp_path):
    p = tmp_path / "events.jsonl"
    d1 = _record(p, "approve", [{"label": "A", "x": 1.0, "y": 2.0, "rate_ML_per_year": 10.0}])
    d2 = _record(p, "approve", [{"label": "B", "x": 3.0, "y": 4.0, "rate_ML_per_year": 20.0}])
    d3 = _record(p, "approve", [{"label": "C", "x": 5.0, "y": 6.0, "rate_ML_per_year": 30.0}])

    # Reverse the MIDDLE approval: the others stay active.
    rec = dmod.reverse_decision(p, d2["id"], "test")
    assert rec["status"] == "reversed" and rec["reversed_by"] == "test"
    assert [w["label"] for w in dmod.active_approved_wells(p)] == ["A", "C"]

    # Reversing a non-active or non-approve decision fails loudly.
    with pytest.raises(ValueError):
        dmod.reverse_decision(p, d2["id"], "test")
    with pytest.raises(KeyError):
        dmod.reverse_decision(p, "dec_99999", "test")

    # Rollback-to-d2 restores it (and rolls back d3).
    dmod.rollback_to(p, d2["id"], "test")
    assert [w["label"] for w in dmod.active_approved_wells(p)] == ["A", "B"]

    # Clear reverses everything active; the audit records remain.
    n = dmod.clear_all(p, "test")
    assert n == 2
    assert dmod.active_approved_wells(p) == []
    statuses = {d["id"]: d["status"] for d in dmod.list_decisions(p)}
    assert statuses[d1["id"]] == "reversed" and statuses[d2["id"]] == "reversed"
    # A clear on an already-empty state appends nothing.
    assert dmod.clear_all(p, "test") == 0
    # ...and any record can still be restored afterwards.
    dmod.rollback_to(p, d1["id"], "test")
    assert [w["label"] for w in dmod.active_approved_wells(p)] == ["A"]


def _inputs():
    def gdf(ids, rates):
        return gpd.GeoDataFrame(
            {"bore_id": ids, "rate_m3_per_day": rates},
            geometry=[Point(i * 100.0, 0.0) for i in range(len(ids))], crs=CRS)
    return Inputs(
        formation_extent=gdf([], []), outcrop=gdf([], []),
        properties=pd.DataFrame(),
        pumping_bores=gdf(["B1", "B2"], [10.0, 20.0]),
        receptor_bores=gdf(["B2"], [20.0]),
        licensed_bores=gdf(["B2"], [20.0]),
        springs=None,
    )


def test_augment_inputs_with_approved():
    wells = [
        {"decision_id": "dec_00001", "label": "NEW_A", "x": 900.0, "y": 0.0,
         "rate_ML_per_year": 365.25},                       # -> 1000 m3/d
        {"decision_id": "dec_00002", "label": "from B1", "x": 0.0, "y": 0.0,
         "rate_ML_per_year": -36.525},                      # trade reduction
    ]
    out = augment_inputs_with_approved(_inputs(), wells, CRS)

    assert len(out.pumping_bores) == 4                      # both wells join take
    new = out.pumping_bores[out.pumping_bores.bore_id == "NEW_A [dec_00001]"]
    assert new.rate_m3_per_day.iloc[0] == pytest.approx(1000.0)
    assert bool(new.approved.iloc[0]) is True
    neg = out.pumping_bores[out.pumping_bores.bore_id == "from B1 [dec_00002]"]
    assert neg.rate_m3_per_day.iloc[0] == pytest.approx(-100.0)
    # CSV rows keep approved=False
    assert not out.pumping_bores[out.pumping_bores.bore_id == "B1"].approved.iloc[0]

    # only POSITIVE ledger wells become licensed/receptor bores
    assert list(out.licensed_bores.bore_id) == ["B2", "NEW_A [dec_00001]"]
    assert list(out.receptor_bores.bore_id) == ["B2", "NEW_A [dec_00001]"]

    # empty ledger: passthrough, same object
    same = augment_inputs_with_approved(_inputs(), [], CRS)
    assert len(same.pumping_bores) == 2

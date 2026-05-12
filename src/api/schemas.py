"""Pydantic request/response models for the regulator API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProposedBore(BaseModel):
    """A single proposed bore, used for backward-compatible single-bore requests."""
    bore_id: str = Field(..., examples=["PROPOSED_001"])
    x: float = Field(..., description="Easting in project CRS (m).")
    y: float = Field(..., description="Northing in project CRS (m).")
    rate_ML_per_year: float = Field(..., gt=0)


class WellSpec(BaseModel):
    """One well in a multi-well or trade change set.

    rate_ML_per_year is signed: positive = adding extraction, negative =
    removing extraction (used for trade scenarios where an existing
    licence is transferred — +rate at the new location and -rate at the
    old).
    """
    label: str = "well"
    x: float
    y: float
    rate_ML_per_year: float


class ScenarioRequest(BaseModel):
    """Three scenario flavours, all reduce to a list of well changes on the backend:

    - single: a single new bore at (x, y) with positive rate.
    - multi:  several new bores; each entry in `new_wells` is a positive-rate WellSpec.
    - trade:  transfer the full rate of `from_bore_id` to (to_x, to_y);
              server constructs +rate at the new location and -rate at the
              old so superposition yields the net effect (recovery at the
              source, new drawdown at the destination).
    """
    scenario_type: Literal["single", "multi", "trade"] = "single"
    # single mode (kept for back-compat with old clients).
    proposed_bore: ProposedBore | None = None
    # multi mode.
    new_wells: list[WellSpec] = []
    # trade mode.
    from_bore_id: str | None = None
    to_x: float | None = None
    to_y: float | None = None
    recharge_multiplier: float = Field(1.0, ge=0.0, le=10.0,
        description="Sensitivity-analysis scale on recharge (1.0 = calibrated).")


class ComplexDrawdown(BaseModel):
    complex_id: str
    n_springs: int = 1
    s_approved_m: float
    s_additional_m: float
    s_total_m: float
    s_additional_theis_m: float | None = None
    r_to_proposed_m: float | None = None             # min distance over member springs
    exceeds_threshold: bool = False                   # s_total_m >= regulatory threshold
    already_exceeded: bool = False                    # s_approved_m alone >= threshold
    triggered_by_proposed: bool = False               # s_approved < threshold but s_total >=


class YearResults(BaseModel):
    time_years: float
    complexes: list[ComplexDrawdown]
    n_exceedances: int = 0
    n_triggered: int = 0
    n_already_exceeded: int = 0


class TheisDiagnostics(BaseModel):
    T_m2_per_day: float
    S_dimensionless: float
    well_cell: list[int]                 # [row, col]


class ScenarioResponse(BaseModel):
    scenario_type: Literal["single", "multi", "trade"] = "single"
    # The well change set actually run (echoed back so the UI can label markers).
    wells_run: list[WellSpec] = []
    # Back-compat: first positive-rate well in wells_run if any.
    proposed_bore: ProposedBore | None = None
    output_years: list[float]
    regulatory_threshold_m: float
    by_year: list[YearResults]
    top_n_total: list[ComplexDrawdown]
    n_exceedances_any_year: int = 0
    n_triggered_any_year: int = 0                     # tips over because of proposal
    n_already_exceeded_any_year: int = 0              # was already over without proposal
    runtime_seconds: float
    theis: TheisDiagnostics | None = None


class BaselineResponse(BaseModel):
    cache_key: str
    regulatory_threshold_m: float
    output_years: list[float]
    by_year: list[YearResults]


class ExistingBore(BaseModel):
    bore_id: str
    x: float
    y: float
    lng: float
    lat: float
    rate_ML_per_year: float


class ExistingBoresResponse(BaseModel):
    bores: list[ExistingBore]


class HealthResponse(BaseModel):
    status: str
    project: str
    crs: str
    n_pumping_bores: int
    n_springs: int
    n_spring_complexes: int
    regulatory_threshold_m: float
    baseline_cached: bool

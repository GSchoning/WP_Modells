"""Pydantic request/response models for the regulator API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProposedBore(BaseModel):
    bore_id: str = Field(..., examples=["PROPOSED_001"])
    x: float = Field(..., description="Easting in project CRS (m).")
    y: float = Field(..., description="Northing in project CRS (m).")
    rate_ML_per_year: float = Field(..., gt=0)


class ScenarioRequest(BaseModel):
    proposed_bore: ProposedBore


class ComplexDrawdown(BaseModel):
    complex_id: str
    n_springs: int = 1
    s_approved_m: float
    s_additional_m: float
    s_total_m: float
    s_additional_theis_m: float | None = None
    r_to_proposed_m: float | None = None             # min distance over member springs
    exceeds_threshold: bool = False                   # s_total_m >= regulatory threshold


class YearResults(BaseModel):
    time_years: float
    complexes: list[ComplexDrawdown]
    n_exceedances: int = 0


class TheisDiagnostics(BaseModel):
    T_m2_per_day: float
    S_dimensionless: float
    well_cell: list[int]                 # [row, col]


class ScenarioResponse(BaseModel):
    proposed_bore: ProposedBore
    output_years: list[float]
    regulatory_threshold_m: float
    by_year: list[YearResults]
    top_n_total: list[ComplexDrawdown]
    n_exceedances_any_year: int = 0
    runtime_seconds: float
    theis: TheisDiagnostics | None = None


class BaselineResponse(BaseModel):
    cache_key: str
    regulatory_threshold_m: float
    output_years: list[float]
    by_year: list[YearResults]


class HealthResponse(BaseModel):
    status: str
    project: str
    crs: str
    n_pumping_bores: int
    n_springs: int
    n_spring_complexes: int
    regulatory_threshold_m: float
    baseline_cached: bool

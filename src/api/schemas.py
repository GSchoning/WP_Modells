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


class TradeDestination(BaseModel):
    """One destination in a multi-destination trade.

    rate_ML_per_year is the (positive) share of the source licence's
    rate that lands here. The sum of all destination rates is checked
    against the source bore's full rate; oversubscription returns 400.
    """
    x: float
    y: float
    rate_ML_per_year: float = Field(..., gt=0)


class ScenarioRequest(BaseModel):
    """Three scenario flavours, all reduce to a list of well changes on the backend:

    - single: a single new bore at (x, y) with positive rate.
    - multi:  several new bores; each entry in `new_wells` is a positive-rate WellSpec.
    - trade:  transfer the full rate of `from_bore_id` either to a single
              (to_x, to_y) destination (back-compat) or split across many
              entries in `to_wells`. Server constructs +rate at each
              destination and -source_rate at the original so superposition
              yields the net effect (recovery at the source, new drawdown
              at each destination).
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
    to_wells: list[TradeDestination] = []
    recharge_multiplier: float = Field(1.0, ge=0.0, le=10.0,
        description="Sensitivity-analysis scale on recharge (1.0 = calibrated).")


class ComplexDrawdown(BaseModel):
    complex_id: str
    n_springs: int = 1
    s_approved_m: float
    s_licensed_m: float = 0.0             # entitlement-take subset of s_approved (<= s_approved_m)
    s_additional_m: float
    s_total_m: float
    s_additional_theis_m: float | None = None
    # Cumulative Theis-superposition estimate of the approved-take impact
    # (every existing bore, standard assessment T/S) — the current-practice
    # method, shown beside the modelled s_approved_m for comparison.
    s_approved_theis_m: float | None = None
    r_to_proposed_m: float | None = None             # min distance over member springs
    exceeds_threshold: bool = False                   # s_total_m >= regulatory threshold
    already_exceeded: bool = False                    # s_approved_m alone >= threshold
    triggered_by_proposed: bool = False               # s_approved < threshold but s_total >=
    # Receptor sits within ~2 grid cells of a proposed well — drawdown in
    # and next to the well cell is mesh-dependent (point sink in a finite
    # cell), so the value carries extra numerical uncertainty.
    mesh_dependent: bool = False


class BoreDrawdown(BaseModel):
    """Drawdown at a receptor water bore (non-S&D licensed bore).

    Report-only: no threshold classification, because the bore trigger
    criterion (unlike the 0.4 m spring value) has not been confirmed by
    the department. Note that s_approved_m at an extraction bore includes
    that bore's own cell-averaged drawdown; s_additional_m carries no
    self-impact and is the decision-relevant number for a proposal.
    """
    bore_id: str
    s_approved_m: float
    s_licensed_m: float = 0.0
    s_additional_m: float
    s_total_m: float
    r_to_proposed_m: float | None = None
    # Within ~2 grid cells of a proposed well — mesh-dependent value.
    mesh_dependent: bool = False


class YearResults(BaseModel):
    time_years: float
    complexes: list[ComplexDrawdown]
    bores: list[BoreDrawdown] = []
    n_exceedances: int = 0
    n_triggered: int = 0
    n_already_exceeded: int = 0


class TheisDiagnostics(BaseModel):
    T_m2_per_day: float
    S_dimensionless: float
    well_cell: list[int]                 # [row, col]


class ScenarioQA(BaseModel):
    """Model-quality metrics for the run, surfaced for regulatory defensibility.

    boundary_warning is set when the proposed change produces non-trivial
    drawdown at the model boundary — CHD cells absorb drawdown (impact is
    then under-predicted near them), no-flow edges reflect it (over-
    predicted). Either way the number needs a caveat.
    """
    max_pct_discrepancy: float = 0.0                  # worst MF6 budget imbalance (%)
    chd_max_drawdown_m: float = 0.0
    noflow_max_drawdown_m: float = 0.0
    boundary_warning: bool = False
    mass_balance_warning: bool = False
    # Linearised rejected-recharge drains that a real drain would have
    # shut off (head fell below drain elevation in the pumped run) —
    # drawdown near those cells is under-predicted. Legacy linearised_ghb
    # mode only; always 0 (and no warning) with real DRN transients.
    n_drain_reversals: int = 0
    drain_warning: bool = False
    # DRN mode: drains dried by the proposal (marginal vs the A baseline)
    # and the proposal's captured rejected-recharge / spring-baseflow
    # discharge. Physics information, not an error flag.
    n_drains_dried: int = 0
    drain_capture_ML_per_year: float = 0.0


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
    qa: ScenarioQA | None = None
    # Traceability: config/input hashes, baseline cache key, mf6 version.
    provenance: dict[str, str] | None = None
    job_id: str | None = None


class ScenarioJobStatus(BaseModel):
    """Envelope returned by POST /api/scenarios and the job-polling endpoint."""
    job_id: str
    status: Literal["queued", "running", "done", "error"]
    progress: str = ""
    created_at: str
    result: ScenarioResponse | None = None            # populated when status == done
    error: str | None = None                          # populated when status == error


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


class ModelSettingsRequest(BaseModel):
    """Runtime model-formulation switch (session override; the config file
    still sets the defaults that apply after a restart). Set one or both."""
    storage_mode: Literal["static", "convertible"] | None = None
    ic_source: Literal["steady_state", "parent_predev"] | None = None


class ModelSettingsResponse(BaseModel):
    storage_mode: Literal["static", "convertible"]     # effective (or target while rebuilding)
    config_default: Literal["static", "convertible"]
    ic_source: Literal["steady_state", "parent_predev"] = "steady_state"
    ic_config_default: Literal["steady_state", "parent_predev"] = "steady_state"
    # Parent-predev IC needs the predev heads export on disk.
    ic_parent_available: bool = True
    # Whether a complete baseline for each option is already on disk —
    # switching to a cached combination is near-instant; otherwise the
    # baseline rebuilds (MF6 runs, minutes). Keyed by the OTHER dimension
    # held at its current value.
    baseline_cached: dict[str, bool]                   # per storage mode
    ic_baseline_cached: dict[str, bool] = {}           # per IC source
    rebuilding: bool = False
    rebuild_error: str | None = None


class HealthResponse(BaseModel):
    status: str
    project: str
    crs: str
    n_pumping_bores: int
    n_springs: int
    n_spring_complexes: int
    regulatory_threshold_m: float
    baseline_cached: bool
    aquifer: str = "precipice"          # module key serving this response
    aquifer_title: str = "Precipice Sandstone"


class DecisionScenario(BaseModel):
    """Snapshot of the scenario being decided on, captured at decision time."""
    scenario_type: Literal["single", "multi", "trade"]
    wells_run: list[WellSpec] = []
    from_bore_id: str | None = None
    bore_label: str | None = None      # human-friendly label, e.g. proposed bore id


class DecisionSummary(BaseModel):
    """Headline numbers from the scenario result, copied into the decision."""
    n_exceedances_any_year: int = 0
    n_triggered_any_year: int = 0
    n_already_exceeded_any_year: int = 0
    regulatory_threshold_m: float
    output_years: list[float] = []
    runtime_seconds: float | None = None


class RecordDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    regulator: str = "unknown"
    note: str = ""
    scenario: DecisionScenario
    summary: DecisionSummary


class Decision(BaseModel):
    id: str
    seq: int
    decision: Literal["approve", "reject"]
    status: Literal["active", "rolled_back", "rejected", "reversed"]
    regulator: str
    created_at: str
    note: str = ""
    scenario: DecisionScenario
    summary: DecisionSummary
    rolled_back_at: str | None = None
    rolled_back_by: str | None = None
    rolled_back_to: str | None = None
    reversed_at: str | None = None
    reversed_by: str | None = None


class DecisionsResponse(BaseModel):
    decisions: list[Decision] = []
    active_head_id: str | None = None


class RollbackRequest(BaseModel):
    regulator: str = "unknown"

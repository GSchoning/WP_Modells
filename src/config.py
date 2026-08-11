from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class WaterUseCfg(BaseModel):
    path: Path
    source_crs: str
    lon_col: str
    lat_col: str
    id_col: str
    rate_col: str
    rate_units: Literal["ML/year", "m3/day"]
    formation_col: str | None = None
    formation_value: str | None = None
    receptor_filter: dict | None = None
    # Identifies "licensed take" — bores holding an entitlement/authority,
    # reported as a separate impact layer (s_licensed) alongside the full
    # existing take (s_approved). A bore is licensed when its `auth_col`
    # value is non-empty AND it is not in `exclude_values` of `exclude_column`
    # (i.e. non-Stock&Domestic). When unset, io_layer falls back to the
    # non-S&D receptor set. Shape:
    #   {auth_col: "AUTH_REFS", exclude_column: "OGIA_P3",
    #    exclude_values: ["Stock_Domestic"]}
    licensed_filter: dict | None = None


class ProposedBoreCfg(BaseModel):
    bore_id: str
    x: float | None = None
    y: float | None = None
    rate_ML_per_year: float | None = None


class InputsCfg(BaseModel):
    formation_extent: Path
    outcrop: Path
    properties_csv: Path
    dem: Path | None = None
    water_use: WaterUseCfg
    springs: Path | None = None
    proposed_bore: ProposedBoreCfg
    # Per-cell steady-state recharge (m/day) keyed by INODE — the OGIA
    # export (columns INODE, RCH_SS_m_per_day). Used when the properties
    # CSV's rch column is empty (as the delivered export is). Applied at
    # exactly the cells OGIA recharges (northern exposed-outcrop belt).
    recharge_csv: Path | None = None
    # Parent-model pre-development heads (extraction export: INODE, X, Y,
    # ILAY, head_predev_m). Used when assessment.ic_source is
    # "parent_predev"; multi-layer files stack per plan cell and are
    # averaged.
    predev_heads_csv: Path | None = None
    # Parent-model calibrated GHB export (ghb_cells.csv from
    # scripts/extract_uwir2025.py). When set and present, boundary GHBs in
    # "uwir_ghb" mode use these cells/heads/conductances instead of the
    # grid-frame-face placement with estimated C = K·b.
    ghb_cells_csv: Path | None = None
    # Optional attribute filter applied to the springs shapefile at ingest,
    # e.g. {column: "source_aqu", contains: "Hutton"} — the shared springs
    # layer attributes each spring to a source aquifer. Springs NOT
    # matching are excluded entirely.
    springs_attr_filter: dict | None = None
    # Union rule for the outcrop-proximity clip: springs matching this
    # attribute test are kept REGARDLESS of distance to the outcrop
    # (artesian complexes such as Boggomoss discharge through the
    # confining cover many km from outcrop, and pressure decline at
    # their location is exactly what the confined model predicts).
    # Springs matching neither this nor the outcrop buffer are dropped.
    springs_attr_keep: dict | None = None
    # Uniform-over-outcrop last resort, used only when neither the rch
    # column nor recharge_csv provides values. UWIR 2025 layer-24 balance:
    # 25,283 ML/yr over 1,231 km² = 5.63e-5 m/d.
    recharge_fallback_m_per_day: float | None = None


class AquiferCfg(BaseModel):
    thickness_m: float = 200
    top_elevation_m: float = 0


class GridCfg(BaseModel):
    source: Literal["properties_csv", "raster"] = "properties_csv"
    buffer_m: float = 50_000
    boundary_type: Literal["no_flow", "chd_regional_gradient"] = "no_flow"
    # Source CSV is exported from a multi-layer regional model; only rows
    # with ILAY == this value are used. The Precipice Sandstone is layer 24
    # in the current model revision (it was layer 23 in the 2019 UWIR
    # model — figures in the 2019 modelling report use the old numbering).
    # A list merges several parent layers into one model layer per cell
    # (Hutton Sandstone = [19, 20], Upper + Lower) — see grid._merge_layer_rows.
    properties_layer: int | list[int] = 24


class TimeCfg(BaseModel):
    total_years: float = 100
    nstp: int = 30
    tsmult: float = 1.2
    # Optional yearly-step early period. If > 0, the simulation is split
    # into two stress periods: a fine period of fine_period_years length
    # with one step per year (tsmult=1), then the remaining time in nstp
    # geometric steps. Useful when receptors near a well need resolution
    # in the first few years of drawdown.
    fine_period_years: int = 10
    output_years: list[float] = Field(default_factory=lambda: [10, 50, 100])


class DrainsCfg(BaseModel):
    """Rejected-recharge drains in the outcrop (parent-model device).

    Preferred source: `riv_cells_csv` — the parent model's RIV cells
    (extracted by scripts/extract_uwir2025.py), which ARE its surficial
    drains (stage == rbot) with calibrated stage and conductance.
    Fallback: drain elevation = minimum of the DEM within each outcrop
    cell, conductance estimated as K·A/b.
    The steady state always uses real DRN cells; `transient_mode` decides
    how the transient runs treat them — see src/drains.py.
    """
    enabled: bool = False
    # How the transient runs represent the drains:
    #   "drn"            — real MF6 DRN cells (head-dependent, shut off when
    #                      the head drops below the drain elevation). The
    #                      per-request scenario is then run as B (existing +
    #                      change set) directly, with s_additional derived
    #                      as B − A. Exact drain physics; superposition
    #                      becomes a QA property rather than the mechanism.
    #   "linearised_ghb" — legacy: flowing drains become fixed-head GHBs so
    #                      the system is exactly linear (A + C = B), at the
    #                      cost of fictitious water where pumping would dry
    #                      a drain (clamped outcrop drawdown).
    transient_mode: Literal["drn", "linearised_ghb"] = "drn"
    # Parent-model RIV export (ILAY, INODE, ICOL, IROW, X, Y, stage_m,
    # cond_m2_per_day, rbot_m). When set and present it supersedes the DEM.
    riv_cells_csv: Path | None = None
    # Fixed conductance (m²/day) for every drain. None = use the calibrated
    # per-cell RIV conductance (riv_cells_csv source) or estimate K·A/b
    # (DEM source).
    conductance_m2_per_day: float | None = None
    conductance_scale: float = 1.0


class SolverCfg(BaseModel):
    complexity: Literal["SIMPLE", "MODERATE", "COMPLEX"] = "MODERATE"


class RunCfg(BaseModel):
    scenarios: list[Literal["A", "C", "L"]] = Field(default_factory=lambda: ["A", "C", "L"])
    workspace_root: Path = Path("/tmp/mf6_workspaces")


class AssessmentCfg(BaseModel):
    # Regulatory drawdown trigger threshold (m). A spring complex is flagged
    # as "exceeded" when s_total at any output year >= this value.
    regulatory_threshold_m: float = 0.4
    spring_complex_col: str = "complex_na"
    spring_id_col: str = "site_no"
    # Springs whose point lands just outside the active domain (inactive
    # cell, or off the grid by less than this distance) are sampled at the
    # NEAREST ACTIVE CELL instead of being silently dropped — at 1500 m
    # cells, an on-the-ground spring can easily fall one cell outside the
    # active footprint. Springs farther than this from any active cell are
    # still excluded. 0 disables snapping.
    spring_snap_max_m: float = 3000.0
    # Ingest filter: springs farther than this from the module's outcrop
    # are dropped — beyond it, a connection to THIS aquifer is not
    # something the single-layer model can represent, so reporting a
    # drawdown there would be structurally meaningless. At 1500 m cells,
    # 3 km is two cells of slack around the mapped outcrop.
    spring_outcrop_buffer_m: float = 3000.0
    # Aquifer parameters for the Theis analytical comparison columns.
    # When set, these OVERRIDE the formation-averaged values — use the
    # department's standard assessment parameters so the Theis columns
    # reproduce current practice (superposition of Theis solutions).
    # None falls back to geometric-mean T / median S over active cells.
    theis_T_m2_per_day: float | None = None
    theis_S: float | None = None
    # Initial-condition source for every transient run:
    #   - "parent_predev" (default): overlay the parent model's
    #     pre-development heads (inputs.predev_heads_csv) onto the
    #     steady-state solution (SS fills uncovered cells). The IC
    #     cancels in twin-run drawdown EXCEPT through the head-dependent
    #     switches — drain drying and storage conversion — and those are
    #     exactly where it matters: measured on the Precipice A baseline
    #     @100 yr, capture doubles and the top spring complexes drop
    #     55-78% (311: 10.9 -> 2.4 m). Caveat: the parent surface is not
    #     an equilibrium of THIS model, so the no-pump twin drifts toward
    #     our own steady state (the drift itself cancels; the drain and
    #     conversion states early in the run reflect the parent surface).
    #   - "steady_state": our own MF6 steady-state pre-run (recharge +
    #     drains + boundary GHBs). Self-consistent, but it equilibrates
    #     ~15 m (median) BELOW the parent's pre-development surface (no
    #     vertical support; the Evergreen is sealed), starving the
    #     outcrop drains of discharge headroom — the conservative
    #     sensitivity case for spring impacts.
    ic_source: Literal["steady_state", "parent_predev"] = "parent_predev"
    # Storage formulation for the transient runs:
    #   - "convertible" (default): MF6 STO iconvert=1 with sy = formation
    #     outcrop Sy, matching the parent model's Ss<->Sy switching.
    #     Verified: conversion keys off head vs cell top even with
    #     icelltype=0, so transmissivity stays constant (no Newton, no dry
    #     cells) and the flow terms stay linear — only storage becomes
    #     piecewise. Water-table-marked cells carry a real elastic Ss (not
    #     the Sy/b decode) so the mixed formulation doesn't double-count.
    #     Identical to "static" wherever heads stay above cell tops.
    #   - "static" (legacy): every cell keeps its assigned storage forever
    #     (iconvert=0). Confined cells keep elastic Ss even when pumping
    #     pulls head below the cell top, over-predicting drawdown wherever
    #     depressurisation would really convert the cell to unconfined
    #     (storage jumps ~100x at conversion). Kept for comparability —
    #     measured on the Precipice A baseline @100 yr it inflates mean
    #     drawdown 16.7 -> 24.0 m and the p90 26.7 -> 72.4 m.
    storage_mode: Literal["static", "convertible"] = "convertible"
    # Sensitivity-analysis knob: scales the rch array uniformly. Default
    # 1.0 = use the calibrated values from the properties CSV. 0.5 halves
    # recharge, 2.0 doubles it. Drawdown is theoretically invariant under
    # confined-linear superposition, so this is mostly an integrity check.
    recharge_multiplier: float = 1.0
    # Lateral boundary treatment. The boundary audit (2026-07) showed the
    # perimeter is almost entirely a thin pinch-out fringe (thickness
    # p50 6-19 m vs 44 m interior; boundary T p50 ~8 m²/d), and the UWIR
    # 2019 report (Appendix A, Fig A1-14) shows the parent model assigns
    # GHBs on the Precipice's *western and southern* truncation faces
    # only, with natural pinch-outs closed:
    #   - "uwir_ghb":      no-flow perimeter + GHB on the W/S grid-frame
    #                      truncation faces (parent-model design).
    #   - "no_flow":       entire perimeter no-flow. Requires drains
    #                      (rejected recharge) as the steady-state outlet.
    #                      Conservative for drawdown (never absorbs it).
    #   - "chd_quadrants": legacy — CHD on chd_quadrants faces, head=NTOP.
    #   - "chd_all":       CHD on every active-edge cell.
    # If the steady state fails to converge under the configured mode, the
    # bootstrap falls back down this list with a loud warning.
    boundary_mode: Literal["uwir_ghb", "no_flow", "chd_quadrants", "chd_all"] = "uwir_ghb"
    # Grid-frame faces that get truncation GHBs in "uwir_ghb" mode.
    # UWIR 2025 (App. F, Fig F.1-15): western face only for layer 24.
    ghb_faces: list[Literal["N", "S", "E", "W"]] = Field(default_factory=lambda: ["W"])
    # Scale on the estimated GHB conductance C = K·b per cell (interim —
    # the 2025 report does not document the conductance basis).
    ghb_conductance_scale: float = 1.0
    # GHB head source: calibrated UWIR 2025 posterior pilot heads
    # (Fig G.1-39, 423-706 m AHD along the western strip) or NTOP.
    ghb_heads: Literal["uwir2025_pilot", "ntop"] = "uwir2025_pilot"
    # Storage at outcrop (negative-SS) cells:
    #   "formation_sy": every water-table-marked cell carries the
    #     formation-wide outcrop Sy (UWIR 2025 Table B.2-2 treats outcrop
    #     Sy as a single formation-wide parameter) — the intended
    #     "specific yield in outcrop" behaviour.
    #   "as_exported": follow the export's per-cell magnitudes (mixed Sy
    #     and Ss values; the split doesn't correlate with exposure,
    #     burial or recharge, so it's likely an export artefact).
    outcrop_storage: Literal["formation_sy", "as_exported"] = "formation_sy"
    # Compass quadrants (relative to the active-domain centroid) where the
    # boundary CHD is placed in "chd_quadrants" mode (also the fallback
    # order if "no_flow" cannot converge). Empty/None = all four.
    chd_quadrants: list[Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW"]] | None = None


class ProjectCfg(BaseModel):
    name: str
    crs: str


class LeakageCfg(BaseModel):
    """Quasi-3D vertical leakage through the confining bed (Hantush).

    Every active cell gets a head-dependent leakage boundary (GHB) whose
    source head is the overlying layer's steady-state head and whose
    conductance is C = (Kv/b') · cell area. This is the missing vertical
    supply term of the single-layer reconstruction: without it the model
    is a closed container and late-time drawdown grows linearly forever.

    Properties worth knowing:
      - The SOURCE HEAD cancels in twin-run drawdown (linear GHB); it
        shapes the steady-state IC — lifting it toward the parent-model
        surface — and thereby the drain states and conversion margins.
      - The CONDUCTANCE is what flattens the drawdown curves.
      - Hantush assumption: the source layer does not draw down, so late-
        time drawdown is slightly under-predicted relative to full 3D.
    """
    enabled: bool = False
    # Per-cell source heads: CSV with X, Y and a head column (the
    # extraction's predev_heads.csv / adjacent_L*_heads.csv schema —
    # INODE,X,Y[,ILAY],head_predev_m). Cells are matched by X/Y cell
    # centre, so the file may come from ANY layer: use the overlying
    # layer's heads when extracted; the aquifer's own pre-development
    # heads are a serviceable interim (pre-development vertical gradients
    # are small next to the ~100 m IC errors this corrects).
    source_heads_csv: Path | None = None
    head_col: str = "head_predev_m"
    # Aquitard vertical conductance per unit area, Kv/b' (1/day).
    # C = kv_over_b · dx·dy per cell. INTERIM: a single formation-wide
    # sensitivity value until the parent-model Kv arrays are extracted.
    kv_over_b_per_day: float | None = None
    # Per-cell conductance derived from the parent model's calibrated Kz
    # (scripts/derive_leakage_conductance.py): CSV with X, Y,
    # cond_m2_per_day. Takes precedence over kv_over_b_per_day.
    conductance_csv: Path | None = None
    conductance_scale: float = 1.0
    # Cells with no source-head coverage (outside the file's footprint)
    # simply get no leakage boundary.


class Config(BaseModel):
    project: ProjectCfg
    inputs: InputsCfg
    aquifer: AquiferCfg = AquiferCfg()
    grid: GridCfg = GridCfg()
    time: TimeCfg = TimeCfg()
    solver: SolverCfg = SolverCfg()
    run: RunCfg = RunCfg()
    assessment: AssessmentCfg = AssessmentCfg()
    drains: DrainsCfg = DrainsCfg()
    leakage: LeakageCfg = LeakageCfg()


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)

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


class AquiferCfg(BaseModel):
    thickness_m: float = 200
    top_elevation_m: float = 0


class GridCfg(BaseModel):
    source: Literal["properties_csv", "raster"] = "properties_csv"
    buffer_m: float = 50_000
    boundary_type: Literal["no_flow", "chd_regional_gradient"] = "no_flow"
    # Source CSV is exported from a multi-layer regional model; only rows
    # with ILAY == this value are used. The Precipice Sandstone is layer 24.
    properties_layer: int = 24


class TimeCfg(BaseModel):
    total_years: float = 100
    nstp: int = 30
    tsmult: float = 1.2
    output_years: list[float] = Field(default_factory=lambda: [10, 50, 100])


class SolverCfg(BaseModel):
    complexity: Literal["SIMPLE", "MODERATE", "COMPLEX"] = "MODERATE"


class RunCfg(BaseModel):
    scenarios: list[Literal["A", "C"]] = Field(default_factory=lambda: ["A", "C"])
    workspace_root: Path = Path("/tmp/mf6_workspaces")


class AssessmentCfg(BaseModel):
    # Regulatory drawdown trigger threshold (m). A spring complex is flagged
    # as "exceeded" when s_total at any output year >= this value.
    regulatory_threshold_m: float = 0.4
    spring_complex_col: str = "complex_na"
    spring_id_col: str = "site_no"
    # Sensitivity-analysis knob: scales the rch array uniformly. Default
    # 1.0 = use the calibrated values from the properties CSV. 0.5 halves
    # recharge, 2.0 doubles it. Drawdown is theoretically invariant under
    # confined-linear superposition, so this is mostly an integrity check.
    recharge_multiplier: float = 1.0
    # Compass quadrants (relative to the active-domain centroid) where the
    # boundary CHD is placed. Empty/None = all four (NE, NW, SW, SE).
    # Set to e.g. ["NW", "SE"] to keep CHD only on the deep-pinch-out
    # edges and leave the outcrop / regional-flow sides as no-flow.
    chd_quadrants: list[Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW"]] | None = None


class ProjectCfg(BaseModel):
    name: str
    crs: str


class Config(BaseModel):
    project: ProjectCfg
    inputs: InputsCfg
    aquifer: AquiferCfg = AquiferCfg()
    grid: GridCfg = GridCfg()
    time: TimeCfg = TimeCfg()
    solver: SolverCfg = SolverCfg()
    run: RunCfg = RunCfg()
    assessment: AssessmentCfg = AssessmentCfg()


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)

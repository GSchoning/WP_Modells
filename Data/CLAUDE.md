# Precipice Sandstone – Water Licence Impact Assessment Tool

## 1. Project goal

Build a workflow that models the **cumulative drawdown impact** of licensed water extraction from an aquifer, with a regulatory use case in mind: assessing the additional impact of a *proposed* new bore on top of the *currently approved* take.

The first deliverable is a **proof-of-concept (POC)** for the Precipice Sandstone (single-layer, regional AEM model). Once the POC is validated, it will be wrapped in a backend + frontend to support regulatory decision-making.

Receptors of concern:

- **Springs** (locations supplied as a shapefile)
- **Other water bores** (locations supplied as a shapefile or CSV)

Assessment timeframes: **10, 50, and 100 years** of constant (steady-state) pumping.

## 2. Approach summary

- Single-layer **transient** finite-difference model using **MODFLOW 6**, driven from Python via **FloPy**. The MODFLOW 6 executable is a Fortran binary that FloPy invokes after generating its input files.
- The model runs inside a **Docker container** so the MODFLOW binary, geospatial Python stack (GDAL/PROJ/GEOS), and Python dependencies are pinned and reproducible.
- Spatially variable K, Ss, and recharge are applied **per-cell** by resampling the user-supplied rasters onto the MODFLOW grid. This is the core reason for moving away from AEM — the MF6 grid carries spatial heterogeneity natively.
- **Superposition** is exploited because the system is linear in drawdown (constant K, Ss, confined, linear BCs):
  - Scenario A: existing approved bores only → "Currently approved impact"
  - Scenario C: proposed new bore only → "Additional impact from new bore"
  - Scenario B: existing + proposed = A + C (computed by addition, not re-modelled) → "Total new impact"
- Drawdown is computed as `initial_head − head(t)` from each scenario's transient run. The initial condition for the transient runs is a steady-state head field with **no pumping** (so all scenarios share the same pre-development baseline and superposition holds cleanly).
- Outputs are evaluated at **discrete receptors** (springs, bores) plus a **drawdown raster** for mapping.

## 3. Phases

### Phase 1 — Proof of concept (this repo, initial scope)
A reproducible Python pipeline driven by a single config file. Jupyter notebooks for diagnostics. No web UI. Goal: demonstrate the model runs, gives sensible drawdowns at springs/bores, and the three impact layers can be reported.

### Phase 2 — Productionisation (later)
- **Backend:** FastAPI service that accepts a scenario definition (new bore location + rate) and returns drawdown at receptors + a drawdown raster.
- **Frontend:** Web map (Leaflet / MapLibre) where a regulator clicks a proposed bore location, sets a pumping rate, and sees the impact on springs and neighbouring bores.
- **Caching:** the existing-bores scenario (Scenario A) is invariant within an assessment cycle and should be cached. Only Scenario C is recomputed per request.

Phase 2 is **out of scope for the POC** but the POC's module boundaries should anticipate it.

## 4. Test case: Precipice Sandstone

- Aquifer: Precipice Sandstone (Surat Basin / GAB, Queensland).
- Treated as a **single confined layer** across the formation extent, **unconfined in the outcrop area** (handled as a model property zone, not by adding a layer).
- Calibrated K and Ss are inherited from a parent regional model and resampled onto the AEM zonation grid.

## 5. Inputs (user-supplied)

Place these in `data/raw/`. The pipeline reads from there and writes processed inputs to `data/processed/`.

| Input | Format | Notes |
|---|---|---|
| Formation extent | Shapefile (polygon) | Defines the active model domain. |
| Outcrop area | Shapefile (polygon) | Subset of the formation extent; treated as unconfined / recharge zone. |
| Spring locations | Shapefile (point) | Receptors for drawdown reporting. Attribute table should include a unique `spring_id` and ideally a name. |
| Other water bores (receptors) | Shapefile or CSV (point) | Receptors for drawdown reporting at neighbouring users. |
| Existing licensed extractions | CSV: `bore_id, x, y, rate_m3_per_day` | Treated as **steady-state pumping**. CRS must match the project CRS or be specified. |
| Proposed new bore | CSV or single row in config: `bore_id, x, y, rate_m3_per_day` | The thing being assessed. |
| Hydraulic conductivity (K) | Raster (GeoTIFF), m/d | Calibrated, spatially variable. |
| Specific storage (Ss) | Raster (GeoTIFF), 1/m | Calibrated, spatially variable. |
| Recharge | Raster (GeoTIFF), m/d | Spatially variable, at same resolution as K/Ss per user. Applied only over the outcrop area. |

A single `config.yaml` records file paths, project CRS, aquifer thickness (or a thickness raster if available), regional gradient / far-field head, and run options.

## 6. Workflow

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ Ingest & validate│──▶│ Build AEM grid & │──▶│  Run scenarios   │
│ (shp, csv, tif)  │   │ resample params  │   │   A and C (TTim) │
└──────────────────┘   └──────────────────┘   └──────────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │ Sample drawdown at     │
                                          │ receptors @ 10/50/100y │
                                          └────────────────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │ Combine via            │
                                          │ superposition (B=A+C)  │
                                          └────────────────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │ Reports + maps + CSV   │
                                          └────────────────────────┘
```

### 6.1 Ingest & validate (`src/io_layer.py`)
- Load all shapefiles and rasters with `geopandas` / `rasterio`.
- Reproject everything to a single project CRS (defined in `config.yaml`, typically a projected metric CRS — e.g. GDA2020 / MGA Zone 55 or 56 for the Precipice).
- Validate: existing bores fall inside formation extent; receptor points fall inside formation extent; rasters cover the formation extent; K, Ss, recharge rasters are co-registered.
- Emit a validation report (`reports/validation.md`).

### 6.2 Build the MODFLOW grid and resample arrays (`src/grid.py`)
- Construct a regular structured grid (DIS package) covering the formation extent **plus a buffer** to push the model boundary far from any pumping bore. Buffer width is configurable; a default of 1–2× the characteristic length scale `L = sqrt(T·t / S)` evaluated at t=100 yr is reasonable.
- Cell size is configurable (default 1 km for the POC; refine in zones around bores and receptors in Phase 2 via a quadtree or local refinement if needed).
- Active/inactive cells (IDOMAIN) are set from the formation extent polygon: cells inside the formation are active, cells in the buffer are active too (so stress can propagate), cells outside the buffer are inactive.
- Resample the user-supplied rasters onto the grid using area-weighted resampling (`rasterio.warp.reproject` with `Resampling.average`):
  - K → `npf.k` array (m/d)
  - Ss → `sto.ss` array (1/m)
  - Recharge → `rch.recharge` array (m/d), masked to outcrop cells (zero elsewhere)
- Identify cells containing receptors (springs, observation bores) and pumping bores by point-in-cell lookup. Store these as `(row, col)` pairs in `data/processed/receptors.parquet` and `data/processed/wells.parquet`.
- Persist the grid definition (origin, cell size, nrow, ncol, CRS) to `data/processed/grid.json` so heads rasters can be written with the correct georeferencing later.

> The grid here is a real finite-difference grid — unlike the AEM zonation grid in earlier drafts, this **is** the model's spatial discretisation. K, Ss, and recharge enter MF6 as per-cell arrays.

### 6.3 Build the MODFLOW 6 simulation (`src/model_builder.py`)
- Use FloPy to construct an `MFSimulation` containing one `GroundwaterFlowModel` per scenario. Two simulations total: one for Scenario A, one for Scenario C.
- Packages:
  - **DIS** — structured grid from §6.2 (1 layer, nrow, ncol, top/bot from config or thickness raster).
  - **NPF** — `icelltype=0` (confined), per-cell `k` from the resampled raster.
  - **STO** — `iconvert=0` (confined), per-cell `ss` from the resampled raster, `transient=True` for the stress period.
  - **IC** — initial heads from a **separate steady-state pre-run** (see below). Same IC is used for both Scenario A and Scenario C.
  - **CHD** or no-flow on the outer buffer boundary — configurable. CHD with regional gradient is more realistic; no-flow is the conservative default if no gradient info is provided.
  - **RCH** — per-cell recharge over outcrop cells (Scenario A only by default; Scenario C inherits the same recharge so it cancels in superposition).
  - **WEL** — wells. Scenario A: every existing bore. Scenario C: only the proposed bore. All with constant rates over the stress period.
  - **OC** — save heads at 10, 50, and 100 years.
  - **IMS** — solver. Default `complexity="MODERATE"`; bump to `COMPLEX` only if convergence issues arise.
- **Steady-state pre-run** (run once, results cached): same model with no wells, recharge active, transient off → produces the pre-development steady-state head field used as IC for the transient runs.
- Time discretisation: a single 100-year stress period with geometric time steps (e.g. `nstp=30`, `tsmult=1.2`) so output at 10, 50, 100 yr is well-resolved.
- Imports for reference:
```python
import flopy
from flopy.mf6 import (
    MFSimulation, ModflowGwf, ModflowTdis, ModflowIms,
    ModflowGwfdis, ModflowGwfic, ModflowGwfnpf, ModflowGwfsto,
    ModflowGwfchd, ModflowGwfrch, ModflowGwfwel, ModflowGwfoc,
)
```

### 6.4 Run scenarios and sample receptors (`src/scenarios.py`)
- Invoke `mf6` for each scenario via FloPy's `sim.run_simulation()`. The binary is on PATH inside the container.
- Read transient heads from the `.hds` binary using `flopy.utils.HeadFile`. Compute drawdown as `s(t) = h_initial − h(t)` per cell.
- Evaluate drawdown at:
  - Each spring location → cell lookup from §6.2
  - Each receptor bore location → cell lookup from §6.2
  - The full grid (drawdown raster) at each output time
- Times: `t = [10, 50, 100]` years.
- Persist:
  - `outputs/scenario_A_receptors.csv` — drawdown at every receptor × time
  - `outputs/scenario_C_receptors.csv`
  - `outputs/scenario_A_drawdown_{t}y.tif` rasters (using the grid georeferencing from §6.2)
  - `outputs/scenario_C_drawdown_{t}y.tif` rasters

### 6.5 Combine via superposition (`src/superposition.py`)
- `Scenario B = A + C`, computed in pandas/numpy. **Do not re-run TTim.**
- Produce the three reporting layers per receptor and per time:
  - `s_approved` (= Scenario A) — currently approved impact
  - `s_total` (= A + C) — total impact under the proposed scenario
  - `s_additional` (= C) — additional impact from the new bore alone
- Same three layers as rasters.

### 6.6 Reporting (`src/reporting.py`)
- A markdown report (`reports/impact_assessment.md`) with:
  - Run metadata (config hash, library versions, run timestamp)
  - Tables of drawdown at springs and receptor bores at each timeframe, all three layers
  - Maps (PNG) of `s_approved`, `s_total`, `s_additional` at each timeframe
  - Top-N most-impacted springs and bores
- A machine-readable bundle: `outputs/impact_assessment.json` with the same data, intended to be the API response shape in Phase 2.

## 7. Project structure

```
.
├── CLAUDE.md                     # this file
├── README.md                     # human-facing quickstart
├── Dockerfile                    # pinned MF6 + Python image
├── docker-compose.yml            # mounts data/ and outputs/, runs the CLI
├── pyproject.toml                # deps + tool config
├── config.yaml                   # run-time configuration
├── data/
│   ├── raw/                      # user-supplied inputs (gitignored)
│   └── processed/                # grid, resampled arrays, receptor cell lookups
├── src/
│   ├── __init__.py
│   ├── io_layer.py               # load + validate inputs
│   ├── grid.py                   # MODFLOW grid + raster resampling
│   ├── model_builder.py          # FloPy / MF6 simulation construction
│   ├── scenarios.py              # run A and C, sample receptors
│   ├── superposition.py          # combine to A+C; emit reporting layers
│   ├── reporting.py              # markdown + maps + JSON bundle
│   └── cli.py                    # `python -m src.cli run --config config.yaml`
├── notebooks/
│   ├── 01_inputs_check.ipynb     # eyeball shapefiles + rasters
│   ├── 02_grid_review.ipynb      # validate grid extent, resampled K/Ss/recharge
│   ├── 03_model_sanity.ipynb     # head field, mass balance, Theis comparison
│   └── 04_results_review.ipynb   # interactive map of impacts
├── tests/
│   ├── test_io_layer.py
│   ├── test_grid.py
│   ├── test_superposition.py     # synthetic linearity test (A+C ≈ B from a combined run)
│   ├── test_theis.py             # single-well drawdown vs analytical Theis solution
│   └── test_end_to_end.py        # small synthetic case
├── outputs/                      # gitignored
└── reports/                      # gitignored
```

## 8. Configuration (`config.yaml` shape)

```yaml
project:
  name: "precipice_poc"
  crs: "EPSG:7855"          # GDA2020 / MGA Zone 55 — confirm for the Precipice extent

inputs:
  formation_extent: "data/raw/precipice_extent.shp"
  outcrop:          "data/raw/precipice_outcrop.shp"
  springs:          "data/raw/springs.shp"
  receptor_bores:   "data/raw/receptor_bores.shp"
  existing_bores:   "data/raw/existing_bores.csv"
  proposed_bore:    "data/raw/proposed_bore.csv"
  k_raster:         "data/raw/K.tif"
  ss_raster:        "data/raw/Ss.tif"
  recharge_raster:  "data/raw/recharge.tif"

aquifer:
  thickness_m: 200          # or "thickness_raster: data/raw/thickness.tif"
  top_elevation_m: 0        # reference top; bottom = top - thickness

grid:
  cell_size_m: 1000         # MODFLOW DIS cell size
  buffer_m: 50000           # extend grid this far beyond formation extent
  boundary_type: "no_flow"  # or "chd_regional_gradient"

time:
  total_years: 100
  nstp: 30                  # number of time steps in the stress period
  tsmult: 1.2               # geometric multiplier
  output_years: [10, 50, 100]

solver:
  complexity: "MODERATE"    # MODERATE | COMPLEX

run:
  scenarios: ["A", "C"]     # B is computed by superposition
  workspace_root: "/tmp/mf6_workspaces"
```

## 9. Dependencies

Python:
- Python ≥ 3.11
- `flopy` (FloPy — Python interface to MODFLOW 6)
- `geopandas`, `shapely`, `pyproj`, `fiona`
- `rasterio`, `rioxarray`, `xarray`
- `numpy`, `pandas`, `scipy`
- `matplotlib`, `contextily` (basemaps for report figures)
- `pyyaml`, `pydantic` (config schema validation)
- `pytest` (tests)
- `jupyterlab` (notebooks)

Native:
- **MODFLOW 6 executable** (`mf6`). Inside the Docker image this is downloaded with `python -m flopy.utils.get_modflow /usr/local/bin` during image build, or fetched from the USGS release page. It must be on PATH so `flopy` can invoke it.

## 10. Key technical notes & gotchas

- **Cell size near pumping wells.** Drawdown in the cell containing a pumping well is mesh-dependent (the well is treated as a point sink withdrawing from a finite cell). For receptor drawdown evaluated more than a few cells from any pumping bore this is fine. For receptors *very* close to a bore (within ~2 cells), expect the result to be biased and consider local refinement or use the analytical Theis correction when reporting.
- **Boundary placement.** A no-flow boundary too close to a pumping bore will over-predict drawdown. The buffer in §6.2 must be large enough that head perturbation at the boundary is negligible at t=100 yr. The notebook `02_grid_review.ipynb` should plot drawdown at the boundary as a sanity check; if it's non-trivial, increase the buffer.
- **Linearity / superposition.** Valid because K and Ss are independent of head, the layer is treated as confined, and BCs are linear. The unit test `tests/test_superposition.py` verifies it on a synthetic case by also running B directly and checking `‖A+C − B‖ < tol`.
- **Steady-state initial condition.** The transient runs use the steady-state head field (with recharge, no pumping) as the initial condition. This means drawdown is measured relative to a pre-development baseline, which is what regulators usually want, **and** it ensures both Scenario A and Scenario C share the same IC so superposition works cleanly. If you instead initialised the transient runs from a steady state that already included existing pumping, Scenario A would give zero drawdown by construction — which is wrong for this use case.
- **Recharge cancels in superposition.** Both Scenario A and Scenario C have the same RCH package. When we compute drawdown as `s = h_initial − h(t)`, the steady-state recharge response is in `h_initial` and `h(t)` and cancels. Drawdown is purely the response to wells. This is desired.
- **Confined layer assumption.** Even in the outcrop, we use `icelltype=0` (confined). True unconfined behaviour would need `icelltype=1` and per-cell saturated thickness updates, which makes the model nonlinear and **breaks superposition**. For a regional Precipice POC, confined is the right call. Document it in the report.
- **Time discretisation.** Geometric time steps (`tsmult > 1`) are essential — early-time drawdown changes fast, late-time slowly. With `nstp=30, tsmult=1.2` over 100 yr, the first step is ~0.04 yr and the last is ~9 yr.
- **MF6 binary on PATH.** Inside Docker, `mf6` is on `/usr/local/bin`. Outside Docker, the user must install it; FloPy provides `python -m flopy.utils.get_modflow`.
- **CRS hygiene.** Project CRS is metric (e.g. GDA2020 / MGA). Reject geographic CRS at ingest. The MODFLOW grid origin and cell size are in project-CRS metres.
- **Determinism.** Pin the MF6 binary version (record it in the report metadata), pin Python deps in `pyproject.toml`, hash the config + input files into the report.
- **Theis sanity test.** A single well in a uniform-K, uniform-Ss aquifer with a far boundary should match the Theis analytical solution at observation points. `tests/test_theis.py` runs this and asserts agreement to within ~5 % at distances > 2 cells. If this fails, the model is wrong before any real data is used.

## 11. Acceptance criteria for the POC

1. The pipeline runs end-to-end inside Docker via `docker compose run app python -m src.cli run --config config.yaml` on the Precipice test data and produces `reports/impact_assessment.md`, the JSON bundle, and the drawdown rasters.
2. The Theis test passes: a single well in a uniform aquifer matches the analytical solution at observation points to within ~5 % at distances > 2 cells.
3. The superposition test passes: a directly-modelled Scenario B agrees with `A + C` to within numerical tolerance on the synthetic test case.
4. Drawdown at every spring and every receptor bore is reported at 10, 50, and 100 years for all three layers (`approved`, `total`, `additional`).
5. The report clearly distinguishes the three impact layers and identifies the most-impacted springs and bores.
6. The report records the MF6 version, Python dependency versions, config hash, and input file hashes for traceability.

## 12. Phase 2 sketch (not for build yet, but informs Phase 1 boundaries)

- **API:** `POST /scenarios` with body `{ proposed_bore: {x, y, rate}, times_years: [...] }` returns the JSON bundle from §6.6. The Scenario A run is precomputed and cached server-side; only Scenario C runs per request.
- **Auth:** regulator login; scenarios saved per user.
- **Frontend:** map with springs + existing bores as layers; click-to-place proposed bore; result panel shows drawdown tables and a difference map; export to PDF.
- **Audit trail:** every scenario request and result is persisted with the input config hash for regulatory defensibility.

---

## Notes for Claude Code

- When implementing, build module-by-module in the order listed in §7, with tests landing alongside each module.
- Don't run TTim on the real data until `tests/test_end_to_end.py` passes on a tiny synthetic case (10 km × 10 km, 3 zones, 2 existing bores, 1 proposed bore) — this catches 90 % of integration bugs without the real-data runtime cost.
- Before generating any code, **read this file in full** and ask me about anything ambiguous in §5 (input schemas) or §10 (technical assumptions). Those are the load-bearing pieces.

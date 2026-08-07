# Water Licence Impact Assessment Tool — GABORA water plan area

*(GABORA = Great Artesian Basin and Other Regional Aquifers, the water
plan area — it is the name of the plan area, not of this tool.)*

## 1. What this is

A regulatory decision-support tool that models the **cumulative drawdown
impact** of licensed water extraction from aquifers in the GABORA water
plan area.
The core question: what is the additional impact of a *proposed* new bore
(or licence trade) on top of the *currently approved* take, evaluated at
springs and neighbouring water bores?

The tool started as a single-aquifer Precipice Sandstone proof of concept
(the original spec is preserved in git history) and is now a working
multi-aquifer web application: a FastAPI backend driving MODFLOW 6 via
FloPy, with a MapLibre frontend where a regulator clicks a proposed bore
location, sets a rate, and gets drawdown tables, maps, an approve/reject
recommendation, and an auditable decision trail.

Receptors of concern:

- **Spring complexes** (points from a shared springs shapefile, grouped by
  `complex_na`; the complex is the regulatory unit of analysis — member
  springs are aggregated by max drawdown)
- **Other water bores** (non-S&D bores from the water-use database)

Assessment timeframes: **10, 50, 100 years** of constant pumping, with the
regulatory threshold (default **0.4 m** — the water-bore trigger, NOT the
0.2 m CSG spring threshold) applied at the final horizon.

## 2. Approach summary

- Single-layer **transient** finite-difference model per aquifer using
  **MODFLOW 6** driven from Python via **FloPy**. The grid, per-cell
  properties, boundaries, and stresses are all inherited from OGIA's UWIR
  2025 regional model (extracted by `scripts/extract_uwir2025.py`).
- **Cached baselines + one MF6 run per request.** Scenario **A** (all
  existing take → `s_approved`) and Scenario **L** (licensed/entitlement
  subset → `s_licensed`) are computed once per aquifer and cached. The
  per-request run depends on `drains.transient_mode`:
  - **`drn` (default)**: transients carry the parent model's rejected-
    recharge drains as real MF6 **DRN** cells (head-dependent, shut off
    below the drain elevation). The request runs Scenario **B** (existing
    + change set) *directly* and `s_additional` is **derived as B − A** —
    the marginal impact of the proposal given existing use. Exact drain
    physics; no fictitious water at the outcrop. Superposition A + C = B
    becomes a QA property that holds wherever no drain changes state
    (`tests/test_drains_transient.py`).
  - **`linearised_ghb` (legacy)**: flowing drains become fixed-head GHBs,
    the system is exactly linear, the request runs Scenario **C** alone
    and `s_total = A + C` by addition. Kept for comparability — it clamps
    outcrop drawdown wherever pumping would dry a drain (the Gubberamunda
    bug this mode replaced).
- **Twin-run drawdown**: every scenario runs the same model with and
  without its wells, and drawdown is `s(t) = h_nopump(t) − h_pump(t)`.
  Anything common to both runs (IC, recharge, boundary effects, IC drift)
  cancels by construction. This — not `h_initial − h(t)` — is what makes
  superposition hold to solver precision in practice; see
  `tests/test_superposition.py`.
- The steady-state pre-run (no pumping, recharge + real drains on) supplies
  the initial head and classifies which drains are flowing.
- Everything runs inside a **Docker/devcontainer** image with the MF6
  binary, geospatial stack, and Python deps pinned.

## 3. Aquifer modules

Each aquifer is an independent module: its own config file, data folder,
model state, and baseline cache. The API bootstraps every module whose
config exists (limit with `GABORA_AQUIFERS=precipice,...`); the landing
page routes to whichever modules booted.

| Module key | Parent-model layer(s) | Config | Data |
|---|---|---|---|
| `precipice` | ILAY 24 | `config.yaml` | `Data/Properties_recharge/`, `Data/Geometry/`, … |
| `hutton` | ILAY 19 + 20 (merged) | `config_hutton.yaml` | `Data/Hutton/` |
| `gubberamunda` | ILAY 7 | `config_gubberamunda.yaml` | `Data/Gubberamunda/` |

The Hutton is split across two parent layers; `grid._merge_layer_rows`
collapses them per cell (transmissivity-weighted kx, summed thickness,
negative-Sy marker preserved).

## 4. Inputs (per module)

Paths live in the module's config. The Precipice names are shown; Hutton
and Gubberamunda mirror the same contract in their folders.

| Input | File | Notes |
|---|---|---|
| Per-cell properties | `properties.csv` | `IROW, ICOL, ILAY, INODE, X, Y, kx, SS, THICKNESS, NTOP, NBOT, IBOUND, OUTCROP`. **This IS the model grid** (1500 m structured); no raster resampling. Negative `SS` values are the parent model's water-table marker — a *dimensionless* outcrop Sy decoded as `Ss = |SS|/thickness` (see §8). |
| Steady-state recharge | `*_SS_recharge.csv` / `recharge_SS.csv` | `INODE, RCH_SS_m_per_day` — OGIA's calibrated field, joined by INODE. Fallback: uniform `recharge_fallback_m_per_day` over the outcrop. |
| Rejected-recharge drains | `riv_cells.csv` | The parent model implements surficial drains as RIV reaches with stage == rbot. Calibrated stage + conductance per cell. Fallback: min-DEM elevation + estimated K·A/b conductance. |
| Boundary GHBs | `ghb_cells.csv` | Parent-model calibrated GHB cells (heads AND conductances) on the truncation faces. Fallback: grid-frame faces with C = K·b and pilot-point heads. |
| Water use | `Data/Water Use/*.csv` | OGIA database: `RN, GIS_LAT/GIS_LNG (EPSG:4283), FORMATION, ML_Aquifer, OGIA_P3, AUTH_REFS, …`. Filtered per module by `FORMATION`. **No licence dates exist** (`YEAR` is the dataset vintage, uniformly 2024) — temporal filtering of take is impossible. |
| Springs | `Data/Springs/*.shp` | Shared layer; springs >1 km from the module's outcrop are dropped at ingest, as are springs with no complex. Springs whose point lands just outside the active domain are sampled at the nearest active cell within `assessment.spring_snap_max_m` (default 3 km) rather than silently reporting no impact. |
| Formation extent / outcrop | shapefiles | Extent may be a polyline (closed via polygonize); outcrop defines the recharge/water-table zone. |
| DEM | `Data/DEM/PCP_DEM.tif` | ⚠ This is the Precipice **base structure surface**, not topography (verified: matches NBOT). Only used as the drain-elevation fallback. |

Bore classifications from the water-use table:

- **Pumping bores (Scenario A)** = every row with positive `ML_Aquifer`
- **Receptor bores** = non-S&D (`OGIA_P3 != Stock_Domestic`)
- **Licensed bores (Scenario L)** = holds an authority number
  (`AUTH_REFS` non-empty) AND non-S&D — the `licensed_filter` config block

## 5. Workflow

```
┌────────────────┐  ┌───────────────────┐  ┌─────────────────────┐
│ Ingest+validate│─▶│ Grid straight from│─▶│ Boundaries: no-flow │
│ (io_layer)     │  │ properties.csv    │  │ pinch-outs + UWIR   │
│                │  │ (grid, Sy decode) │  │ GHBs + anchors      │
└────────────────┘  └───────────────────┘  └──────────┬──────────┘
                                                      ▼
┌────────────────────┐  ┌──────────────────────────────────────┐
│ Transient drains:  │◀─│ Steady-state pre-run: no wells,      │
│ real DRN (default) │  │ recharge + REAL DRN cells → IC head  │
│ or legacy GHB      │  └──────────────────────────────────────┘
└─────────┬──────────┘
          ▼
┌──────────────────────────────────────────────┐
│ Scenarios A, L (cached) and B (per request;  │
│ C in legacy mode), each as a TWIN RUN:       │
│ s = h_nopump − h_pump (twin shared/cached)   │
└─────────────────────┬────────────────────────┘
                      ▼
┌──────────────────────────────────────────────┐
│ Layers: s_additional = B − A (drn mode) or   │
│ s_total = A + C (legacy); s_approved /       │
│ s_licensed / s_additional / s_total per      │
│ complex × year + bores + rasters             │
└─────────────────────┬────────────────────────┘
                      ▼
┌──────────────────────────────────────────────┐
│ API response / UI (tables, bar chart, maps)  │
│ or CLI report (markdown + JSON + figures)    │
└──────────────────────────────────────────────┘
```

Module responsibilities:

- `src/io_layer.py` — load water use / springs / shapefiles, reproject to
  the project CRS, classify bores (pumping / receptor / licensed),
  validation findings.
- `src/grid.py` — `Grid` dataclass built from `properties.csv`
  (`build_grid_from_properties`): multi-layer merge, negative-Sy decode,
  INODE-keyed recharge, sanitisation counters. `synthetic_uniform_grid`
  for analytical tests.
- `src/model_builder.py` — FloPy package assembly (`build_steady_state`,
  `build_transient`) plus boundary helpers: `boundary_ghb_for_config`
  (calibrated file or estimated faces), `anchor_ghb_cells` (weak GHB per
  BC-less active island so the SS matrix isn't singular), CHD variants
  (legacy/fallback).
- `src/drains.py` — drain cells from `riv_cells.csv` (or DEM fallback),
  `linearise_drains` (flowing DRN → fixed-head GHB for the transient
  runs), `count_reversals` QA metric.
- `src/scenarios.py` — `run_steady_state`, `run_scenario` (A / L / C with
  multi-well + trade change sets), twin-run drawdown, receptor sampling,
  per-complex time series, QA metrics (budget discrepancy, boundary
  drawdown, drain reversals).
- `src/superposition.py` — combine receptor tables and rasters into the
  four reporting layers.
- `src/theis.py` — analytical Theis comparison shown beside the numerical
  result.
- `src/reporting.py`, `src/figures.py` — CLI markdown report, JSON bundle,
  diagnostic PNGs.
- `src/api/` — FastAPI app (`app.py`: endpoints, per-module state,
  background jobs; `cache.py`: baseline disk cache; `schemas.py`;
  `decisions.py`: append-only decision audit trail in `var/`).
- `src/cli.py` — `validate`, `run`, `theis`, `serve`.

## 6. API + frontend

- `POST /api/scenarios` — single / multi-well / trade change set → job id;
  `GET /api/scenarios/jobs/{id}` polls. Only the per-request scenario runs
  MF6 (B in drn mode, C in legacy; once when the cached no-pump twin
  matches). Response: per-year
  per-complex `s_approved / s_licensed / s_additional / s_total`, threshold
  classifications (`already_exceeded`, `triggered_by_proposed`), a per-year
  `bores` list (drawdown at every in-domain receptor bore — report-only,
  no threshold classification until the bore trigger criterion is
  confirmed; `s_approved` at an extraction bore includes its own
  cell-averaged drawdown, `s_additional` carries no self-impact), Theis
  comparisons (proposal-only AND cumulative-over-all-existing-bores — the
  current-practice method — using `assessment.theis_T_m2_per_day` /
  `theis_S` when set, else formation averages), QA block, provenance
  hashes.
- `GET /api/baseline` — cached Scenario A (+ licensed layer and bores)
  without a proposal.
- `GET /api/map-data`, `/api/aquifers`, `/api/existing-bores`,
  `/api/spring-series`, drawdown raster PNG endpoints, decision endpoints.
- `GET/POST /api/model-settings` — runtime storage-mode switch (session
  override; the config file sets the boot default). Baselines cache per
  mode, so flipping back to a previously used mode is near-instant; a
  first switch rebuilds in a background thread behind `run_lock` while
  the UI polls. Sidebar "Model settings" control in the scenario UI.
- Module selection: `?aquifer=<key>` or `X-Aquifer` header (middleware
  binds each request; default precipice).
- **Frontend** (`frontend/`, static): `index.html`/`landing.js` — mock
  login + aquifer selector map with per-aquifer stats; `precipice.html`/
  `app.js` — the scenario UI (place bore / multi / trade, stacked bar by
  complex with the licensed split, results table, decision panel);
  `setup.html` — model-setup layer viewer; `scenario.html` — drawdown
  raster maps.
- **Baseline cache** (`outputs/cache/<key>/`): receptors, drawdown
  rasters, per-complex series, the no-pump twin heads, the licensed
  layer, and the receptor-bore tables (A and L).
  Cache key = schema version + a fingerprint of the model source
  files + config + input file hashes, so code or data changes rebuild
  automatically. Bump `CACHE_SCHEMA_VERSION` when the cached shape or
  meaning changes.

## 7. Project structure

```
.
├── CLAUDE.md / README.md
├── Dockerfile / docker-compose.yml / .devcontainer/
├── pyproject.toml
├── config.yaml                    # Precipice module
├── config_hutton.yaml             # Hutton module (ILAY 19+20)
├── config_gubberamunda.yaml       # Gubberamunda module (ILAY 7)
├── Data/                          # user-supplied inputs (see §4)
├── docs/gabora-poc-overview-draft.{docx,md}  # department-facing PoC
│                                  # overview (house report style; .md
│                                  # mirrors the .docx for diffable source)
├── scripts/extract_uwir2025.py    # parent-model extraction (properties,
│                                  # RIV, GHB, recharge exports)
├── src/                           # see §5 module list
├── frontend/                      # static MapLibre UI
├── tests/
│   ├── test_config.py / test_grid.py / test_io_layer.py
│   ├── test_model_builder.py / test_drains.py
│   ├── test_superposition.py      # incl. MF6 A+C=B with drains/GHB/Sy on
│   ├── test_theis.py              # numerical vs analytical
│   └── test_end_to_end.py         # small synthetic case
├── outputs/                       # gitignored; cache/ is the baseline store
├── reports/                       # gitignored; CLI-generated
└── var/                           # gitignored but DURABLE: decision audit
                                   # trail — volume-mount + back up
```

`notebooks/` exists as an empty placeholder (the compose `notebook`
service mounts it for ad-hoc JupyterLab work).

## 8. Key technical notes & gotchas

- **Twin-run drawdown is load-bearing.** `h_initial` is not guaranteed to
  be the exact steady state of the transient system (legacy mode swaps
  DRN → GHB between the pre-run and the transients; numerical drift and
  fallback ICs exist in both modes). The `h_initial − h(t)` formula leaks
  that drift into drawdown; `h_nopump(t) − h_pump(t)` cancels it. Do not
  "simplify" this away.
- **Linearity / superposition.** K and Ss are head-independent, every cell
  is `icelltype=0`, and all BCs except DRN are linear (WEL, RCHA, GHB,
  CHD). In legacy linearised mode the whole system is exactly linear and
  `tests/test_superposition.py` verifies `‖(A+C) − B‖` at solver precision
  with the full machinery on. In drn mode the DRN package is piecewise
  linear: superposition holds wherever no drain changes state and the
  direct B run is never *less* than A + C where drains dry
  (`tests/test_drains_transient.py`) — which is why B is run directly and
  never assembled by addition.
- **Boundary placement** follows the UWIR 2025 parent-model design: every
  natural pinch-out edge is **no-flow** (the boundary audit showed the
  perimeter is a thin fringe — thickness p50 6–19 m vs 44 m interior), and
  **GHBs** sit only on truncation faces where the aquifer continues beyond
  the model frame (`assessment.boundary_mode: "uwir_ghb"`, calibrated
  cells from `ghb_cells.csv` when present). The GHB *head* cancels in
  twin-differenced drawdown — only conductance matters — but it shapes the
  IC and drain states. Every run reports max boundary drawdown as a QA
  metric and the UI warns when it is non-trivial.
- **Outcrop treatment.** True unconfined (`icelltype=1`) is avoided; the
  parent model's devices are reproduced instead: (1) outcrop cells carry
  water-table-scale storage — the properties CSV stores a *negative,
  dimensionless* Sy in the SS column, decoded as `Ss = |SS|/thickness` so
  `Ss·b = Sy` exactly (treating it as a 1/m value would inflate outcrop
  storage by the cell thickness); with `assessment.outcrop_storage:
  "formation_sy"` all water-table cells carry the formation-wide Sy (UWIR
  2025 Table B.2-2). (2) Rejected recharge is simulated with **drains at
  the parent model's calibrated RIV cells** (stage == rbot ⇒ pure drain,
  via `drains.riv_cells_csv`). In drn mode these are real DRN cells in
  every run — head-dependent, shutting off when pumped below their
  elevation, exactly like the parent model — and each scenario reports
  `n_drains_dried` plus `drain_capture_m3d` (captured rejected-recharge /
  spring-baseflow discharge, marginal B − A in the API). In legacy mode
  flowing drains become fixed GHBs and `n_drain_reversals` flags where
  that under-predicts drawdown; the Gubberamunda outcrop (drains on ~100%
  of outcrop cells, C/T ≈ 600) showed this clamps drawdown to ~0 across
  the outcrop — the reason drn mode is the default.
- **Storage conversion** (`assessment.storage_mode`). The parent model
  switches cell storage Ss ↔ Sy as heads cross the cell top
  (desaturation). `"static"` (legacy) never converts: confined cells keep
  elastic Ss even when pumped below their top, over-predicting drawdown
  wherever depressurisation would really unconfine a cell.
  `"convertible"` sets STO `iconvert=1` with `sy` = the formation outcrop
  Sy; MF6's conversion keys off head vs cell top **even with
  `icelltype=0`** (verified against a Newton true-unconfined run), so T
  stays constant and no Newton/dry-cell machinery is needed. Two traps:
  (1) water-table-marked cells must swap their `Ss = Sy/b` decode for a
  real elastic Ss (`grid.ss_elastic`) or the mixed formulation counts the
  yield twice; (2) the convertible outer (Picard) loop does real work, so
  `_make_sim` tightens `outer_dvclose` to 1e-6 — the complexity presets'
  loose value silently leaked ~21% of the budget on a stiff test.
  `tests/test_storage_conversion.py` pins all of this.
- **Anchor GHBs.** With a closed no-flow perimeter, an active island with
  no head-dependent BC has a singular steady-state matrix (MF6 dies with a
  floating overflow). Each such component gets one deliberately weak
  (1 m²/d) GHB at its highest cell — defines the datum, exchanges
  negligible flux, stays linear.
- **Recharge cancels** in the twin-differenced drawdown (both runs carry
  the same RCHA); it matters only for the IC and drain-state
  classification. The recharge multiplier exists purely for sensitivity
  analysis and forces a separate cache slot.
- **s_licensed ≤ s_approved** everywhere (subset of the same linear
  system). The API clamps defensively; a genuine violation would indicate
  a broken run, not rounding.
- **Cell size near pumping wells.** Drawdown in and within ~2 cells of a
  well cell is mesh-dependent (point sink in a finite cell). Receptors
  that close to a proposed well are flagged `mesh_dependent`; the Theis
  column gives the analytical cross-check.
- **Time discretisation.** Stress periods are built so an MF6 timestep
  ends exactly on every output year (`scenarios.build_perioddata`); the
  first `fine_period_years` run in annual steps, the rest in geometric
  blocks (`tsmult`).
- **No licence dates in the water-use data.** `YEAR` is the dataset
  vintage (2024, uniform). "Licensed take" is defined by AUTH_REFS +
  non-S&D, not by date — don't promise temporal filtering.
- **Threshold: 0.4 m** is the water-bore trigger for this tool. The 0.2 m
  figure in UWIR documents is the CSG/petroleum spring threshold — do not
  "correct" it.
- **CRS hygiene.** Each module's project CRS is metric (GDA94 / MGA Z55,
  EPSG:28355). Everything is reprojected at ingest; geographic sources
  (water use, springs) declare their CRS in config.
- **Determinism / provenance.** MF6 version, config + input hashes, cache
  key, and git rev are recorded in the report metadata and the API
  provenance block.

## 9. Dependencies

Python ≥ 3.11 — `flopy`, `geopandas`, `shapely`, `pyproj`, `rasterio`,
`numpy`, `pandas`, `scipy`, `matplotlib`, `pyyaml`, `pydantic`, `fastapi`,
`uvicorn`, `pytest`. Native: the **MODFLOW 6** binary on PATH (installed in
the image / devcontainer via `flopy.utils.get_modflow`).

## 10. Running it

```bash
# Web app (bootstraps every module with a config present; ~15–20 s warm,
# minutes cold while baselines build):
python -m src.cli serve --config config.yaml
GABORA_AQUIFERS=precipice python -m src.cli serve   # limit modules

# Offline pipeline → reports/impact_assessment.md + JSON + figures:
python -m src.cli run --config config.yaml \
  --proposed-x <easting> --proposed-y <northing> --proposed-rate <ML/yr>

python -m src.cli validate --config config.yaml     # ingest checks only
python -m src.cli theis                             # analytical sanity
pytest                                              # full suite
```

## Notes for Claude Code

- `config.yaml` is heavily commented and is the source of truth for the
  current modelling decisions — read it alongside this file.
- The §8 gotchas are load-bearing regulatory/numerical constraints, not
  style preferences. In particular: twin-run drawdown, the negative-Sy
  decode, the DRN→GHB linearisation, and the 0.4 m threshold.
- When a change alters what a cached baseline would produce, bump
  `CACHE_SCHEMA_VERSION` in `src/api/cache.py` (the code fingerprint
  catches `src/` model files automatically, but not data-contract or
  semantic changes).
- Run `pytest` before handing work back; the MF6-backed tests skip
  automatically when the binary is absent.

# GAB Regulatory Advisor Tool 
#V0.01
Interactive water-licence impact tool for Great Artesian Basin aquifers
(Surat Basin, Queensland) — currently the **Precipice Sandstone**,
**Hutton Sandstone**, and **Gubberamunda Sandstone**, each an independent
module behind one landing page. MODFLOW 6 driven from Python via FloPy,
served through a FastAPI backend with a MapLibre frontend for regulator
decision support.

Four drawdown reporting layers, built by linear superposition:

- `s_approved` — Scenario A: all existing extraction (cached)
- `s_licensed` — Scenario L: licensed/entitlement take only, i.e. bores
  holding an authority number, non-S&D (cached subset of A)
- `s_additional` — Scenario C: proposed change set only (per-request)
- `s_total` = A + C, compared against a configurable regulatory threshold
  (default **0.4 m**) at the regulator's chosen time horizon

See `CLAUDE.md` for the full design spec.

## Quickstart — GitHub Codespaces (recommended)

1. Open this repo in a Codespace. The `.devcontainer` config builds the
   pinned image (Python 3.11 + MF6 + geospatial stack).
2. Wait for `postCreateCommand` to finish (`pip install -e .` and the MF6
   binary download).
3. **Launch the dashboard:**
   ```bash
   python -m src.cli serve --config config.yaml
   ```
4. Codespaces will offer a forwarded link for port **8000**. Click it to
   open the UI in a new tab.
5. First boot bootstraps every aquifer module whose config exists —
   steady-state IC + Scenario A and licensed-take baselines each (a few
   minutes cold) — and writes results to `outputs/cache/<key>/`.
   Subsequent boots load in ~15–20 s. Limit modules for a faster boot:
   `GABORA_AQUIFERS=precipice python -m src.cli serve`.

In the UI:

- **Landing page** (`/`) — aquifer selector map (GABORA subareas) with
  per-aquifer extraction stats (licensed vs S&D split) and
  springs-over-threshold counts; click a green polygon to open its module.
- **Scenario page** (`/precipice.html?aquifer=<key>`) — click anywhere on
  the satellite map to place a proposed bore (or multi-bore / trade change
  set), set the rate (ML/year), then **Run scenario**. Bottom panel shows
  an APPROVE / REJECT recommendation, impact-summary tiles, a stacked bar
  chart by spring complex (existing impact split into licensed vs
  S&D/other), and an expandable table including a Theis analytical
  comparison.
- **Model setup** (`/setup.html`) — toggleable layers showing the modelled
  grid, recharge zone, GHB / no-flow boundaries, rejected-recharge drains,
  pumping bores (sized by extraction rate), and spring complexes on
  satellite imagery.
- **Drawdown maps** (`/scenario.html`) — side-by-side cumulative and
  proposed-only drawdown rasters after a scenario has been run, with year
  selector, opacity slider, and click-to-sample.

### Restart cheatsheet

```bash
# Stop with Ctrl+C, then:
git pull
python -m src.cli serve --config config.yaml

# Force a fresh Scenario A baseline rebuild:
rm -rf outputs/cache
python -m src.cli serve --config config.yaml

# Port 8000 already in use:
lsof -ti :8000 | xargs -r kill -9
```

## CLI pipeline (offline reports)

The same code can run end-to-end from the command line, producing
`reports/impact_assessment.md`, the JSON bundle, springs CSVs, and
diagnostic figures:

```bash
python -m src.cli validate --config config.yaml          # ingest check only
python -m src.cli run      --config config.yaml          # full pipeline
python -m src.cli run      --config config.yaml \
  --proposed-x 800000 --proposed-y 7180000 --proposed-rate 1000   # ML/year
python -m src.cli theis                                  # analytical sanity test
```

## Local Docker (no Codespace)

```bash
docker compose build
docker compose run --rm --service-ports api      # dashboard on :8000
docker compose run --rm app python -m src.cli run --config config.yaml
docker compose run --rm --service-ports notebook # JupyterLab on :8888
```

## Configuration knobs

All knobs live in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `assessment.regulatory_threshold_m` | `0.4` | Drawdown trigger threshold (m) |
| `assessment.spring_complex_col` | `complex_na` | DBF column grouping springs into complexes |
| `assessment.recharge_multiplier` | `1.0` | Sensitivity scale on the recharge array |
| `assessment.boundary_mode` | `uwir_ghb` | No-flow pinch-outs + calibrated GHBs on truncation faces (UWIR 2025 design); `no_flow` closes the perimeter entirely |
| `inputs.water_use.licensed_filter` | AUTH_REFS + non-S&D | Defines the licensed-take subset behind `s_licensed` |
| `drains.riv_cells_csv` | parent-model export | Calibrated rejected-recharge drain stages/conductances (DEM fallback when absent) |
| `time.output_years` | `[10, 50, 100]` | Years at which to evaluate drawdown |
| `grid.properties_layer` | `24` (Precipice) | ILAY(s) in the multi-layer source CSV; Hutton uses `[19, 20]`, Gubberamunda `7` |
| `solver.complexity` | `MODERATE` | MF6 IMS preset; bump to `COMPLEX` if convergence is fragile |

The regulator UI also exposes the recharge multiplier and proposed-bore
parameters as live inputs; changing the multiplier triggers a one-time
re-baseline against a new cache slot.

## Data layout

User-supplied inputs live under `Data/` (paths in `config.yaml`):

| Folder | Contents | CRS |
|---|---|---|
| `Data/Geometry/` | Precipice formation extent + outcrop shapefiles | GDA94 / MGA Z55 (EPSG:28355) |
| `Data/Properties_recharge/` | Precipice per-cell properties, recharge, `riv_cells.csv` (calibrated drains), `ghb_cells.csv` (calibrated boundary GHBs) | GDA94 / MGA Z55 |
| `Data/Hutton/`, `Data/Gubberamunda/` | Same contract per module (extent, outcrop, properties, recharge, RIV, GHB) | GDA94 / MGA Z55 |
| `Data/Water Use/` | OGIA water-use database(s): rates, `OGIA_P3` class, `AUTH_REFS` (no licence dates — `YEAR` is the dataset vintage) | GDA94 geographic (EPSG:4283) |
| `Data/Springs/Active springs_POINT_*.shp` | Spring receptor points with `complex_na`, shared across modules | Web Mercator (reprojected on ingest) |
| `Data/DEM/PCP_DEM.tif` | ⚠ Precipice **base** structure surface, not topography — drain-elevation fallback only | GDA94 / MGA Z55 |

Project CRS is **GDA94 / MGA Zone 55**. Everything is reprojected on
ingest. Water-use rates in the OGIA file are read as **ML/year** and
converted internally to m³/d.

## Key implementation choices

- **Twin-run drawdown.** Each scenario runs MF6 twice (with and without
  wells); drawdown = `h_no_pump − h_with_pump`. IC, recharge, and boundary
  effects cancel by construction, isolating the well response.
- **Spring complex is the unit of analysis.** Per-spring drawdowns are
  sampled, then aggregated by `max` within each complex (conservative for
  regulatory purposes) and tagged with the member-spring count.
- **Decision rule.** A scenario triggers REJECT when at the regulatory
  time horizon (last output year) any complex is `triggered_by_proposed`
  — i.e. would be under threshold without the proposal but exceeds with
  it. Complexes that are `already_exceeded` from existing licences
  surface as an advisory note, not grounds for rejection.
- **Boundaries follow the UWIR 2025 parent model.** Natural pinch-out
  edges are no-flow; calibrated GHBs sit only on the truncation faces
  where the aquifer continues beyond the model frame. Rejected recharge
  in the outcrop uses the parent model's calibrated drain cells — real
  drains in the steady-state pre-run, linearised to fixed-head GHBs in
  the transient runs so superposition stays exact.

## Repository layout

```
.
├── CLAUDE.md                  design spec — read first
├── Dockerfile                 Python 3.11 + MF6 + geospatial stack
├── docker-compose.yml
├── .devcontainer/             Codespaces config
├── pyproject.toml
├── config.yaml                Precipice module configuration
├── config_hutton.yaml         Hutton module (ILAY 19+20)
├── config_gubberamunda.yaml   Gubberamunda module (ILAY 7)
├── Data/                      user-supplied inputs (see table above)
├── docs/                      gabora-poc-overview-draft.docx/.md — department-facing PoC overview
├── scripts/                   extract_uwir2025.py — parent-model exports
├── src/
│   ├── cli.py                 typer entry points: run, serve, validate, theis
│   ├── config.py              pydantic Config schema
│   ├── io_layer.py            ingest + validate; bore classification
│   ├── grid.py                build MF6 grid from properties.csv
│   ├── model_builder.py       FloPy package assembly + boundary helpers
│   ├── drains.py              rejected-recharge drains + linearisation
│   ├── scenarios.py           run Scenario A / L / C, sample receptors
│   ├── superposition.py       combine to the four reporting layers
│   ├── theis.py               analytical comparison
│   ├── reporting.py           markdown + JSON impact report
│   ├── figures.py             diagnostic PNGs (domain, K, drawdown maps)
│   └── api/                   FastAPI dashboard
│       ├── app.py             endpoints + per-module lifespan state
│       ├── cache.py           disk cache for the A + L baselines
│       ├── schemas.py         Pydantic request/response models
│       └── decisions.py       append-only decision audit trail (var/)
├── frontend/                  static MapLibre UI
│   ├── index.html  / landing.js    aquifer selector landing page
│   ├── precipice.html / app.js     scenario page (all modules)
│   ├── setup.html  / setup.js      model boundaries + grid map
│   ├── scenario.html / scenario.js drawdown raster maps
│   └── coming-soon.html            placeholder for unmodelled aquifers
├── tests/                     pytest suite
├── var/                       decision audit trail (durable runtime data)
└── notebooks/                 empty placeholder for ad-hoc JupyterLab
```

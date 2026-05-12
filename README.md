# GAB Regulatory Advisor Tool — Precipice Sandstone POC

Interactive water-licence impact tool for the Precipice Sandstone aquifer
(Surat Basin / GAB, Queensland). MODFLOW 6 driven from Python via FloPy,
served through a FastAPI backend with a MapLibre frontend for regulator
decision support.

Three drawdown reporting layers, built by linear superposition:

- `s_approved` — Scenario A: existing licensed extractions only (cached)
- `s_additional` — Scenario C: proposed new bore only (per-request)
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
5. First boot runs the steady-state IC + Scenario A baseline (~5 min) and
   writes the result to `outputs/cache/<key>/`. Subsequent boots load
   instantly.

In the UI:

- **Main page** (`/`) — click anywhere on the satellite map to place a
  proposed bore, set the bore ID and rate (ML/year), then **Run scenario**.
  Bottom panel shows an APPROVE / REJECT recommendation, four
  impact-summary tiles, a stacked bar chart by spring complex, and an
  expandable table including a Theis analytical comparison.
- **Model setup** (`/setup.html`) — toggleable layers showing the modelled
  grid, recharge zone, CHD vs no-flow boundaries, pumping bores, and
  spring complexes on satellite imagery.
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
| `assessment.chd_quadrants` | `["NW", "SE"]` | Which compass quadrants of the active-domain boundary carry CHD; the rest are no-flow |
| `time.output_years` | `[10, 50, 100]` | Years at which to evaluate drawdown |
| `grid.properties_layer` | `24` | Which ILAY in the multi-layer source CSV is the Precipice |
| `solver.complexity` | `MODERATE` | MF6 IMS preset; bump to `COMPLEX` if convergence is fragile |

The regulator UI also exposes the recharge multiplier and proposed-bore
parameters as live inputs; changing the multiplier triggers a one-time
re-baseline against a new cache slot.

## Data layout

User-supplied inputs live under `Data/` (paths in `config.yaml`):

| Folder | Contents | CRS |
|---|---|---|
| `Data/Geometry/` | Formation extent + outcrop shapefiles | GDA94 / MGA Z55 (EPSG:28355) |
| `Data/Properties_recharge/properties.csv` | Per-cell `kx`, `SS`, `rch`, `NTOP`, `NBOT`, `THICKNESS`, `IBOUND`, `OUTCROP` | GDA94 / MGA Z55 |
| `Data/Water Use/WATERUSE_GDA94.csv` | OGIA P3 licensed-extractions database | GDA94 geographic (EPSG:4283) |
| `Data/Springs/Active springs_POINT_*.shp` | Spring receptor points with `complex_na` | Web Mercator (reprojected on ingest) |
| `Data/DEM/PCP_DEM.tif` | Surface DEM | GDA94 / MGA Z55 (currently unused) |

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
- **Boundary CHD** is placed only on the configured quadrants of the
  active-domain edge (defaults `NW + SE`, the deep pinch-out side). The
  outcrop / recharge-inflow side and opposite corner become no-flow.

## Repository layout

```
.
├── CLAUDE.md                  design spec — read first
├── Dockerfile                 Python 3.11 + MF6 + geospatial stack
├── docker-compose.yml
├── .devcontainer/             Codespaces config
├── pyproject.toml
├── config.yaml                run-time configuration
├── Data/                      user-supplied inputs (see table above)
├── src/
│   ├── cli.py                 typer entry points: run, serve, validate, theis
│   ├── config.py              pydantic Config schema
│   ├── io_layer.py            ingest + validate inputs
│   ├── grid.py                build MF6 grid from properties.csv
│   ├── model_builder.py       FloPy package assembly + boundary helpers
│   ├── scenarios.py           run Scenario A / C, sample receptors
│   ├── superposition.py       B = A + C
│   ├── theis.py               analytical comparison
│   ├── reporting.py           markdown + JSON impact report
│   ├── figures.py             diagnostic PNGs (domain, K, drawdown maps)
│   └── api/                   FastAPI dashboard
│       ├── app.py             endpoints + lifespan state
│       ├── cache.py           disk cache for Scenario A
│       └── schemas.py         Pydantic request/response models
├── frontend/                  static MapLibre UI
│   ├── index.html  / app.js   main scenario page
│   ├── setup.html  / setup.js model boundaries + grid map
│   └── scenario.html / scenario.js  drawdown raster maps
├── tests/                     pytest suite
└── notebooks/                 diagnostic notebooks
```

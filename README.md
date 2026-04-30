# Precipice Sandstone — Water Licence Impact Assessment POC

Cumulative-drawdown impact tool for the Precipice Sandstone aquifer
(Surat Basin / GAB, Queensland). MODFLOW 6 driven from Python via FloPy,
with three reporting layers built by superposition:

- `s_approved` — Scenario A: existing licensed extractions only
- `s_total` — Scenario B = A + C
- `s_additional` — Scenario C: proposed new bore only

See `CLAUDE.md` for the full design spec.

## Quickstart — GitHub Codespaces

1. Open this repo in a Codespace. The `.devcontainer` config builds the
   pinned image from `Dockerfile` (Python 3.11 + MF6 + geospatial stack).
2. Wait for `postCreateCommand` to finish (installs the project in editable
   mode and ensures `mf6` is on PATH).
3. Run the pipeline:
   ```bash
   python -m src.cli run --config config.yaml
   ```

## Quickstart — local Docker

```bash
docker compose build
docker compose run --rm app python -m src.cli run --config config.yaml
docker compose run --rm --service-ports notebook   # JupyterLab on :8888
```

## Data layout

User-supplied inputs live under `Data/` (see `config.yaml` for paths):

| Folder | Contents | CRS |
|---|---|---|
| `Data/Geometry/` | Formation extent + outcrop shapefiles | GDA94 / MGA Z55 (EPSG:28355) |
| `Data/Properties_recharge/properties.csv` | Per-cell K, Ss, recharge, top/bot, thickness, IBOUND, outcrop flag | GDA94 / MGA Z55 |
| `Data/Water Use/WATERUSE_GDA94.csv` | OGIA P3 water-use database | GDA94 geographic (EPSG:4283) |
| `Data/DEM/PCP_DEM.tif` | Surface DEM (reference only) | GDA94 / MGA Z55 |
| `Data/Springs/springs.shp` | Spring receptors *(to be added)* | GDA94 / MGA Z55 |

Project CRS is **GDA94 / MGA Zone 55** for the model; water use is reprojected from
GDA94 geographic on ingest.

## Repository layout

```
.
├── CLAUDE.md                 design spec — read first
├── Dockerfile                Python 3.11 + MF6 + geospatial stack
├── docker-compose.yml
├── .devcontainer/            Codespaces config (uses Dockerfile)
├── pyproject.toml
├── config.yaml               run-time configuration
├── Data/                     user-supplied inputs (see table above)
├── src/                      pipeline modules
├── tests/                    pytest suite
└── notebooks/                diagnostic notebooks
```

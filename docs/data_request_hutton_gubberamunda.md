# Data request: Hutton + Gubberamunda screening models

Everything needed to stand up the GABORA screening tool for the Hutton
Sandstone and Gubberamunda Sandstone, mirroring the working Precipice
model. Source: the OGIA 2025 regional model (UWIR 2025), same as the
Precipice exports.

**Layer numbers (2025 model, 35-layer table, report Fig 6-1):**

| Aquifer | Model layer(s) |
|---|---|
| Gubberamunda Sandstone | 7 |
| Hutton Sandstone | 19 (Upper) + 20 (Lower) — export BOTH; the tool merges them |
| (Precipice, for reference) | 24 |

## 1. Per-aquifer files

Place under `Data/Hutton/` and `Data/Gubberamunda/` (structure below).

### 1.1 `properties.csv` — per-cell model export
Same schema as the Precipice export, one row per active cell per layer:

```
ICOL, IROW, ILAY, INODE, IBOUND, NTOP, NBOT, X, Y, THICKNESS, OUTCROP, Depth, kx, SS
```

Requirements (each of these bit us on the Precipice — please check):
- **Include all layers for the aquifer** (Hutton: ILAY 19 AND 20 rows).
- X/Y = cell centres, GDA94 / MGA zone 55 (EPSG:28355), 1500 m grid,
  same IROW/ICOL frame as the regional model (IROW 1 = north).
- INODE unique per row.
- **SS sign convention documented**: negative SS marks water-table
  (outcrop) cells. State explicitly whether the magnitude is the
  formation-wide Sy (dimensionless) or a specific storage (1/m) —
  the Precipice export mixed both and we had to reverse-engineer it.
  Ideally: export Ss and Sy as **separate columns**.
- The `rch` column may be omitted (see 1.2) — but if included, populate
  it (the Precipice export shipped an empty column).

### 1.2 `recharge_SS.csv` — steady-state recharge
Same as `Precipice_L24_SS_recharge.csv`:

```
INODE, X, Y, RCH_SS_m_per_day
```

Units m/day; rows only for cells that actually receive recharge in the
steady state (the exposed outcrop belt).

### 1.3 `outcrop.shp` — outcrop polygon(s)
Shapefile with .dbf/.shx/.prj, any CRS as long as .prj is present.
Should be consistent with the OUTCROP flag in properties.csv.

### 1.4 Springs
If the formation has attributed springs: point shapefile with
`site_no` (unique spring id) and `complex_na` (spring complex name),
.prj included. If no springs are attributed to the aquifer (likely for
Gubberamunda), say so explicitly — the tool then reports at receptor
bores only.

### 1.5 Boundary conditions (from UWIR 2025 report / model files)
- **GHB cell locations** for each layer: Appendix F — Fig F.1-3
  (layer 7), F.1-11 (layer 19), F.1-12 (layer 20). Both aquifers have
  **western heads AND southern conductances** in the parameter tables
  (unlike the Precipice, which is west-only).
- **GHB pilot-point head values** (posterior base) for layers 7, 19,
  20: Appendix G, "GHB heads – layer N" figures (the layer-24
  equivalent was Fig G.1-39). A transcription of parameter name +
  posterior value is enough.
- **Southern GHB conductance values** for layers 7 and 19 (posterior
  base from the G.1-40..70 histogram series), if legible.

## 2. Shared files (one copy serves all aquifers)

### 2.1 Topographic DEM — **still outstanding, also needed for Precipice**
The real ground-surface DEM used by the regional model for drain
elevations (finer than 1500 m; the parent model samples its minimum
per cell). NOTE: the file currently in the repo (`Data/DEM/PCP_DEM.tif`)
is the **structure surface of the Precipice BASE, not topography**
(verified: matches model NBOT, median offset −0.4 m, corr 0.99998) — do
not copy that pattern. Suggested name: `Data/DEM/topo_DEM.tif`.
Interim alternative for drain elevations: `riv_cells.csv` (extracted
from the parent model's RIV package) carries the calibrated surficial
drain stage per cell.

### 2.2 Water use
`Data/Water Use/WATERUSE_GDA94.csv` already covers all formations via
its FORMATION column. Please confirm the **exact FORMATION strings**
used for these aquifers (e.g. "Hutton Sandstone", "Gubberamunda
Sandstone") — the tool filters on exact match. If Upper/Lower Hutton
are separate strings, list both.

### 2.3 Already in the repo (no action)
- `Data/Aquifers/GABORA_units_subareas.shp` (landing-page polygons)

## 3. Folder structure

```
Data/
├── Aquifers/                        # exists — GABORA subareas
├── DEM/
│   ├── topo_DEM.tif                 # NEW — real topography (shared)
│   └── PCP_DEM.tif                  # exists — Precipice structure top
├── Water Use/
│   └── WATERUSE_GDA94.csv           # exists — shared, all formations
├── Properties_recharge/             # exists — Precipice (leave as is)
├── Hutton/
│   ├── properties.csv               # ILAY 19 + 20 rows
│   ├── recharge_SS.csv
│   ├── outcrop.shp (+ .dbf .shx .prj)
│   ├── springs.shp (+ sidecars)     # if attributed springs exist
│   └── ghb_pilot_heads.csv          # layer, parameter, posterior_head_m
│       (or a notes.md transcribing the Appendix G values)
└── Gubberamunda/
    ├── properties.csv               # ILAY 7 rows
    ├── recharge_SS.csv
    ├── outcrop.shp (+ sidecars)
    ├── springs.shp                  # or note "none attributed"
    └── ghb_pilot_heads.csv
```

## 4. Nice-to-have (upgrades the Precipice too)
- Calibrated **DRN conductances** for outcrop drains (any layer).
- Calibrated **GHB conductances** / construction basis (K·A/L inputs).
- Pre-development **potentiometric surface** grids per aquifer.
- Confirmation of the **outcrop Sy** values per formation (syz7,
  syz19, syz20 posterior vs map-annotation discrepancy noted for
  syz24).
```

# Precipice (upgrade files only) - UWIR 2025 model extraction notes

Extracted by scripts/extract_uwir2025.py from the OGIA UWIR 2025 regional
model (MODFLOW-USG) at \\espogia01\scratchHDD\UWIR2025\Base_models\Groundwater\Base
Model layers: [24]. Grid 1500 m, GDA94 / MGA zone 55 (EPSG:28355),
IROW 1 = north. INODE = global USG node number.

- properties.csv: all nodes of the layer(s); same schema as the OGIA
  Precipice export. SS is verbatim from UWIRGen5_usg._ss_cal_adj; negative
  SS marks water-table (outcrop) cells and its magnitude is the
  DIMENSIONLESS formation-wide Sy (divide by cell thickness for a 1/m Ss).
  OUTCROP = 'Y' where SS < 0. Depth = uppermost-model-node top minus cell
  midpoint (OGIA's own export used their topo DEM; differs by < 2 m on
  ~0.2 % of cells). kx in m/day from UWIRGen5_usg._kx.
- recharge_SS.csv: steady-state recharge (the '(steady-state 1995)' arrays,
  SP 337+ of the prediction run) at ALL of the aquifer's recharge nodes.
  NOTE: for the Precipice, OGIA's export kept only 215 of the 547
  water-table cells (filter not reproducible from the model files, rates
  match exactly on those 215) - confirm OGIA's cell filter if it matters.
- ghb_cells.csv: the model's actual GHB cells with CALIBRATED head and
  conductance per cell (supersedes transcribing Appendix F/G figures).
- riv_cells.csv: RIV package cells on these layers (stress period 1). The
  UWIR 2025 model implements surficial drains via RIV (stage == rbot);
  cond is the calibrated drain conductance (m2/day).
- predev_heads.csv: steady-state starting heads (UWIRGen5_usg._sshds) =
  pre-development potentiometric surface.
- outcrop.shp: dissolved 1500 m cells with SS < 0 (union across layers).
- extent.shp: dissolved active (IBOUND=1) cells - the model-derived
  formation extent (stands in for an official OGIA extent polygon).

Validation: the same code regenerates the OGIA Precipice layer-24
properties.csv byte-equivalent numerically (all columns; Depth within 2 m
on 80/37207 cells) and its recharge rates exactly, before anything is
written.

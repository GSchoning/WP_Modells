# A proof-of-concept tool for cumulative impact assessment of water licence applications in the GABORA water plan area

**A proof-of-concept overview** · Version 0.1 (draft) · July 2026 · Document ID: [to be confirmed]

> The work presented in this document provides an update on a component of ongoing work to support the assessment and management of water resource development impacts. It is not a statutory document. Conclusions are subject to further review and changes ahead of any operational deployment and other reporting as needed.

*Citation:* X (2026), A proof-of-concept tool for cumulative impact assessment of water licence applications in the GABORA water plan area. A proof-of-concept overview, Department of Local Government, Water and Volunteers, Queensland Government, Australia. July 2026.

*Contributors:* [to be confirmed]

---

## 1  Introduction

### 1.1  Primary target audience

This overview is prepared for departmental officers involved in water licensing and groundwater assessment, and for technical reviewers of the proof of concept. A basic understanding of Great Artesian Basin (GAB) hydrogeology and of groundwater licensing in Queensland is implied. This paper summarises the approach and its benefits; the full technical detail, configuration and test suite sit with the tool's repository documentation.

### 1.2  Background

When a water licence application is assessed, the material question is not only what the proposed bore will do, but what it will do in addition to the extraction that is already occurring. Numerical groundwater models are not currently run for GAB licence assessments: impacts are estimated by superposition of the Theis analytical solution. That method is fast, but it cannot represent aquifer geometry, spatially variable properties, boundaries or recharge, and the assessment does not generally account for other sources of impact such as stock and domestic (S&D) take alongside licensed extraction.

A calibrated numerical representation of the system does exist. The Office of Groundwater Impact Assessment (OGIA) maintains the regional model that underpins the Underground Water Impact Report (UWIR) for the Surat Cumulative Management Area (OGIA 2025). That model was built to assess the impacts of coal seam gas (CSG) development; because it also simulates non-CSG abstraction, it is a natural fit for the licence assessment problem. What has prevented its direct use in licensing is run time: full regional scenarios are impractical at licensing timeframes. A licensing tool that is consistent with the regional model, rather than parallel to it, also avoids introducing a second and potentially conflicting basis for impact predictions.

### 1.3  Purpose of this paper

This paper describes a proof-of-concept decision-support tool for assessing water licence applications in aquifers of the Great Artesian Basin and other regional aquifers (GABORA) water plan area. The tool predicts the drawdown impact of a proposed bore or licence trade at every spring complex and neighbouring water bore, separates that impact from the impact of currently approved extraction, and compares the result against a regulatory trigger threshold at 10, 50 and 100 years. The paper outlines the approach (section 3), the delivered proof of concept (section 4), its verification (section 5), and its current limitations and suggested next steps (sections 6 and 7).

## 2  The assessment problem

An assessing officer needs three numbers for each receptor: the impact of extraction that is already occurring, the additional impact attributable to the application in front of them, and the combined total against the trigger threshold. The current Theis-based method estimates the additional impact of the application, but without the calibrated spatial detail that makes the number defensible, and without the cumulative context, since other sources of impact such as S&D take are not accounted for. Using the regional model directly would supply both, but would require re-running large scenarios for every application at impractical run times, with the applicant's share attributed by differencing two large and nearly equal results. The tool bypasses the run-time barrier by reconstructing fast single-layer models from the calibrated regional model (section 3.1), giving regional-model fidelity at interactive speed, packaged so that a licensing officer rather than a modeller can run it. It also represents all recorded extraction, including S&D take, and reports its contribution separately from licensed entitlement take.

The benefits described in this paper are therefore realised by combining two properties that have not previously been available together in the licensing context: a fast-running tool, and one that leverages a calibrated groundwater model. Neither property alone is sufficient. A fast analytical method without the calibrated spatial detail cannot represent aquifer geometry, heterogeneity or boundaries, while a calibrated regional model without interactive speed cannot support assessment at licensing timeframes. Compared to the current practice of superposing Theis solutions, this represents a significant improvement in the fidelity, completeness and traceability of the impact estimates behind licensing decisions.

## 3  Approach

### 3.1  Inheritance from the regional model

Each aquifer is represented as a single-layer MODFLOW 6 model (Langevin et al 2017) whose grid, hydraulic properties, recharge, boundary conditions and spring-discharge cells are extracted directly from OGIA's UWIR 2025 regional model. Although that model was built for CSG impact assessment, it simulates non-CSG abstraction as part of the same system, so its calibrated representation transfers naturally to the licensing problem; the single-layer reconstruction is what removes its run-time barrier. The tool does not introduce a new conceptualisation and it has not been separately calibrated; it carries the regional model's calibrated parameters into a faster, single-aquifer frame. Impact predictions are therefore traceable to the same calibrated property fields that support the UWIR, which is the principal basis of the tool's defensibility.

### 3.2  Scenario design

The baseline scenarios, comprising all existing extraction (scenario A) and the licensed entitlement subset of that extraction (scenario L), are computed once per aquifer and cached, together with a no-pumping twin of the same model. Each assessment then costs a single model run: the combined scenario, comprising existing extraction plus the proposed change, is modelled directly against the cached twin, and the applicant's contribution is reported as the difference between the combined and baseline results. This attribution is the marginal impact of the application given all existing use, which is the question the assessment is required to answer. Results return in minutes.

The near-linearity that this design exploits is verified rather than assumed. For the linear configuration the automated test suite confirms that a directly modelled combined scenario matches the sum of separately modelled scenarios to solver precision, with differences of the order of 10-9 m. Where the head-dependent spring discharge described in section 3.4 responds to pumping, the combined scenario is deliberately modelled directly, because simple addition of separate runs would understate the impact there (section 5).

### 3.3  Twin-run drawdown

Every scenario is evaluated as the difference between two otherwise identical model runs, with and without its wells. Components common to both runs, including the initial condition, recharge and boundary influences, cancel by construction, so the reported drawdown is purely the response to pumping. This formulation is robust to imperfections in the initial head field, which are a common and difficult-to-detect error mode in impact modelling.

### 3.4  Representation of the outcrop and springs

Outcrop cells carry water-table-scale storage, and spring discharge and rejected recharge are represented at the parent model's calibrated drain cells as head-dependent drains, exactly as in the parent model: a drain discharges in proportion to the head above its elevation and shuts off when pumping draws the head below it. A proposed bore near the outcrop therefore first captures local spring and baseflow discharge, and drawdown grows once that discharge is exhausted, which is the physically expected sequence. Every assessment reports the number of drain cells the proposal dries and the volume of surface discharge it captures, in ML/year, so the depletion of spring flow and baseflow is quantified alongside drawdown rather than left implicit.

### 3.5  Impact layers and threshold classification

Results are reported per spring complex and per output year in four layers: the impact of all currently approved extraction (approved), the subset of that impact attributable to entitlement holders (licensed), the impact of the proposed change alone (additional), and their combined total. The default trigger threshold is 0.4 m, the water bore threshold; this is deliberately distinct from the 0.2 m spring threshold that applies to CSG assessments under the UWIR. Each complex is classified as already exceeding the threshold from approved take, which is advisory and not attributable to the applicant, or as triggered by the proposal, which is the decision-relevant case. The spring complex is the unit of analysis, and the worst-affected member spring sets the complex's reported drawdown, which is the conservative choice for trigger reporting.

## 4  The proof of concept

### 4.1  Aquifer modules

Three aquifers are currently live as independent modules: the Precipice Sandstone, the Hutton Sandstone and the Gubberamunda Sandstone. Each module carries its own configuration, extracted datasets and cached baseline. Current extraction represented in the modules, from the 2024 OGIA water-use dataset, is summarised in Table 1. In total the three modules represent about 6,700 bores and around 37,500 ML/year of take, of which about 74% is licensed entitlement take and the remainder is S&D use.


**Table 1  Extraction represented in the three aquifer modules (2024 OGIA water-use dataset)**

| Aquifer | Bores | Total take (ML/year) | Licensed take (ML/year) | S&D take (ML/year) |
|---|---|---|---|---|
| Precipice Sandstone | 919 | 13,316 | 11,893 (89 bores) | 1,423 (830 bores) |
| Hutton Sandstone | 4,574 | 15,003 | 9,967 (404 bores) | 5,037 (4,170 bores) |
| Gubberamunda Sandstone | 1,215 | 9,162 | 5,791 (100 bores) | 3,371 (1,115 bores) |

The architecture generalises: standing up a further aquifer requires a data extraction from the parent regional model and one configuration file.

### 4.2  Assessment workflow

The assessing officer works from a web map. A proposed bore is placed by clicking the map, or several bores for a multi-bore application, or a trade in which extraction is removed at a source bore and added at one or more destinations. Rates are entered in ML/year. Results return in minutes as impact tables, a stacked chart per spring complex separating licensed from S&D impact, drawdown maps, and a recommendation against the threshold. Every scenario also reports a Theis analytical estimate (Theis 1935) beside the numerical result, so divergence between the two, arising from heterogeneity, boundaries or storage, is visible to the officer rather than hidden.

### 4.3  Decision tracking and provenance

Every approve or reject decision is recorded as an event in an append-only register, together with the officer's name, a free-text reason, the timestamp, the full scenario change set, the headline results, hashes of the configuration and input datasets, and the model version. Rollbacks are themselves recorded events attributed to a named officer; the current status of each decision (active, rolled back or rejected) is derived by replaying the event history, so nothing is ever rewritten or deleted. Because the full scenario definition travels with each event, any past decision can be re-run and its numbers reproduced exactly from the recorded provenance. The register is stored as durable runtime data, separate from regenerable model outputs, and is intended to support internal review, audit and information-access processes.

In the proof of concept the register is a record of decisions rather than an input to the modelling: extraction approved through the tool is not yet fed back into the cached baseline, which continues to reflect the ingested water-use dataset. The event format already carries the information needed for that integration, which is identified as a next step (section 7).

## 5  Verification

The tool carries an automated test suite of 32 tests that runs on every change. Superposition is verified against a directly modelled combined scenario for the linear configuration, both on an idealised model and with the full boundary and storage machinery active, with agreement at solver precision. A dedicated regression test reproduces the spring-discharge behaviour at the outcrop: a bore pumped inside a drain field must first capture the local discharge and then develop a growing cone of depression, and the directly modelled combined scenario is confirmed to never understate impact relative to the added parts. The numerical engine is verified against the Theis analytical solution for a single bore in a uniform aquifer. End-to-end pipeline tests run on a synthetic case. The engine is MODFLOW 6 (currently version 6.7.0), a United States Geological Survey code in wide international use, invoked unmodified through the FloPy interface (Bakker et al 2016).

## 6  Current scope and limitations

The following bounds the proof of concept and indicates where it is and is not appropriate to rely on:

- Single-layer modules. Each aquifer is modelled alone and inter-aquifer leakage through aquitards is not represented. For assessments where vertical connectivity is material, the regional model remains the appropriate instrument.
- Inherited calibration. The tool carries OGIA's calibrated properties but has not itself been history-matched; predictions should be read as consistent-with-UWIR estimates rather than as an independent calibration.
- Outcrop storage approximation. Water-table storage in the outcrop is represented with a fixed specific yield rather than a full unconfined treatment; this is the parent model's own approximation and is appropriate at the drawdowns of regulatory interest, but very large water-table declines would be less well represented. Spring discharge itself is treated without approximation (head-dependent drains, section 3.4).
- Near-bore accuracy. Drawdown within about two grid cells (about 3 km) of a pumped bore is mesh-dependent; affected receptors are flagged in results, with the Theis estimate as the cross-check.
- Water-use data vintage. The extraction dataset is the 2024 OGIA dataset and carries no licence commencement dates, so take cannot be filtered by approval date; licensed take is defined by authority number and use class.
- Constant-rate scenarios. Extraction is modelled as constant over the assessment period, the standard conservative licensing assumption; time-varying regimes are not yet supported.
- Single parameter realisation. The tool currently evaluates one calibrated parameter set, so predictive uncertainty in the impact metrics is not yet quantified. Ensembles of calibrated parameter sets are now routinely produced during model calibration and uncertainty analysis, and section 7 identifies their use in Monte Carlo simulation as a key next step.
- Prototype security posture. The login is a mock-up for demonstration; operational deployment would require departmental authentication, hosting and records-management integration.
## 7  Suggested next steps

- Departmental review of the assessment logic, including threshold values, horizons and the complex aggregation rule, against policy settings.
- A comparison exercise running a set of historical licence decisions through the tool against the assessments made at the time.
- Engagement with OGIA to confirm the parent-model extraction approach and to obtain calibrated boundary conductances where the tool currently estimates them.
- Production hardening: authentication, hosting, records integration, and a data refresh pathway for each new UWIR and water-use dataset.
- Integration of the decision register with the modelled baseline, so that extraction approved through the tool is reflected in the approved-take layer of subsequent assessments within a licensing cycle. The event format already records the required change sets.
- Development of simple but fast-running models for the other GAB aquifers in the plan area, calibrated or inherited from regional modelling where available, and progressively added to the tool as modules. This would extend the same assessment capability across the plan area and would represent a significant improvement over the superposition of Theis solutions currently used where no regional model product is available.
- Quantification of predictive uncertainty by Monte Carlo simulation. Ensembles of calibrated parameter sets are now routinely obtained as part of model calibration and uncertainty analysis. Running the assessment scenarios across such an ensemble would gauge the uncertainty in the key impact metrics; that uncertainty, together with a quantified risk appetite, would support more informed water management decisions than a single deterministic estimate. The computational cost would be modest: ensemble members are independent and parallelise naturally, and could be run cheaply on transient (spot) cloud computing instances.
## 8  Conclusions

- A working proof of concept demonstrates that licence-application impact assessment can be performed at interactive speed while remaining consistent with OGIA's calibrated regional modelling. The combination of a fast-running tool with a calibrated model is what realises the benefits, and is a significant improvement over the superposition of Theis solutions currently used to support assessments.
- Cached baselines and twin runs reduce an assessment to one model run, with the applicant's contribution reported as the marginal impact over existing use; spring and baseflow discharge responds to pumping exactly as in the parent model, and the volume a proposal captures is itself reported in ML/year.
- Impacts are reported in four layers so that approved, licensed and proposed contributions are visible separately, and threshold exceedances are attributed to the correct cause.
- Every result and decision carries reproducible provenance, supporting the defensibility of decisions informed by the tool.
- Known limitations are bounded and reported by the tool itself; the suggested next steps (section 7) address them in order of consequence.
- The most consequential extensions are fast-running models for the remaining GAB aquifers in the plan area, and Monte Carlo simulation across the routinely produced calibrated parameter ensembles so that impact metrics carry quantified uncertainty for risk-based decision making.
## References

Bakker, M, Post, V, Langevin, CD, Hughes, JD, White, JT, Starn, JJ & Fienen, MN 2016, 'Scripting MODFLOW model development using Python and FloPy', Groundwater, vol. 54, no. 5, pp. 733-739.

Langevin, CD, Hughes, JD, Banta, ER, Niswonger, RG, Panday, S & Provost, AM 2017, Documentation for the MODFLOW 6 Groundwater Flow Model, Techniques and Methods 6-A55, United States Geological Survey, Reston, Virginia.

OGIA 2025, Underground Water Impact Report for the Surat Cumulative Management Area, Office of Groundwater Impact Assessment, Department of Local Government, Water and Volunteers, Queensland Government, Australia.

Theis, CV 1935, 'The relation between the lowering of the piezometric surface and the rate and duration of discharge of a well using ground-water storage', Transactions of the American Geophysical Union, vol. 16, pp. 519-524.


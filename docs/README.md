# EasySewer 2.0 documentation

The public entry point is `from easysewer import Model`. Version 2.0 provides no compatibility or migration support for EasySewer 1.x. Start with delivery scope, installation and the first runnable example. Earlier development batches are kept in a local archive; this directory contains current usage, contracts, limits and maintenance guidance.

Documentation in `docs/` is maintained in English, including titles, prose, tables and example comments. Keep API names, commands and serialized identifiers exact. Immutable historical evidence retains its original content outside this directory.

## Getting started

- [2.0 delivery scope](2.0-delivery.md)
- [Installation](2.0-installation.md)
- [From an empty model to portable results](2.0-first-run.md)
- [Quick Start](2.0-quick-start.md)
- [Known issues and follow-up work](2.0-known-issues.md)

## Modeling and configuration

- [2.0 model and semantic INP editing](2.0-model.md)
- [Candidate 2.0 analysis options and transformations](2.0-options.md)
- [Candidate field inspection](2.0-field-inspection.md)
- [Semantic diagnostic locations](2.0-diagnostic-locations.md)
- [Candidate Model JSON 1.0](2.0-json.md)
- [Scenarios and runtime configuration](2.0-scenarios.md)
- [Candidate record provenance](2.0-provenance.md)
- [Candidate shared resources](2.0-resources.md)
- [Candidate storage and divider nodes](2.0-nodes.md)
- [Candidate pumps and regulators](2.0-regulators.md)
- [Candidate transects, streets and inlets](2.0-surface.md)
- [Candidate reports and project metadata](2.0-project.md)
- [Map labels and profile plots](2.0-display.md)
- [Hydraulic event periods](2.0-events.md)

## Hydrology and water quality

- [Candidate rainfall, catchment and snow model](2.0-hydrology.md)
- [Candidate climate model](2.0-climate.md)
- [Candidate climate data documents](2.0-climate-data.md)
- [Candidate historical rainfall documents](2.0-historical-rainfall.md)
- [Candidate flow, concentration and mass inflows](2.0-inflows.md)
- [Candidate control programs](2.0-controls.md)
- [Candidate pollutants and land uses](2.0-quality.md)
- [Pollutant-unit conversion extensions](2.0-pollutant-units.md)
- [Treatment expressions](2.0-treatment.md)
- [LID controls and deployments](2.0-lid.md)
- [Candidate groundwater domain](2.0-groundwater.md)
- [Candidate 2.0 rainfall-dependent inflows](2.0-rdii.md)

## Execution, files and recovery

- [2.0 backend and session](2.0-backend.md)
- [2.0 Runner and execution records](2.0-runner.md)
- [Files, directories and runtime resources](2.0-resource-management.md)
- [Native file I/O contracts](2.0-native-io.md)
- [2.0 interface files and external resources](2.0-files.md)
- [2.0 external time-series and user rainfall data](2.0-data-files.md)
- [Directory resources (2.0 development)](2.0-directory-resources.md)
- [Candidate 2.0 hotstart state documents](2.0-hotstart.md)
- [Candidate 2.0 RUNOFF and RDII cache documents](2.0-runoff-cache.md)
- [Candidate RUNOFF replay semantics](2.0-runoff-replay.md)
- [Cache production conditions](2.0-cache-conditions.md)
- [Recovering an interrupted local run](2.0-run-recovery.md)
- [Candidate FlexiblePonding backend](2.0-flexible-ponding.md)
- [Complete checkpoint scope (2.0 development)](checkpoint-support.md)
- [Checkpoint session contract (2.0 development)](checkpoint-session.md)
- [Runner checkpoints (2.0 development)](checkpoint-runner.md)
- [Runner checkpoint context (development)](checkpoint-runner-context.md)

## Results

- [Candidate OUT results](2.0-output.md)
- [Candidate RPT documents and tables](2.0-report.md)
- [Complete result archives](2.0-result-archive.md)
- [Candidate result applicability](2.0-result-applicability.md)
- [RunResult archive schemas](2.0-result-schemas.md)

## Development and maintenance

- [2.0 architecture](2.0-architecture.md)
- [Testing and builds](2.0-testing.md)
- [Pure Python distribution](2.0-pure-python.md)
- [Support inventory and evidence scope](2.0-support-matrix.md)
- [Development archive](development-archive.md)

## Data formats

The 15 `*.schema.json` files define data formats used by current code. Their `1.x` names do not refer to the old API. Model, scenario, run-configuration and FlexiblePonding formats are covered in their guides; archive versions are documented in [RunResult archive schemas](2.0-result-schemas.md).

The [publication list](publish.json) explicitly selects files for source releases. The [historical index](development-archive.json) records original-file digests without bundling large evidence files.

#!/usr/bin/env bash
# FHIR Normalizer demo — synthetic FHIR resources → flat per-resource DataFrames.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   fhir_resources       ← synthetic_data_generator (fhir_patients, 28 resources)
#         │
#         ├── patients_flat        ← fhir_resource_normalizer (filter: Patient)
#         ├── observations_flat    ← fhir_resource_normalizer (filter: Observation)
#         ├── claims_flat          ← fhir_resource_normalizer (filter: Claim, Coverage)
#         └── provider_directory   ← fhir_resource_normalizer (filter: Practitioner, Organization)
#
# Pure Python — no external services. Demonstrates how one normalizer
# component is wired multiple times with different resource_types filters,
# splitting a mixed FHIR firehose into purpose-built tables.

set -euo pipefail
PROJECT_DIR="${1:-fhir-normalizer-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator       --auto-install 2>&1 | tail -2
$CLI add fhir_resource_normalizer       --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import FhirResourceNormalizerComponent
__all__ = ["FhirResourceNormalizerComponent"]' > "src/$PKG/components/fhir_resource_normalizer/__init__.py"

# 1) Upstream — synthetic mixed FHIR resources (Patient, Observation,
#    Practitioner, Organization, Coverage, Claim)
mkdir -p "src/$PKG/defs/fhir_resources"
cat > "src/$PKG/defs/fhir_resources/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: fhir_resources
  schema_type: fhir_patients
  row_count: 28
  random_state: 42
  group_name: ingest
EOF

# 2) Patients — flatten only Patient resources, normalize gender M/F → male/female
mkdir -p "src/$PKG/defs/patients_flat"
cat > "src/$PKG/defs/patients_flat/defs.yaml" <<EOF
type: $PKG.components.fhir_resource_normalizer.component.FhirResourceNormalizerComponent
attributes:
  asset_name: patients_flat
  upstream_asset_key: fhir_resources
  resource_column: resource
  resource_types: [Patient]
  value_maps:
    gender:
      M: male
      F: female
      U: unknown
  case_insensitive_map: true
  group_name: healthcare
EOF

# 3) Observations — flatten only Observation resources
mkdir -p "src/$PKG/defs/observations_flat"
cat > "src/$PKG/defs/observations_flat/defs.yaml" <<EOF
type: $PKG.components.fhir_resource_normalizer.component.FhirResourceNormalizerComponent
attributes:
  asset_name: observations_flat
  upstream_asset_key: fhir_resources
  resource_column: resource
  resource_types: [Observation]
  group_name: healthcare
EOF

# 4) Claims — flatten Claim + Coverage for insurance reporting
mkdir -p "src/$PKG/defs/claims_flat"
cat > "src/$PKG/defs/claims_flat/defs.yaml" <<EOF
type: $PKG.components.fhir_resource_normalizer.component.FhirResourceNormalizerComponent
attributes:
  asset_name: claims_flat
  upstream_asset_key: fhir_resources
  resource_column: resource
  resource_types: [Claim, Coverage]
  group_name: billing
EOF

# 5) Provider directory — Practitioner + Organization
mkdir -p "src/$PKG/defs/provider_directory"
cat > "src/$PKG/defs/provider_directory/defs.yaml" <<EOF
type: $PKG.components.fhir_resource_normalizer.component.FhirResourceNormalizerComponent
attributes:
  asset_name: provider_directory
  upstream_asset_key: fhir_resources
  resource_column: resource
  resource_types: [Practitioner, Organization]
  group_name: directory
EOF

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    fhir_resources       ← synthetic_data_generator (fhir_patients, 28 resources)
          │
          ├── patients_flat        ← fhir_resource_normalizer (Patient only)
          ├── observations_flat    ← fhir_resource_normalizer (Observation only)
          ├── claims_flat          ← fhir_resource_normalizer (Claim + Coverage)
          └── provider_directory   ← fhir_resource_normalizer (Practitioner + Organization)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected per cycle (4 cycles for row_count=28):
  Patients         ×4
  Observations     ×8 (HR + temperature each)
  Practitioners    ×4
  Organizations    ×4
  Coverage         ×4
  Claim            ×4
MSG

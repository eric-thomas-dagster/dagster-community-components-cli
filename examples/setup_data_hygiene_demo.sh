#!/usr/bin/env bash
# Data Hygiene Pipeline — exercise 8 utility transforms in a single chain.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   raw_customers          ← synthetic_data_generator (customers schema)
#         │
#         ├── audited      ← audit_columns (adds run_id, materialization_time)
#         ├── schema_check ← schema_validator (asserts required fields)
#         ├── mapped       ← field_mapper (renames messy source columns)
#         ├── canon        ← map_values (canonicalize country codes)
#         ├── masked       ← data_masking (mask emails, redact SSNs)
#         ├── hashed       ← hash (row-fingerprint hash)
#         ├── keyed        ← surrogate_key (stable SK from business key)
#         ├── numbered     ← record_id (monotonic ID with prefix)
#         └── counts       ← count_records (group-by tally)
#
# Story: an ops engineer is preparing a customer dimension from raw CRM
# data. The chain audits, validates, normalizes, masks PII, hashes for
# change-detection, generates surrogate + monotonic IDs, then aggregates.

set -euo pipefail
PROJECT_DIR="${1:-data-hygiene-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy tabulate jsonschema
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 9 components (1 source + 8 utilities)"
$CLI add synthetic_data_generator --auto-install 2>&1 | tail -1
$CLI add audit_columns            --auto-install 2>&1 | tail -1
$CLI add schema_validator         --auto-install 2>&1 | tail -1
$CLI add field_mapper             --auto-install 2>&1 | tail -1
$CLI add map_values               --auto-install 2>&1 | tail -1
$CLI add data_masking             --auto-install 2>&1 | tail -1
$CLI add hash                     --auto-install 2>&1 | tail -1
$CLI add surrogate_key            --auto-install 2>&1 | tail -1
$CLI add record_id                --auto-install 2>&1 | tail -1
$CLI add count_records            --auto-install 2>&1 | tail -1

# __init__.py exports for each installed component
for c in synthetic_data_generator audit_columns schema_validator field_mapper map_values \
         data_masking hash surrogate_key record_id count_records; do
  # Find the class name from the local component
  CLS=$(grep -oE '^class\s+\w+\s*\(' "src/$PKG/components/$c/component.py" | awk '{print $2}' | tr -d '(' | tail -1)
  if [ -n "$CLS" ]; then
    echo "from .component import $CLS" > "src/$PKG/components/$c/__init__.py"
  fi
done

# Remove auto-installed example defs (their asset_names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" \
       "src/$PKG/defs/audit_columns" \
       "src/$PKG/defs/schema_validator" \
       "src/$PKG/defs/field_mapper" \
       "src/$PKG/defs/map_values" \
       "src/$PKG/defs/data_masking" \
       "src/$PKG/defs/hash" \
       "src/$PKG/defs/surrogate_key" \
       "src/$PKG/defs/record_id" \
       "src/$PKG/defs/count_records"

# 1. Synthetic raw CRM customers (50 rows)
mkdir -p "src/$PKG/defs/raw_customers"
cat > "src/$PKG/defs/raw_customers/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: raw_customers
  schema_type: customers
  row_count: 50
  random_state: 42
  group_name: raw
EOF

# 2. audit_columns: add lineage columns
mkdir -p "src/$PKG/defs/audited"
cat > "src/$PKG/defs/audited/defs.yaml" <<EOF
type: $PKG.components.audit_columns.component.AuditColumnsComponent
attributes:
  asset_name: audited
  upstream_asset_key: raw_customers
  include_run_id: true
  include_asset_key: true
  include_materialization_time: true
  static_columns:
    source_system: crm_export
    pipeline_owner: data-platform
  group_name: hygiene
EOF

# 3. schema_validator: ensure required fields present + types right
mkdir -p "src/$PKG/defs/schema_check"
cat > "src/$PKG/defs/schema_check/defs.yaml" <<EOF
type: $PKG.components.schema_validator.component.SchemaValidatorComponent
attributes:
  asset_name: schema_check
  upstream_asset_key: audited
  group_name: hygiene
  json_schema:
    type: object
    required:
      - customer_id
      - email
      - state
    properties:
      customer_id:
        type: string
      email:
        type: string
      state:
        type: string
  on_invalid: tag
EOF

# 4. field_mapper: rename messy CRM columns to canonical names
mkdir -p "src/$PKG/defs/mapped"
cat > "src/$PKG/defs/mapped/defs.yaml" <<EOF
type: $PKG.components.field_mapper.component.FieldMapperComponent
attributes:
  asset_name: mapped
  upstream_asset_key: schema_check
  group_name: hygiene
  mapping:
    customer_id: cust_id
    email: email_address
    state: state_code
  drop_unmapped: false
EOF

# 5. map_values: canonicalize US-state code → state name
mkdir -p "src/$PKG/defs/canon"
cat > "src/$PKG/defs/canon/defs.yaml" <<EOF
type: $PKG.components.map_values.component.MapValuesComponent
attributes:
  asset_name: canon
  upstream_asset_key: mapped
  column: state_code
  mapping:
    CA: California
    NY: New York
    TX: Texas
    FL: Florida
    WA: Washington
    IL: Illinois
    MA: Massachusetts
    GA: Georgia
    CO: Colorado
    AZ: Arizona
    OR: Oregon
    PA: Pennsylvania
  output_column: state_name
  default_value: Other
  group_name: hygiene
EOF

# 6. data_masking: mask email + redact whatever PII fields exist
mkdir -p "src/$PKG/defs/masked"
cat > "src/$PKG/defs/masked/defs.yaml" <<EOF
type: $PKG.components.data_masking.component.DataMaskingComponent
attributes:
  asset_name: masked
  upstream_asset_key: canon
  rules:
    - column: email_address
      method: hash
    - column: cust_id
      method: pseudonymize
  group_name: hygiene
EOF

# 7. hash: row fingerprint for change-detection
mkdir -p "src/$PKG/defs/hashed"
cat > "src/$PKG/defs/hashed/defs.yaml" <<EOF
type: $PKG.components.hash.component.HashComponent
attributes:
  asset_name: hashed
  upstream_asset_key: masked
  columns: [cust_id, state_code, state_name]
  output_column: row_hash
  algorithm: sha256
  group_name: hygiene
EOF

# 8. surrogate_key: stable SK from business key
mkdir -p "src/$PKG/defs/keyed"
cat > "src/$PKG/defs/keyed/defs.yaml" <<EOF
type: $PKG.components.surrogate_key.component.SurrogateKeyComponent
attributes:
  asset_name: keyed
  upstream_asset_key: hashed
  business_key_columns: [cust_id]
  output_column: customer_sk
  method: sha256
  truncate_chars: 16
  group_name: hygiene
EOF

# 9. record_id: monotonic ID with prefix (downstream-friendly)
mkdir -p "src/$PKG/defs/numbered"
cat > "src/$PKG/defs/numbered/defs.yaml" <<EOF
type: $PKG.components.record_id.component.RecordIdComponent
attributes:
  asset_name: numbered
  upstream_asset_key: keyed
  output_column: customer_record_id
  start: 1000
  step: 1
  id_prefix: "CUST-"
  group_name: hygiene
EOF

# 10. count_records: aggregation by country
mkdir -p "src/$PKG/defs/counts"
cat > "src/$PKG/defs/counts/defs.yaml" <<EOF
type: $PKG.components.count_records.component.CountRecordsComponent
attributes:
  asset_name: counts
  upstream_asset_key: numbered
  group_by:
    - state_name
  count_column: customer_count
  include_null_counts: true
  group_name: reporting
EOF

cat <<MSG

>>> Setup complete (100% components — 10 in 9-step chain).

Asset graph (left-to-right):
    raw_customers
      → audited      (audit_columns)
      → schema_check (schema_validator)
      → mapped       (field_mapper)
      → canon        (map_values)
      → masked       (data_masking)
      → hashed       (hash)
      → keyed        (surrogate_key)
      → numbered     (record_id)
      → counts       (count_records)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: 50 source rows flow through 9 transforms; final 'counts' asset
shows row counts grouped by state_name (~6-12 unique US states).
MSG

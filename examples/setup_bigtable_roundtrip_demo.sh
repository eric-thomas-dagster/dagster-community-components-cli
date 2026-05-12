#!/usr/bin/env bash
# Bigtable round-trip — write DataFrame rows to Cloud Bigtable, then read them back.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   Both Bigtable components in one chain:
#     bigtable_writer_asset → bigtable_reader_asset
#
# Asset graph:
#   device_state                 (5 synthetic device rows)
#         │
#         └── device_state_written  ← bigtable_writer_asset
#                                     (writes to demo-instance / demo-table)
#                  │
#                  └── device_state_readback  ← bigtable_reader_asset
#                                                (scans device# prefix)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#
# REQUIRED APIS
#   Cloud Bigtable        https://console.cloud.google.com/apis/library/bigtable.googleapis.com
#   Cloud Bigtable Admin  https://console.cloud.google.com/apis/library/bigtableadmin.googleapis.com
#
# REQUIRED IAM (on the service account)
#   roles/bigtable.user        on the instance
#
# REQUIRED PRE-PROVISIONING (one-time; deleted at end)
#   gcloud bigtable instances create demo-instance \
#     --display-name="Dagster demo" \
#     --cluster-config=id=demo-cluster,zone=us-central1-a,nodes=1,type=SSD \
#     --project=$GCP_PROJECT_ID
#
#   # Create table + column families (via Python since cbt may not be installed):
#   python -c "
#   from google.cloud import bigtable
#   from google.cloud.bigtable import column_family
#   c = bigtable.Client(project='$GCP_PROJECT_ID', admin=True)
#   t = c.instance('demo-instance').table('demo-table')
#   t.create(column_families={'meta': column_family.MaxVersionsGCRule(1),
#                              'metrics': column_family.MaxVersionsGCRule(1)})
#   "
#
# COST while running
#   ~\$0.65/hour for the 1-node instance while it exists. **Delete it
#   when done** (see cleanup at the bottom).

set -euo pipefail
PROJECT_DIR="${1:-bigtable-roundtrip-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi

INSTANCE_ID="${INSTANCE_ID:-demo-instance}"
TABLE_ID="${TABLE_ID:-demo-table}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-bigtable
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator --auto-install 2>&1 | tail -2
$CLI add bigtable_writer_asset    --auto-install 2>&1 | tail -2
$CLI add bigtable_reader_asset    --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import BigtableWriterAssetComponent
__all__ = ["BigtableWriterAssetComponent"]' > "src/$PKG/components/bigtable_writer_asset/__init__.py"
echo 'from .component import BigtableReaderAssetComponent
__all__ = ["BigtableReaderAssetComponent"]' > "src/$PKG/components/bigtable_reader_asset/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/bigtable_writer_asset" "src/$PKG/defs/bigtable_reader_asset"

# 1) Upstream: 10 synthetic sensor readings (sensors schema has sensor_id, timestamp, sensor_type, location, value, unit, status)
mkdir -p "src/$PKG/defs/sensor_readings"
cat > "src/$PKG/defs/sensor_readings/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: sensor_readings
  schema_type: sensors
  row_count: 10
  random_state: 42
  group_name: ingest
EOF

# 2) Writer — row key from sensor_id, column families meta (type/location/status) + metrics (value/unit)
mkdir -p "src/$PKG/defs/sensors_written"
cat > "src/$PKG/defs/sensors_written/defs.yaml" <<EOF
type: $PKG.components.bigtable_writer_asset.component.BigtableWriterAssetComponent
attributes:
  asset_name: sensors_written
  upstream_asset_key: sensor_readings
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  instance_id: "$INSTANCE_ID"
  table_id: "$TABLE_ID"
  row_key_column: sensor_id
  column_family: meta
  column_map:
    value: { family: metrics, qualifier: value }
    unit:  { family: metrics, qualifier: unit }
    sensor_type: { family: meta, qualifier: type }
    location:    { family: meta, qualifier: location }
    status:      { family: meta, qualifier: status }
    timestamp:   { family: meta, qualifier: ts }
  batch_size: 500
  group_name: warehouse
EOF

# 3) Reader
mkdir -p "src/$PKG/defs/sensors_readback"
cat > "src/$PKG/defs/sensors_readback/defs.yaml" <<EOF
type: $PKG.components.bigtable_reader_asset.component.BigtableReaderAssetComponent
attributes:
  asset_name: sensors_readback
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  instance_id: "$INSTANCE_ID"
  table_id: "$TABLE_ID"
  row_key_prefix: "SENS"
  column_families: [meta, metrics]
  limit: 100
  decode_values_as: utf-8
  deps: [sensors_written]
  group_name: ingest
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    sensor_readings           ← synthetic_data_generator (sensors, 10 rows)
          │
          └── sensors_written      ← bigtable_writer_asset
                  │
                  └── sensors_readback  ← bigtable_reader_asset

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

CLEANUP (don't forget — Bigtable bills ~\$0.65/hr per node while running):
    gcloud bigtable instances delete $INSTANCE_ID --project=$GCP_PROJECT_ID --quiet
MSG

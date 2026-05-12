#!/usr/bin/env bash
# Firestore round-trip — write DataFrame rows to Firestore, read back with filter.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   Both Firestore components chained:
#     firestore_writer_asset → firestore_reader_asset
#
# Asset graph:
#   devices                 (5 synthetic device docs)
#         │
#         └── devices_written   ← firestore_writer_asset
#                                 (writes to 'devices' collection)
#                  │
#                  └── devices_active  ← firestore_reader_asset
#                                        (where status == active)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#
# REQUIRED APIS
#   Cloud Firestore  https://console.cloud.google.com/apis/library/firestore.googleapis.com
#
# REQUIRED IAM (on the service account)
#   roles/datastore.user        (Firestore inherits this name from Datastore)
#
# REQUIRED PRE-PROVISIONING (one-time; free)
#   gcloud firestore databases create --location=us-central1 \
#     --type=firestore-native --project=$GCP_PROJECT_ID
#
# COST while running
#   Free at this scale. Firestore Native free tier: 50K reads, 20K writes/day.

set -euo pipefail
PROJECT_DIR="${1:-firestore-roundtrip-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-firestore
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing firestore_writer_asset + firestore_reader_asset"
$CLI add synthetic_data_generator --auto-install 2>&1 | tail -2
$CLI add firestore_writer_asset   --auto-install 2>&1 | tail -2
$CLI add firestore_reader_asset   --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import FirestoreWriterAssetComponent
__all__ = ["FirestoreWriterAssetComponent"]' > "src/$PKG/components/firestore_writer_asset/__init__.py"
echo 'from .component import FirestoreReaderAssetComponent
__all__ = ["FirestoreReaderAssetComponent"]' > "src/$PKG/components/firestore_reader_asset/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/firestore_writer_asset" "src/$PKG/defs/firestore_reader_asset"

# 1) Upstream: 10 synthetic sensor readings
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

# 2) Writer
mkdir -p "src/$PKG/defs/sensors_written"
cat > "src/$PKG/defs/sensors_written/defs.yaml" <<EOF
type: $PKG.components.firestore_writer_asset.component.FirestoreWriterAssetComponent
attributes:
  asset_name: sensors_written
  upstream_asset_key: sensor_readings
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  collection: sensors
  id_column: sensor_id
  write_mode: set
  drop_id_column_from_body: true
  group_name: warehouse
EOF

# 3) Reader (filtered)
mkdir -p "src/$PKG/defs/sensors_normal"
cat > "src/$PKG/defs/sensors_normal/defs.yaml" <<EOF
type: $PKG.components.firestore_reader_asset.component.FirestoreReaderAssetComponent
attributes:
  asset_name: sensors_normal
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  collection: sensors
  where:
    - { field: status, op: "==", value: normal }
  # Note: WHERE + ORDER BY on different fields requires a composite index in Firestore.
  limit: 10
  deps: [sensors_written]
  group_name: ingest
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    sensor_readings      ← synthetic_data_generator (sensors, 10 rows)
          │
          └── sensors_written  ← firestore_writer_asset
                  │
                  └── sensors_normal  ← firestore_reader_asset (where status==normal)
                                        (where status == "active")

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: 5 docs written; 4 read back (3 hq active + 1 dr active).

Cleanup (optional):
    gcloud firestore databases delete (default) --project=$GCP_PROJECT_ID --quiet
MSG

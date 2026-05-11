#!/usr/bin/env bash
# Pub/Sub publish demo — DataFrame rows → Cloud Pub/Sub messages w/ attributes.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   pubsub_publish_asset publishing 5 order events to a topic, with
#   filterable attribute columns (event_type, region) for downstream
#   subscription filters.
#
# Asset graph:
#   order_events            (5 synthetic event rows)
#         │
#         └── events_published  ← pubsub_publish_asset
#                                 (publishes to demo-events topic with
#                                  attributes for filter routing)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#
# REQUIRED APIS
#   Pub/Sub  https://console.cloud.google.com/apis/library/pubsub.googleapis.com
#
# REQUIRED IAM
#   roles/pubsub.publisher  on the topic
#
# PRE-PROVISIONING (one-time)
#   gcloud pubsub topics create demo-events --project=$GCP_PROJECT_ID
#   gcloud pubsub subscriptions create demo-events-sub \
#     --topic=demo-events --project=$GCP_PROJECT_ID
#
# COST while running
#   Free. Pub/Sub free tier: 10 GB/mo combined publish + delivery.

set -euo pipefail
PROJECT_DIR="${1:-pubsub-publish-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
TOPIC="${TOPIC:-demo-events}"

echo ">>> Ensuring topic exists: $TOPIC"
gcloud pubsub topics create "$TOPIC" --project="$GCP_PROJECT_ID" 2>&1 | grep -v "Resource already exists" || true
gcloud pubsub subscriptions create "${TOPIC}-sub" --topic="$TOPIC" --project="$GCP_PROJECT_ID" 2>&1 | grep -v "Resource already exists" || true

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-pubsub
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator --auto-install 2>&1 | tail -2
$CLI add pubsub_publish_asset     --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import PubSubPublishAssetComponent
__all__ = ["PubSubPublishAssetComponent"]' > "src/$PKG/components/pubsub_publish_asset/__init__.py"

# 1) Upstream: synthetic event log
mkdir -p "src/$PKG/defs/events"
cat > "src/$PKG/defs/events/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: events
  schema_type: events
  row_count: 10
  random_state: 42
  group_name: ingest
EOF

# 2) Publish — JSON-serialize each row as the body; expose event_type + device as filter attrs
mkdir -p "src/$PKG/defs/events_published"
cat > "src/$PKG/defs/events_published/defs.yaml" <<EOF
type: $PKG.components.pubsub_publish_asset.component.PubSubPublishAssetComponent
attributes:
  asset_name: events_published
  upstream_asset_key: events
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  topic: "$TOPIC"
  # message_column omitted → entire row JSON-serialized as the body
  attribute_columns: [event_type, device]   # subscribers can filter on these
  flush_batch_size: 100
  group_name: dispatch
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    events                 ← synthetic_data_generator (events, 10 rows)
          │
          └── events_published  ← pubsub_publish_asset
                                  (attrs: event_type, device for filter routing)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify delivery:
    gcloud pubsub subscriptions pull ${TOPIC}-sub \\
      --project=$GCP_PROJECT_ID --limit=10 --auto-ack

Cleanup:
    gcloud pubsub subscriptions delete ${TOPIC}-sub --project=$GCP_PROJECT_ID --quiet
    gcloud pubsub topics delete $TOPIC --project=$GCP_PROJECT_ID --quiet
MSG

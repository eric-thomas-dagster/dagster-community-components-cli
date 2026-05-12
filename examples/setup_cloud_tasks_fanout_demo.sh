#!/usr/bin/env bash
# Cloud Tasks Fan-out — Dagster asset emits N work items, each row becomes
# one HTTP task on a Cloud Tasks queue. The queue dispatches them
# asynchronously to a target URL (here, httpbin.org/post — no real worker
# needed for the demo).
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   cloud_tasks_enqueue_asset: per-row HTTP task creation with optional
#   OIDC auth, schedule_time, body templating, dispatch_deadline.
#
# Asset graph:
#   pending_jobs              (5 synthetic work items: job_id, kind, payload)
#         │
#         └── tasks_enqueued  ← cloud_tasks_enqueue_asset
#                                (POSTs each row as JSON to https://httpbin.org/post
#                                 via the demo-queue Cloud Tasks queue)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id (e.g. servicepulse-490502)
#
# REQUIRED APIS (enable URL)
#   Cloud Tasks  https://console.cloud.google.com/apis/library/cloudtasks.googleapis.com
#
# REQUIRED IAM (on the service account)
#   roles/cloudtasks.enqueuer   (or roles/cloudtasks.admin to also create the queue)
#
# QUEUE SETUP (one-time, included below for convenience — idempotent-ish)
#   gcloud tasks queues create demo-queue --location=us-central1 \
#     --project=$GCP_PROJECT_ID
#
# COST while running
#   Free. Cloud Tasks bills $0.40 per million tasks above 1M/month free tier.

set -euo pipefail
PROJECT_DIR="${1:-cloud-tasks-fanout-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
LOCATION="${LOCATION:-us-central1}"
QUEUE_ID="${QUEUE_ID:-demo-queue}"

# Try to create the queue (no-op if it exists)
echo ">>> Ensuring Cloud Tasks queue exists: $LOCATION/$QUEUE_ID"
gcloud tasks queues create "$QUEUE_ID" --location="$LOCATION" --project="$GCP_PROJECT_ID" 2>&1 | grep -v "already exists" || true

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-tasks
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator   --auto-install 2>&1 | tail -2
$CLI add cloud_tasks_enqueue_asset  --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import CloudTasksEnqueueAssetComponent
__all__ = ["CloudTasksEnqueueAssetComponent"]' > "src/$PKG/components/cloud_tasks_enqueue_asset/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/cloud_tasks_enqueue_asset"

# 1) Upstream: 10 synthetic events (each becomes one Cloud Tasks job)
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

# 2) Sink: one Cloud Tasks HTTP task per row → httpbin.org
mkdir -p "src/$PKG/defs/tasks_enqueued"
cat > "src/$PKG/defs/tasks_enqueued/defs.yaml" <<EOF
type: $PKG.components.cloud_tasks_enqueue_asset.component.CloudTasksEnqueueAssetComponent
attributes:
  asset_name: tasks_enqueued
  upstream_asset_key: events
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  location: "$LOCATION"
  queue_id: "$QUEUE_ID"
  target_url: https://httpbin.org/post
  http_method: POST
  body_columns: [event_id, event_type, user_id, page]
  group_name: dispatch
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    events                 ← synthetic_data_generator (events, 10 rows)
          │
          └── tasks_enqueued  ← cloud_tasks_enqueue_asset
                              (POSTs each row to https://httpbin.org/post
                               via $LOCATION/$QUEUE_ID)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect (after materialize, tasks are usually delivered within seconds):
    gcloud tasks queues describe $QUEUE_ID --location=$LOCATION --project=$GCP_PROJECT_ID
    gcloud tasks list --queue=$QUEUE_ID --location=$LOCATION --project=$GCP_PROJECT_ID

Cleanup:
    gcloud tasks queues delete $QUEUE_ID --location=$LOCATION --project=$GCP_PROJECT_ID --quiet
MSG

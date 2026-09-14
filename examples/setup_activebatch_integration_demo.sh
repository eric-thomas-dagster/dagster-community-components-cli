#!/usr/bin/env bash
# ActiveBatch Integration end-to-end demo — Dagster wraps Redwood ActiveBatch
# jobs (Windows-native scheduler) as daily-partitioned assets, with ops for
# restart/hold/release/abort/reconcile, and an ActiveBatch <-> Dagster
# inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   ab_eod_settlement       ← daily-partitioned, kinds: python + activebatch
#   ab_regulatory_extract   ← daily-partitioned, kinds: python + activebatch
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full REST API lifecycle on stdout.
# ActiveBatch is Windows-native — no public Docker image ships; the demo
# runs the entire component end-to-end zero-license. Point at a real
# ActiveBatch by setting demo_mode: false + ACTIVEBATCH_USER /
# ACTIVEBATCH_PASSWORD env vars.

set -euo pipefail
PROJECT_DIR="${1:-activebatch-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing activebatch_integration component"
$CLI add activebatch_integration --auto-install 2>&1 | tail -1

echo 'from .component import ActiveBatchIntegrationComponent, ActiveBatchJobSpec, ActiveBatchSourceTableSpec
__all__ = ["ActiveBatchIntegrationComponent", "ActiveBatchJobSpec", "ActiveBatchSourceTableSpec"]' \
  > "src/$PKG/components/activebatch_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/activebatch_integration"

mkdir -p "src/$PKG/defs/activebatch_integration"
cat > "src/$PKG/defs/activebatch_integration/defs.yaml" <<EOF
type: $PKG.components.activebatch_integration.component.ActiveBatchIntegrationComponent

attributes:
  # demo_mode simulates the ActiveBatch REST API on stdout — flip to false to
  # hit a real ActiveBatch server (requires ACTIVEBATCH_USER / ACTIVEBATCH_PASSWORD).
  # ActiveBatch is Windows-native; no public Docker image ships.
  demo_mode: true
  endpoint: "http://activebatch.internal/absvc/api/v1"

  group_name: activebatch_integration
  activebatch_user_env: ACTIVEBATCH_USER
  activebatch_password_env: ACTIVEBATCH_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - object_id: "12345"
      asset_name: ab_eod_settlement
      description: >-
        End-of-day batch settlement. ActiveBatch orchestrates the underlying
        Windows / SQL Server / SSIS processes on WIN-Q-01. Dagster triggers
        via REST with the scheduledDate from the partition key, polls
        through Queued -> Running -> Succeeded, retrieves the instance log,
        emits a completion event.
      plan: DAILY_BATCH
      execution_queue: WIN-Q-01
      application: CORE_BANKING

    - object_id: "12346"
      asset_name: ab_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      plan: DAILY_BATCH
      execution_queue: WIN-Q-01
      application: COMPLIANCE
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole REST API lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for ab_eod_settlement"
uv run dg launch --assets 'ab_eod_settlement' --partition "$PARTITION" 2>&1 | tail -30

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    ab_eod_settlement          (daily partitioned, kinds: python + activebatch)
    ab_regulatory_extract      (daily partitioned, kinds: python + activebatch)

Also shipped:
    Jobs      activebatch_restart_instance / activebatch_hold_instance /
              activebatch_release_instance / activebatch_abort_instance /
              activebatch_reconciliation
    Sensors   activebatch_external_execution_monitor / activebatch_inbound_trigger
    Schedule  activebatch_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'ab_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real ActiveBatch instance (Windows-native — no Docker image):
    Set demo_mode: false in defs.yaml, then:
    export ACTIVEBATCH_USER=svc_dagster
    export ACTIVEBATCH_PASSWORD='<your-password>'
    uv run dg dev

Sister schedulers with the same component shape:
    runmyjobs_integration      Redwood RunMyJobs (same Redwood family)
    controlm_integration       BMC Control-M
MSG

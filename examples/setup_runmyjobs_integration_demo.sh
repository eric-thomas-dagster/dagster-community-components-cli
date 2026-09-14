#!/usr/bin/env bash
# RunMyJobs Integration end-to-end demo — Dagster wraps Redwood RunMyJobs
# (aka SAP Redwood Scheduler) JobDefinitions as daily-partitioned assets,
# with ops for restart/hold/release/kill/reconcile, and a RMJ ↔ Dagster
# inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   rmj_eod_settlement       ← daily-partitioned, kinds: python + runmyjobs
#   rmj_regulatory_extract   ← daily-partitioned, kinds: python + runmyjobs
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full REST API lifecycle on stdout.
# Zero external dependencies. Point at a real RunMyJobs by setting
# demo_mode: false + RUNMYJOBS_USER / RUNMYJOBS_PASSWORD env vars.

set -euo pipefail
PROJECT_DIR="${1:-runmyjobs-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing runmyjobs_integration component"
$CLI add runmyjobs_integration --auto-install 2>&1 | tail -1

echo 'from .component import RunMyJobsIntegrationComponent, RunMyJobsJobSpec, RunMyJobsSourceTableSpec
__all__ = ["RunMyJobsIntegrationComponent", "RunMyJobsJobSpec", "RunMyJobsSourceTableSpec"]' \
  > "src/$PKG/components/runmyjobs_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/runmyjobs_integration"

mkdir -p "src/$PKG/defs/runmyjobs_integration"
cat > "src/$PKG/defs/runmyjobs_integration/defs.yaml" <<EOF
type: $PKG.components.runmyjobs_integration.component.RunMyJobsIntegrationComponent

attributes:
  # demo_mode simulates the REST API on stdout — flip to false to hit
  # a real RunMyJobs server (requires RUNMYJOBS_USER / RUNMYJOBS_PASSWORD).
  demo_mode: true
  endpoint: "https://runmyjobs.internal:8443/RunMyJobs/api-rest"

  group_name: runmyjobs_integration
  runmyjobs_user_env: RUNMYJOBS_USER
  runmyjobs_password_env: RUNMYJOBS_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_definition: EOD_BATCH_SETTLEMENT
      asset_name: rmj_eod_settlement
      description: >-
        End-of-day batch settlement. RunMyJobs orchestrates the underlying
        SAP / mainframe processes. Dagster submits via REST with the
        scheduled date from the partition key.
      application: DAILY_BATCH
      partition_type: CORE_BANKING
      sub_partition_type: SETTLEMENT
      queue: prod_queue

    - job_definition: REGULATORY_EXTRACT
      asset_name: rmj_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      application: DAILY_BATCH
      partition_type: COMPLIANCE
      sub_partition_type: REGULATORY_REPORTING
      queue: compliance_queue

  source_tables:
    - table_name: "CORE_BANKING.SETTLEMENT_LEDGER"
      asset_name: rmj_settlement_table
      produced_by: rmj_eod_settlement
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the full REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for rmj_eod_settlement"
uv run dg launch --assets 'rmj_eod_settlement' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    rmj_eod_settlement          (daily partitioned, kinds: python + runmyjobs)
    rmj_regulatory_extract      (daily partitioned, kinds: python + runmyjobs)
    rmj_settlement_table        (source, kinds: runmyjobs + database)

Also shipped:
    Jobs      runmyjobs_restart_process / runmyjobs_hold_application /
              runmyjobs_release_processes / runmyjobs_kill_process /
              runmyjobs_reconciliation
    Sensors   runmyjobs_external_execution_monitor / runmyjobs_inbound_trigger
    Schedule  runmyjobs_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'rmj_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real RunMyJobs instance:
    Set demo_mode: false in defs.yaml, then:
    export RUNMYJOBS_USER=svc_dagster
    export RUNMYJOBS_PASSWORD='<your-password>'
    uv run dg dev

Note on REST paths:
    RunMyJobs REST paths shift across versions (v6 / v9 / SAP-branded).
    If your instance uses a different prefix, either bake it into
    'endpoint' or fork _execute_runmyjobs in component.py to match.
MSG

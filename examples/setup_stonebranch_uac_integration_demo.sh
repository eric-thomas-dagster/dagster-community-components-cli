#!/usr/bin/env bash
# Stonebranch UAC Integration end-to-end demo — Dagster wraps Stonebranch
# Universal Automation Center Tasks as daily-partitioned assets, with ops for
# rerun/hold/release/cancel/reconcile, and a UAC ↔ Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   sb_eod_settlement       ← daily-partitioned, kinds: python + stonebranch
#   sb_regulatory_extract   ← daily-partitioned, kinds: python + stonebranch
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full UAC REST API lifecycle on stdout.
# Zero external dependencies, zero license required. Point at a real UAC instance
# by setting demo_mode: false + STONEBRANCH_USER / STONEBRANCH_PASSWORD env vars.
#
# Stonebranch publishes a stonebranch/uac-demo Docker image but it requires a
# trial license file from Stonebranch support — no self-serve pull-and-run.
# The demo_mode simulator exercises the same _execute_stonebranch code path
# end-to-end, zero license required.

set -euo pipefail
PROJECT_DIR="${1:-stonebranch-uac-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing stonebranch_uac_integration component"
$CLI add stonebranch_uac_integration --auto-install 2>&1 | tail -1

echo 'from .component import StonebranchUACIntegrationComponent, StonebranchTaskSpec, StonebranchSourceTableSpec
__all__ = ["StonebranchUACIntegrationComponent", "StonebranchTaskSpec", "StonebranchSourceTableSpec"]' \
  > "src/$PKG/components/stonebranch_uac_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/stonebranch_uac_integration"

mkdir -p "src/$PKG/defs/stonebranch_uac_integration"
cat > "src/$PKG/defs/stonebranch_uac_integration/defs.yaml" <<EOF
type: $PKG.components.stonebranch_uac_integration.component.StonebranchUACIntegrationComponent

attributes:
  # demo_mode simulates the UAC REST API on stdout — flip to false to hit
  # a real Stonebranch UAC instance (requires STONEBRANCH_USER / STONEBRANCH_PASSWORD).
  demo_mode: true
  endpoint: "http://localhost:8080/uc"

  group_name: stonebranch_uac_integration
  stonebranch_user_env: STONEBRANCH_USER
  stonebranch_password_env: STONEBRANCH_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - task_name: EOD_BATCH_SETTLEMENT
      asset_name: sb_eod_settlement
      description: >-
        End-of-day batch settlement. Stonebranch UAC Universal Agent runs
        the shell/JCL/script on the target host. Dagster launches the Task
        via ops-task-launch with scheduledTime taken from the partition key.
      workflow: DAILY_BATCH
      application: CORE_BANKING
      agent: ux-agent-prod-01
      run_as: svc_dagster

    - task_name: REGULATORY_EXTRACT
      asset_name: sb_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      workflow: DAILY_BATCH
      application: COMPLIANCE
      agent: ux-agent-prod-02
      run_as: svc_dagster
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole UAC REST lifecycle runs end-to-end
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for sb_eod_settlement"
uv run dg launch --assets 'sb_eod_settlement' --partition "$PARTITION" 2>&1 | tail -25

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    sb_eod_settlement       (daily partitioned, kinds: python + stonebranch)
    sb_regulatory_extract   (daily partitioned, kinds: python + stonebranch)

Also shipped:
    Jobs      stonebranch_rerun_task_instance / stonebranch_hold_task_instance /
              stonebranch_release_task_instance / stonebranch_cancel_task_instance /
              stonebranch_reconciliation
    Sensors   stonebranch_external_execution_monitor / stonebranch_inbound_trigger
    Schedule  stonebranch_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'sb_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule, run rerun/hold/release/cancel ops):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Stonebranch UAC instance:
    Set demo_mode: false in defs.yaml + update endpoint to your UAC host, then:
    export STONEBRANCH_USER=ops.admin
    export STONEBRANCH_PASSWORD='<your-password>'
    uv run dg dev

Migrating from Control-M / running alongside RunMyJobs or IWS?
    Sister components share the same asset/op/sensor/schedule shape:
      controlm_integration        — BMC Control-M Automation API
      runmyjobs_integration       — Redwood RunMyJobs REST
      iws_integration             — IBM Workload Scheduler REST
    One Dagster pane of glass over whichever schedulers own the batch.
MSG

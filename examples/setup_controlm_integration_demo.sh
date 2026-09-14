#!/usr/bin/env bash
# Control-M Integration end-to-end demo — Dagster wraps BMC Control-M jobs
# as daily-partitioned assets, with ops for restart/hold/free/kill/reconcile,
# and a Control-M ↔ Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   controlm_eod_settlement     ← daily-partitioned, kinds: python + control-m
#   controlm_regulatory_extract ← daily-partitioned, kinds: python + control-m
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full Automation API lifecycle on
# stdout. Zero external dependencies. Point at a real Control-M by setting
# demo_mode: false + CONTROLM_USER / CONTROLM_PASSWORD env vars.

set -euo pipefail
PROJECT_DIR="${1:-controlm-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing controlm_integration component"
$CLI add controlm_integration --auto-install 2>&1 | tail -1

echo 'from .component import ControlMIntegrationComponent, ControlMJobSpec, ControlMSourceTableSpec
__all__ = ["ControlMIntegrationComponent", "ControlMJobSpec", "ControlMSourceTableSpec"]' \
  > "src/$PKG/components/controlm_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/controlm_integration"

mkdir -p "src/$PKG/defs/controlm_integration"
cat > "src/$PKG/defs/controlm_integration/defs.yaml" <<EOF
type: $PKG.components.controlm_integration.component.ControlMIntegrationComponent

attributes:
  # demo_mode simulates the Automation API on stdout — flip to false to hit
  # a real Control-M server (requires CONTROLM_USER / CONTROLM_PASSWORD).
  demo_mode: true
  endpoint: "https://controlm.internal:8443/automation-api"

  group_name: controlm_integration
  controlm_user_env: CONTROLM_USER
  controlm_password_env: CONTROLM_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_name: EOD_BATCH_SETTLEMENT
      asset_name: controlm_eod_settlement
      description: >-
        End-of-day batch settlement. Control-M for z/OS manages the JCL
        submission and JES scheduling. Dagster triggers via Automation API
        with ODATE from the partition key.
      folder: DAILY_BATCH
      application: CORE_BANKING
      sub_application: SETTLEMENT
      host: ctm-agent-prod-01

    - job_name: REGULATORY_EXTRACT
      asset_name: controlm_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      folder: DAILY_BATCH
      application: COMPLIANCE
      sub_application: REGULATORY_REPORTING
      host: ctm-agent-prod-02

  source_tables:
    - table_name: "CORE_BANKING.SETTLEMENT_LEDGER"
      asset_name: controlm_settlement_table
      produced_by: controlm_eod_settlement
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole Automation API lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for controlm_eod_settlement"
uv run dg launch --assets 'controlm_eod_settlement' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    controlm_eod_settlement       (daily partitioned, kinds: python + control-m)
    controlm_regulatory_extract   (daily partitioned, kinds: python + control-m)
    controlm_settlement_table     (source, kinds: control-m + database)

Also shipped:
    Jobs      controlm_restart_job / controlm_hold_folder / controlm_free_folder /
              controlm_kill_job / controlm_reconciliation
    Sensors   controlm_external_execution_monitor / controlm_inbound_trigger
    Schedule  controlm_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'controlm_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Control-M:
    Set demo_mode: false in defs.yaml, then:
    export CONTROLM_USER=svc_dagster
    export CONTROLM_PASSWORD='<your-password>'
    uv run dg dev
MSG

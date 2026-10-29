#!/usr/bin/env bash
# OpCon Integration end-to-end demo — Dagster wraps SMA Technologies /
# Continuous OpCon daily jobs as daily-partitioned assets, with ops for
# force-restart/hold/release/cancel/reconcile, and an OpCon <-> Dagster
# inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   opcon_eod_ledger_close     ← daily-partitioned, kinds: python + opcon
#   opcon_regulatory_extract   ← daily-partitioned, kinds: python + opcon
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the whole OpCon REST API lifecycle
# (token exchange + scheduleactions + dailyjobs status polling) on stdout.
# Zero external dependencies. OpCon is on-prem workload-automation software
# with no public Docker image or license-free trial, so the simulator is the
# only zero-license way to exercise the full surface. Point at a real OpCon
# instance by setting demo_mode: false + OPCON_USER / OPCON_PASSWORD env vars.

set -euo pipefail
PROJECT_DIR="${1:-opcon-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing opcon_integration component"
$CLI add opcon_integration --auto-install 2>&1 | tail -1

echo 'from .component import OpConIntegrationComponent, OpConJobSpec, OpConSourceTableSpec
__all__ = ["OpConIntegrationComponent", "OpConJobSpec", "OpConSourceTableSpec"]' \
  > "src/$PKG/components/opcon_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/opcon_integration"

mkdir -p "src/$PKG/defs/opcon_integration"
cat > "src/$PKG/defs/opcon_integration/defs.yaml" <<EOF
type: $PKG.components.opcon_integration.component.OpConIntegrationComponent

attributes:
  # demo_mode simulates the OpCon REST API on stdout — flip to false to hit
  # a real OpCon server (requires OPCON_USER / OPCON_PASSWORD).
  demo_mode: true
  endpoint: "https://opcon.internal"

  group_name: opcon_integration
  opcon_user_env: OPCON_USER
  opcon_password_env: OPCON_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_name: EOD_LEDGER_CLOSE
      asset_name: opcon_eod_ledger_close
      description: >-
        End-of-day ledger close. OpCon orchestrates the underlying
        mainframe/AS400/Windows steps. Dagster force-starts via REST with
        the scheduled date from the partition key.
      schedule_name: DAILY_FINANCE
      machine: AS400-PROD-01
      force_restart: true

    - job_name: REGULATORY_EXTRACT
      asset_name: opcon_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      schedule_name: DAILY_FINANCE
      machine: AS400-PROD-01
      force_restart: true

  source_tables:
    - table_name: "FINANCE.GL_LEDGER"
      asset_name: opcon_gl_ledger_table
      produced_by: opcon_eod_ledger_close
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole OpCon REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for opcon_eod_ledger_close"
uv run dg launch --assets 'opcon_eod_ledger_close' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    opcon_eod_ledger_close     (daily partitioned, kinds: python + opcon)
    opcon_regulatory_extract   (daily partitioned, kinds: python + opcon)
    opcon_gl_ledger_table      (source, kinds: opcon + database)

Also shipped:
    Jobs      opcon_force_restart_job / opcon_hold_job / opcon_release_job /
              opcon_cancel_job / opcon_reconciliation
    Sensors   opcon_external_execution_monitor / opcon_inbound_trigger
    Schedule  opcon_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'opcon_eod_ledger_close' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real OpCon instance:
    Set demo_mode: false in defs.yaml, then:
    export OPCON_USER=svc_dagster
    export OPCON_PASSWORD='<your-password>'
    uv run dg dev

    Note: OpCon is on-prem workload-automation software with no public
    Docker image or license-free trial. Production wiring targets a real
    installed OpCon instance. Verify JOB:HOLD/JOB:RELEASE/JOB:CANCEL action
    names (the hold/release/cancel ops) against your instance's own Swagger
    UI (/api-doc/index.html) -- only JOB:START/JOB:FORCERESTART were
    individually confirmed from public docs.
MSG

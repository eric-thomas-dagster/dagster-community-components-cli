#!/usr/bin/env bash
# JAMS Integration end-to-end demo — Dagster wraps Fortra JAMS Scheduler jobs
# as daily-partitioned assets, with ops for restart/hold/release/cancel/reconcile,
# and a JAMS <-> Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   jams_eod_settlement       ← daily-partitioned, kinds: python + jams
#   jams_regulatory_extract   ← daily-partitioned, kinds: python + jams
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the whole JAMS REST API lifecycle on
# stdout. Zero external dependencies. Fortra JAMS runs on Windows Server with
# no public Docker image, so the simulator is the only zero-license way to
# exercise the full surface. Point at a real JAMS instance by setting
# demo_mode: false + JAMS_USER / JAMS_PASSWORD env vars.

set -euo pipefail
PROJECT_DIR="${1:-jams-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing jams_integration component"
$CLI add jams_integration --auto-install 2>&1 | tail -1

echo 'from .component import JAMSIntegrationComponent, JAMSJobSpec, JAMSSourceTableSpec
__all__ = ["JAMSIntegrationComponent", "JAMSJobSpec", "JAMSSourceTableSpec"]' \
  > "src/$PKG/components/jams_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/jams_integration"

mkdir -p "src/$PKG/defs/jams_integration"
cat > "src/$PKG/defs/jams_integration/defs.yaml" <<EOF
type: $PKG.components.jams_integration.component.JAMSIntegrationComponent

attributes:
  # demo_mode simulates the JAMS REST API on stdout — flip to false to hit
  # a real JAMS server (requires JAMS_USER / JAMS_PASSWORD).
  demo_mode: true
  endpoint: "https://jams.internal/jams/rest/api"

  group_name: jams_integration
  jams_user_env: JAMS_USER
  jams_password_env: JAMS_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_name: EOD_BATCH_SETTLEMENT
      asset_name: jams_eod_settlement
      description: >-
        End-of-day batch settlement. JAMS orchestrates the underlying
        Windows / SQL Server Agent / PowerShell steps on the Windows agent.
        Dagster submits via REST with SCHEDULED_TIME from the partition key.
      folder: DAILY_BATCH
      agent: WIN-AGT-01
      application: CORE_BANKING

    - job_name: REGULATORY_EXTRACT
      asset_name: jams_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      folder: DAILY_BATCH
      agent: WIN-AGT-01
      application: COMPLIANCE

  source_tables:
    - table_name: "CORE_BANKING.SETTLEMENT_LEDGER"
      asset_name: jams_settlement_table
      produced_by: jams_eod_settlement
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole JAMS REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for jams_eod_settlement"
uv run dg launch --assets 'jams_eod_settlement' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    jams_eod_settlement           (daily partitioned, kinds: python + jams)
    jams_regulatory_extract       (daily partitioned, kinds: python + jams)
    jams_settlement_table         (source, kinds: jams + database)

Also shipped:
    Jobs      jams_restart_entry / jams_hold_entry / jams_release_entry /
              jams_cancel_entry / jams_reconciliation
    Sensors   jams_external_execution_monitor / jams_inbound_trigger
    Schedule  jams_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'jams_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real JAMS instance:
    Set demo_mode: false in defs.yaml, then:
    export JAMS_USER=svc_dagster
    export JAMS_PASSWORD='<your-password>'
    uv run dg dev

    Note: Fortra JAMS runs on Windows Server with a SQL Server metadata
    store — no public Docker image. Production wiring targets your real
    installed JAMS instance.
MSG

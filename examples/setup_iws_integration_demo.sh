#!/usr/bin/env bash
# IBM Workload Scheduler (IWS / TWS) Integration end-to-end demo — Dagster
# wraps IWS jobs as daily-partitioned assets, with ops for rerun/hold/
# release/kill/reconcile, and an IWS <-> Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Hybrid deployment target: banking / insurance / government shops where
# IWS owns EOD settlement + regulatory extracts + mainframe JCL, and
# Dagster owns the cloud / analytics / AI pipeline that consumes it.
# One Dagster UI, correct lineage across both.
#
# Asset graph:
#   iws_eod_settlement       ← daily-partitioned, kinds: python + iws
#   iws_regulatory_extract   ← daily-partitioned, kinds: python + iws
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full REST API lifecycle on
# stdout (AUTH -> SUBMIT -> POLL Waiting/Ready/Running/Succ -> STDLIST).
# Zero external dependencies, no IBM entitlement / license required.
# Point at a real IWS by setting demo_mode: false + IWS_USER / IWS_PASSWORD
# env vars, and (for z/OS) swap /twsd/v1 for /twsz/v1 in endpoint.

set -euo pipefail
PROJECT_DIR="${1:-iws-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing iws_integration component"
$CLI add iws_integration --auto-install 2>&1 | tail -1

echo 'from .component import IWSIntegrationComponent, IWSJobSpec, IWSSourceTableSpec
__all__ = ["IWSIntegrationComponent", "IWSJobSpec", "IWSSourceTableSpec"]' \
  > "src/$PKG/components/iws_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/iws_integration"

mkdir -p "src/$PKG/defs/iws_integration"
cat > "src/$PKG/defs/iws_integration/defs.yaml" <<EOF
type: $PKG.components.iws_integration.component.IWSIntegrationComponent

attributes:
  # demo_mode simulates the IWS REST API on stdout — flip to false to hit
  # a real IWS server (requires IWS_USER / IWS_PASSWORD).
  # For z/OS engine, swap /twsd/v1 for /twsz/v1 in endpoint.
  demo_mode: true
  endpoint: "https://iws.internal:31116/twsd/v1"

  group_name: iws_integration
  iws_user_env: IWS_USER
  iws_password_env: IWS_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_name: EOD_BATCH_SETTLEMENT
      asset_name: iws_eod_settlement
      description: >-
        End-of-day batch settlement. IWS orchestrates the underlying
        mainframe JCL and distributed batch. Dagster submits via REST
        with the scheduled date from the partition key, polls through
        Waiting -> Ready -> Running -> Succ, then retrieves stdlist.
      application: DAILY_BATCH
      workstation: CPU1-MASTER
      alias_name: CORE_BANKING_SETTLEMENT

    - job_name: REGULATORY_EXTRACT
      asset_name: iws_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      application: DAILY_BATCH
      workstation: CPU1-MASTER
      alias_name: COMPLIANCE_REGULATORY
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole REST API lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for iws_eod_settlement"
uv run dg launch --assets 'iws_eod_settlement' --partition "$PARTITION" 2>&1 | tail -25

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    iws_eod_settlement        (daily partitioned, kinds: python + iws)
    iws_regulatory_extract    (daily partitioned, kinds: python + iws)

Also shipped:
    Jobs      iws_rerun_job / iws_hold_job / iws_release_job /
              iws_kill_job / iws_reconciliation
    Sensors   iws_external_execution_monitor / iws_inbound_trigger
    Schedule  iws_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'iws_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real IWS instance:
    Set demo_mode: false in defs.yaml, then:
    export IWS_USER=svc_dagster
    export IWS_PASSWORD='<your-password>'
    # For z/OS engine, swap /twsd/v1 for /twsz/v1 in endpoint
    uv run dg dev

Hybrid deployment story:
    IWS keeps owning z/OS JCL + mainframe scheduling + regulatory
    calendars. Dagster owns cloud / analytics / AI. One UI, correct
    lineage across both — multi-year modernization stops being a
    rip-and-replace bet.

Sister components (same shape, other schedulers):
    controlm_integration / runmyjobs_integration / stonebranch_uac_integration
MSG

#!/usr/bin/env bash
# UiPath Orchestrator Integration end-to-end demo — Dagster wraps UiPath
# Releases as daily-partitioned assets, with ops for restart/soft-stop/kill/
# disable-schedule/reconcile, plus a UiPath -> Dagster inbound-trigger sensor.
#
# RPA meets orchestrated data pipelines: UiPath owns the desktop/attended
# RPA layer, Dagster orchestrates the wider workflow (ingest -> RPA -> transform
# -> notify) with each Release as a first-class daily-partitioned asset.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   uipath_invoice_extract    ← daily-partitioned, kinds: python + uipath + rpa
#   uipath_hr_onboarding      ← daily-partitioned, kinds: python + uipath + rpa
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full OData REST lifecycle on
# stdout. Zero external dependencies, zero UiPath licensing. Point at a real
# Orchestrator by setting demo_mode: false + UIPATH_CLIENT_ID /
# UIPATH_CLIENT_SECRET env vars (free from the UiPath Cloud Community tier
# at https://cloud.uipath.com/signup).

set -euo pipefail
PROJECT_DIR="${1:-uipath-orchestrator-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing uipath_orchestrator_integration component"
$CLI add uipath_orchestrator_integration --auto-install 2>&1 | tail -1

echo 'from .component import UiPathOrchestratorIntegrationComponent, UiPathProcessSpec, UiPathSourceTableSpec
__all__ = ["UiPathOrchestratorIntegrationComponent", "UiPathProcessSpec", "UiPathSourceTableSpec"]' \
  > "src/$PKG/components/uipath_orchestrator_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/uipath_orchestrator_integration"

mkdir -p "src/$PKG/defs/uipath_orchestrator_integration"
cat > "src/$PKG/defs/uipath_orchestrator_integration/defs.yaml" <<EOF
type: $PKG.components.uipath_orchestrator_integration.component.UiPathOrchestratorIntegrationComponent

attributes:
  # demo_mode simulates the OData REST API on stdout — flip to false to hit
  # a real UiPath Orchestrator (requires UIPATH_CLIENT_ID / UIPATH_CLIENT_SECRET
  # from an External Application registered under Admin -> External Applications).
  demo_mode: true
  endpoint: "https://cloud.uipath.com/organization/tenant/orchestrator_"

  group_name: uipath_orchestrator_integration
  uipath_client_id_env: UIPATH_CLIENT_ID
  uipath_client_secret_env: UIPATH_CLIENT_SECRET

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - release_key: "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
      asset_name: uipath_invoice_extract
      description: >-
        Invoice-extract RPA process. Attended bots read PDF invoices from a
        shared drive, key each line into the ERP GUI, and write a summary
        row to the invoice-staging table. Dagster starts the job via
        Orchestrator's OData API with the partition date as an InputArgument,
        polls Pending -> Running -> Successful, then captures OutputArguments.
      folder: Finance
      folder_id: 42
      robot_ids: []
      input_arguments:
        InvoiceDate: "{partition_key}"
        Mode: "batch"
      machine_group: finance_robots
      run_as: svc_dagster

    - release_key: "b2c3d4e5-f6a7-8901-bcde-f12345678901"
      asset_name: uipath_hr_onboarding
      description: >-
        HR onboarding RPA process. Unattended bots read new-hire tickets,
        provision accounts in the legacy HRIS Windows client, and email the
        welcome packet. Runs on the Finance folder robots (shared VDI pool).
      folder: Finance
      folder_id: 42
      robot_ids: []
      input_arguments:
        OnboardingDate: "{partition_key}"
      machine_group: finance_robots
      run_as: svc_dagster
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole OData REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for uipath_invoice_extract"
RUN_LOG="./.uipath_demo_run.log"
uv run dg launch --assets 'uipath_invoice_extract' --partition "$PARTITION" 2>&1 | tee "$RUN_LOG" | tail -30

echo ""
echo ">>> Verifying the OData REST trace fired (grep the run log for lifecycle markers)"
grep -E "\[AUTH\]|\[FOLDER\]|\[START\]|\[POLL\]|\[OUTPUT\]|\[DONE\]" "$RUN_LOG" | head -12 || true
rm -f "$RUN_LOG"

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    uipath_invoice_extract    (daily partitioned, kinds: python + uipath + rpa)
    uipath_hr_onboarding      (daily partitioned, kinds: python + uipath + rpa)

Also shipped:
    Jobs      uipath_restart_job / uipath_stop_job_soft / uipath_stop_job_kill /
              uipath_disable_schedule / uipath_reconciliation
    Sensors   uipath_external_execution_monitor / uipath_inbound_trigger
    Schedule  uipath_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'uipath_invoice_extract' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule, run any of the 5 op-jobs):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real UiPath Orchestrator:
    1. Free UiPath Cloud Community tier: https://cloud.uipath.com/signup
    2. Admin -> External Applications -> register a Confidential app,
       grant scopes OR.Jobs / OR.Execution / OR.Folders.
    3. Set demo_mode: false in defs.yaml and update the endpoint to
       https://cloud.uipath.com/<org>/<tenant>/orchestrator_
    4. Export credentials:
         export UIPATH_CLIENT_ID='<app-id>'
         export UIPATH_CLIENT_SECRET='<app-secret>'
       uv run dg dev
MSG

#!/usr/bin/env bash
# Automation Anywhere Integration end-to-end demo -- Dagster wraps A360 / AAI
# Control Room Bots as daily-partitioned assets, with ops for redeploy / pause /
# resume / stop / reconcile, and an AA -> Dagster inbound-trigger sensor
# (callbackInfo -> GraphQL launchRun).
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   aa_invoice_extract   <- daily-partitioned, kinds: python + automation-anywhere + rpa
#   aa_hr_onboarding     <- daily-partitioned, kinds: python + automation-anywhere + rpa
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 -- demo_mode: true simulates the full Control Room REST lifecycle
# on stdout (AUTH -> DEPLOY -> POLL x 5 -> LOGS -> DONE). Zero external
# dependencies, zero AA entitlement, zero network. Automation Anywhere ships
# no public Docker image -- it's an enterprise-licensed, Windows-heavy install.
# Point at a real Control Room by setting demo_mode: false + AUTOMATION_ANYWHERE_USER
# / AUTOMATION_ANYWHERE_PASSWORD env vars (AAI Cloud offers a business trial at
# https://aai.automationanywhere.com/).

set -euo pipefail
PROJECT_DIR="${1:-automation-anywhere-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing automation_anywhere_integration component"
$CLI add automation_anywhere_integration --auto-install 2>&1 | tail -1

echo 'from .component import AutomationAnywhereIntegrationComponent, AutomationAnywhereBotSpec, AutomationAnywhereSourceTableSpec
__all__ = ["AutomationAnywhereIntegrationComponent", "AutomationAnywhereBotSpec", "AutomationAnywhereSourceTableSpec"]' \
  > "src/$PKG/components/automation_anywhere_integration/__init__.py"

# Remove auto-installed example defs -- we ship our own
rm -rf "src/$PKG/defs/automation_anywhere_integration"

mkdir -p "src/$PKG/defs/automation_anywhere_integration"
cat > "src/$PKG/defs/automation_anywhere_integration/defs.yaml" <<EOF
type: $PKG.components.automation_anywhere_integration.component.AutomationAnywhereIntegrationComponent

attributes:
  # demo_mode simulates the Control Room REST lifecycle on stdout -- flip to
  # false to hit a real A360 / AAI instance (requires AUTOMATION_ANYWHERE_USER /
  # AUTOMATION_ANYWHERE_PASSWORD).
  demo_mode: true
  endpoint: "https://control-room.internal"

  group_name: automation_anywhere_integration
  automation_anywhere_user_env: AUTOMATION_ANYWHERE_USER
  automation_anywhere_password_env: AUTOMATION_ANYWHERE_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - file_id: 12345
      asset_name: aa_invoice_extract
      description: >-
        Invoice extraction bot. Automation Anywhere orchestrates the underlying
        screen-scraping + PDF parsing steps. Dagster deploys via REST with the
        partition date as botInput, polls through DEPLOYED -> QUEUED -> RUNNING
        -> COMPLETED, then retrieves logs.
      workspace: Finance
      application: FINANCE
      device_pool_id: 7
      run_as_user_ids: [42]
      bot_input:
        run_date: "{partition_key}"
        source_folder: "/mnt/invoices/inbound"

    - file_id: 12346
      asset_name: aa_hr_onboarding
      description: >-
        HR new-hire onboarding bot. Reads new-hire records for the partition
        date and provisions accounts across HRIS + IdP + payroll.
      workspace: Finance
      application: HR
      device_pool_id: 7
      run_as_user_ids: [42]
      bot_input:
        run_date: "{partition_key}"
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole Control Room REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for aa_invoice_extract"
uv run dg launch --assets 'aa_invoice_extract' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    aa_invoice_extract       (daily partitioned, kinds: python + automation-anywhere + rpa)
    aa_hr_onboarding         (daily partitioned, kinds: python + automation-anywhere + rpa)

Also shipped:
    Jobs      automation_anywhere_redeploy_bot / automation_anywhere_pause_execution /
              automation_anywhere_resume_execution / automation_anywhere_stop_execution /
              automation_anywhere_reconciliation
    Sensors   automation_anywhere_external_execution_monitor /
              automation_anywhere_inbound_trigger
    Schedule  automation_anywhere_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'aa_invoice_extract' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Control Room (A360 on-prem or AAI Cloud):
    Set demo_mode: false in defs.yaml + point endpoint at your tenant URL, then:
    export AUTOMATION_ANYWHERE_USER=svc_dagster
    export AUTOMATION_ANYWHERE_PASSWORD='<your-password>'    # or apiKey
    uv run dg dev

  AAI Cloud offers a business trial at https://aai.automationanywhere.com/.
  There's no public Docker image -- Control Room is an enterprise-licensed,
  Windows-heavy install, so the demo runs entirely in demo_mode.
MSG

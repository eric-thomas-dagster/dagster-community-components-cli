#!/usr/bin/env bash
# Power Automate Integration end-to-end demo — Dagster wraps Microsoft
# Power Automate cloud flows as daily-partitioned assets, with ops for
# retrigger/cancel/turn-off/turn-on/reconcile, plus a Power Automate
# -> Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   pa_invoice_extract   ← daily-partitioned, kinds: python + power-automate + rpa
#   pa_hr_onboarding     ← daily-partitioned, kinds: python + power-automate + rpa
#
# Plus 5 op jobs + 2 sensors + 1 reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full Flow Management REST lifecycle
# on stdout. Zero external dependencies, no M365 tenant needed. Point at a
# real Power Automate service by setting demo_mode: false + registering an
# Azure AD app (Flows.Read.All + Flows.Manage.All) and exporting
# POWER_AUTOMATE_TENANT_ID / POWER_AUTOMATE_CLIENT_ID / POWER_AUTOMATE_CLIENT_SECRET.

set -euo pipefail
PROJECT_DIR="${1:-power-automate-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing power_automate_integration component"
$CLI add power_automate_integration --auto-install 2>&1 | tail -1

echo 'from .component import PowerAutomateIntegrationComponent, PowerAutomateFlowSpec, PowerAutomateSourceTableSpec
__all__ = ["PowerAutomateIntegrationComponent", "PowerAutomateFlowSpec", "PowerAutomateSourceTableSpec"]' \
  > "src/$PKG/components/power_automate_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/power_automate_integration"

mkdir -p "src/$PKG/defs/power_automate_integration"
cat > "src/$PKG/defs/power_automate_integration/defs.yaml" <<EOF
type: $PKG.components.power_automate_integration.component.PowerAutomateIntegrationComponent

attributes:
  # demo_mode simulates the Flow Management REST API on stdout — flip to false
  # to hit a real Power Automate service (requires an Azure AD app with
  # Flows.Read.All + Flows.Manage.All and POWER_AUTOMATE_TENANT_ID /
  # POWER_AUTOMATE_CLIENT_ID / POWER_AUTOMATE_CLIENT_SECRET env vars).
  demo_mode: true
  endpoint: "https://api.flow.microsoft.com"

  group_name: power_automate_integration
  power_automate_tenant_env: POWER_AUTOMATE_TENANT_ID
  power_automate_client_id_env: POWER_AUTOMATE_CLIENT_ID
  power_automate_client_secret_env: POWER_AUTOMATE_CLIENT_SECRET

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - flow_id: "b1c2d3e4-5f60-7a80-9b0c-1d2e3f405060"
      asset_name: pa_invoice_extract
      description: >-
        Invoice-extract cloud flow. Power Automate handles the SharePoint /
        Outlook / Teams orchestration Dagster shouldn't. Dagster triggers via
        the manual trigger REST endpoint, polls Running -> Succeeded, records
        the runName.
      environment_id: "Default-a0b1c2d3-e4f5-6789-abcd-ef0123456789"
      solution: Finance
      application: INVOICE_OPS
      trigger_input:
        DATE: "{partition_key}"
        SOURCE_SYSTEM: SAP

    - flow_id: "c2d3e4f5-6071-8b90-ac1d-2e3f40506070"
      asset_name: pa_hr_onboarding
      description: >-
        HR onboarding automation. Must complete before downstream identity /
        access provisioning fires.
      environment_id: "Default-a0b1c2d3-e4f5-6789-abcd-ef0123456789"
      solution: HR
      application: ONBOARDING
      trigger_input:
        DATE: "{partition_key}"
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole Flow Management REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for pa_invoice_extract"
uv run dg launch --assets 'pa_invoice_extract' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    pa_invoice_extract  (daily partitioned, kinds: python + power-automate + rpa)
    pa_hr_onboarding    (daily partitioned, kinds: python + power-automate + rpa)

Also shipped:
    Jobs      power_automate_retrigger / power_automate_cancel_run /
              power_automate_turn_off / power_automate_turn_on /
              power_automate_reconciliation
    Sensors   power_automate_external_execution_monitor /
              power_automate_inbound_trigger
    Schedule  power_automate_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'pa_invoice_extract' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Power Automate service:
  1. Register an Azure AD app (Entra ID -> App registrations -> New registration)
     https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade
  2. Add API permissions (Power Automate Service):
       Flows.Read.All    (list flows / read run history)
       Flows.Manage.All  (trigger / cancel / turn off / turn on)
     Grant admin consent for the tenant.
  3. Create a client secret under Certificates & secrets.
  4. Set demo_mode: false in defs.yaml, then:
       export POWER_AUTOMATE_TENANT_ID='<your-tenant-guid>'
       export POWER_AUTOMATE_CLIENT_ID='<your-app-client-id>'
       export POWER_AUTOMATE_CLIENT_SECRET='<your-client-secret>'
       uv run dg dev
MSG

#!/usr/bin/env bash
# Blue Prism Integration end-to-end demo — Dagster wraps SS&C Blue Prism 7+
# Web API processes as daily-partitioned assets, with ops for
# restart/stop/terminate/hold/reconcile, and a Blue Prism <-> Dagster
# inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   bp_invoice_extract   <- daily-partitioned, kinds: python + blue-prism + rpa
#   bp_hr_onboarding     <- daily-partitioned, kinds: python + blue-prism + rpa
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full Web API lifecycle on stdout.
# Zero external dependencies. Blue Prism does not publish a public Docker
# image (Windows-native, enterprise-licensed to SS&C customers), so demo_mode
# is how you evaluate the shape without a Blue Prism environment.
#
# Point at a real Blue Prism by setting demo_mode: false + BLUE_PRISM_USER /
# BLUE_PRISM_PASSWORD (or BLUE_PRISM_API_KEY) env vars.

set -euo pipefail
PROJECT_DIR="${1:-blue-prism-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing blue_prism_integration component"
$CLI add blue_prism_integration --auto-install 2>&1 | tail -1

echo 'from .component import BluePrismIntegrationComponent, BluePrismProcessSpec, BluePrismSourceTableSpec
__all__ = ["BluePrismIntegrationComponent", "BluePrismProcessSpec", "BluePrismSourceTableSpec"]' \
  > "src/$PKG/components/blue_prism_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/blue_prism_integration"

mkdir -p "src/$PKG/defs/blue_prism_integration"
cat > "src/$PKG/defs/blue_prism_integration/defs.yaml" <<EOF
type: $PKG.components.blue_prism_integration.component.BluePrismIntegrationComponent

attributes:
  # demo_mode simulates the Blue Prism 7 Web API lifecycle on stdout — flip
  # to false to hit a real Blue Prism instance (requires BLUE_PRISM_USER /
  # BLUE_PRISM_PASSWORD or BLUE_PRISM_API_KEY env vars).
  demo_mode: true
  endpoint: "https://blueprism.internal/api/v7"

  group_name: blue_prism_integration
  blue_prism_user_env: BLUE_PRISM_USER
  blue_prism_password_env: BLUE_PRISM_PASSWORD
  blue_prism_api_key_env: BLUE_PRISM_API_KEY

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - process_id: "b1e2c3d4-5f6a-7b8c-9d0e-1f2a3b4c5d6e"
      asset_name: bp_invoice_extract
      description: >-
        Invoice extraction bot. Blue Prism scrapes the AP portal, normalizes
        vendors, and loads to the staging table on the Finance runtime
        resource. Dagster starts the session with the partition_key as the
        invoice date, polls Pending -> Running -> Completed, then retrieves
        session logs.
      resource_id: "a0b1c2d3-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
      resource_group: Finance
      application: Accounts_Payable
      inputs:
        InvoiceDate: "{partition_key}"
        BatchSize: 500

    - process_id: "c3d4e5f6-7a8b-9c0d-1e2f-3a4b5c6d7e8f"
      asset_name: bp_hr_onboarding
      description: >-
        Employee onboarding bot. Creates AD/Okta accounts, sends welcome
        emails, and files the HRIS record from the HR runtime resource.
        Must complete before HR dashboards refresh.
      resource_id: "d4e5f6a7-8b9c-0d1e-2f3a-4b5c6d7e8f90"
      resource_group: Finance
      application: Employee_Lifecycle
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole Web API lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for bp_invoice_extract"
uv run dg launch --assets 'bp_invoice_extract' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    bp_invoice_extract     (daily partitioned, kinds: python + blue-prism + rpa)
    bp_hr_onboarding       (daily partitioned, kinds: python + blue-prism + rpa)

Also shipped:
    Jobs      blue_prism_restart_session / blue_prism_stop_session /
              blue_prism_terminate_session / blue_prism_hold_process /
              blue_prism_reconciliation
    Sensors   blue_prism_external_execution_monitor / blue_prism_inbound_trigger
    Schedule  blue_prism_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'bp_invoice_extract' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Blue Prism 7+ instance:
    Set demo_mode: false in defs.yaml + override endpoint, then either:

    # Option A — Basic auth (username + password -> bearer token)
    export BLUE_PRISM_USER=svc_dagster
    export BLUE_PRISM_PASSWORD='<your-password>'

    # Option B — X-API-Key (Blue Prism 7 Web API)
    export BLUE_PRISM_API_KEY='<your-web-api-key>'

    uv run dg dev

Blue Prism 6 vs 7 caveat: this component targets the Blue Prism 7+ REST
Web API. Older deployments (v6 and earlier) may only expose the legacy
SOAP interface — the REST paths will 404 against those environments.
Verify against your Blue Prism version before flipping demo_mode off.
MSG

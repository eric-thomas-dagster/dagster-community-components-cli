#!/usr/bin/env bash
# GoAnywhere Integration end-to-end demo — Dagster wraps Fortra GoAnywhere MFT
# projects as daily-partitioned assets, with ops for rerun/dashboard-summary/
# reconcile, and a GoAnywhere <-> Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   goanywhere_partner_edi_inbound   ← daily-partitioned, kinds: python + goanywhere
#   goanywhere_regulatory_upload     ← daily-partitioned, kinds: python + goanywhere
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the whole GACMD REST API lifecycle on
# stdout. Zero external dependencies. GoAnywhere MFT ships real Docker images
# (fortrallc/goanywhere-mft et al.) but the admin console/REST API need a
# trial or paid license key to do anything, so the simulator is still the
# only zero-license way to exercise the full surface. Point at a real
# GoAnywhere instance by setting demo_mode: false + GOANYWHERE_USER /
# GOANYWHERE_PASSWORD env vars.

set -euo pipefail
PROJECT_DIR="${1:-goanywhere-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing goanywhere_integration component"
$CLI add goanywhere_integration --auto-install 2>&1 | tail -1

echo 'from .component import GoAnywhereIntegrationComponent, GoAnywhereProjectSpec, GoAnywhereSourceFileSpec
__all__ = ["GoAnywhereIntegrationComponent", "GoAnywhereProjectSpec", "GoAnywhereSourceFileSpec"]' \
  > "src/$PKG/components/goanywhere_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/goanywhere_integration"

mkdir -p "src/$PKG/defs/goanywhere_integration"
cat > "src/$PKG/defs/goanywhere_integration/defs.yaml" <<EOF
type: $PKG.components.goanywhere_integration.component.GoAnywhereIntegrationComponent

attributes:
  # demo_mode simulates the GACMD REST API on stdout — flip to false to hit
  # a real GoAnywhere server (requires GOANYWHERE_USER / GOANYWHERE_PASSWORD).
  demo_mode: true
  endpoint: "https://goanywhere.internal/goanywhere/rest"

  group_name: goanywhere_integration
  goanywhere_user_env: GOANYWHERE_USER
  goanywhere_password_env: GOANYWHERE_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  # Opt-in scaffold -- on here so the demo shows the full surface (off by
  # default otherwise; only generate_external_monitor_sensor defaults true).
  generate_rerun_job: true
  generate_dashboard_job: true
  generate_reconciliation: true
  generate_inbound_trigger_sensor: true

  # deps is per-project, not one list shared by every project.
  projects:
    - project_name: PARTNER_EDI_INBOUND
      asset_name: goanywhere_partner_edi_inbound
      description: >-
        Nightly partner EDI pickup + decrypt + validate. Dagster runs the
        project via GACMD REST with SCHEDULED_DATE from the partition key.
      job_queue: SFTP_INBOUND
      mode: NORMAL
      priority: 5
      variables:
        SOURCE_PATH: "/inbound/partners"

    - project_name: REGULATORY_UPLOAD
      asset_name: goanywhere_regulatory_upload
      description: >-
        Encrypted regulatory file upload to the external SFTP endpoint.
      job_queue: SFTP_OUTBOUND
      mode: NORMAL
      priority: 8
      deps: ["goanywhere_partner_edi_inbound"]

  source_files:
    - path: "/inbound/partners/processed"
      asset_name: goanywhere_partner_edi_landed
      produced_by: goanywhere_partner_edi_inbound
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole GACMD REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for goanywhere_partner_edi_inbound"
uv run dg launch --assets 'goanywhere_partner_edi_inbound' --partition "$PARTITION" 2>&1 | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    goanywhere_partner_edi_inbound   (daily partitioned, kinds: python + goanywhere)
    goanywhere_regulatory_upload     (daily partitioned, kinds: python + goanywhere)
    goanywhere_partner_edi_landed    (source, kinds: goanywhere + file)

Also shipped:
    Jobs      goanywhere_rerun_project / goanywhere_dashboard / goanywhere_reconciliation
    Sensors   goanywhere_external_execution_monitor / goanywhere_inbound_trigger
    Schedule  goanywhere_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'goanywhere_partner_edi_inbound' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real GoAnywhere instance:
    Set demo_mode: false in defs.yaml, then:
    export GOANYWHERE_USER=svc_dagster
    export GOANYWHERE_PASSWORD='<your-password-or-admin-api-key>'
    uv run dg dev

    Note: GoAnywhere MFT ships real Docker images (fortrallc/goanywhere-mft
    et al.) but the admin console/REST API need a trial or paid license key
    to do anything — there's no documented license-free mode. Production
    wiring targets a real licensed GoAnywhere instance.
MSG

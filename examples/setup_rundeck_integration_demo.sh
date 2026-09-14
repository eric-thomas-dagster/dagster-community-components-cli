#!/usr/bin/env bash
# Rundeck Integration end-to-end demo — Dagster wraps Rundeck Jobs
# as daily-partitioned assets, with ops for restart/disable/enable/abort/reconcile,
# and a Rundeck ↔ Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   rundeck_eod_settlement       ← daily-partitioned, kinds: python + rundeck
#   rundeck_regulatory_extract   ← daily-partitioned, kinds: python + rundeck
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full Rundeck REST lifecycle on
# stdout. Zero external dependencies. Rundeck Community Edition is free +
# open-source, so real-Docker validation is a one-liner (see the MSG block
# at the end for the `docker run rundeck/rundeck:5.10.0` command).

set -euo pipefail
PROJECT_DIR="${1:-rundeck-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing rundeck_integration component"
$CLI add rundeck_integration --auto-install 2>&1 | tail -1

echo 'from .component import RundeckIntegrationComponent, RundeckJobSpec, RundeckSourceTableSpec
__all__ = ["RundeckIntegrationComponent", "RundeckJobSpec", "RundeckSourceTableSpec"]' \
  > "src/$PKG/components/rundeck_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/rundeck_integration"

mkdir -p "src/$PKG/defs/rundeck_integration"
cat > "src/$PKG/defs/rundeck_integration/defs.yaml" <<EOF
type: $PKG.components.rundeck_integration.component.RundeckIntegrationComponent

attributes:
  # demo_mode simulates the REST API on stdout — flip to false to hit
  # a real Rundeck server (requires RUNDECK_API_TOKEN env var).
  demo_mode: true
  endpoint: "http://localhost:4440"
  api_version: 47

  group_name: rundeck_integration
  rundeck_token_env: RUNDECK_API_TOKEN

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_id: 3d7f9e2a-1c4b-4a5f-8d2e-6f8a1b2c3d4e
      asset_name: rundeck_eod_settlement
      description: >-
        End-of-day settlement job. Rundeck runs the underlying shell / Ansible
        steps across the settlement node group. Dagster fires the execution via
        REST with the partition date as an argString option, polls through
        running -> succeeded, then records the executionId.
      project: DAILY_BATCH
      arg_string: "-runDate {{ partition_key }} -mode full"

    - job_id: 7a2b8c1d-9e4f-4b6a-8c5d-2f1e3a4b5c6d
      asset_name: rundeck_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      project: DAILY_BATCH
      arg_string: "-runDate {{ partition_key }} -region ALL"
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole REST lifecycle runs
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for rundeck_eod_settlement"
uv run dg launch --assets 'rundeck_eod_settlement' --partition "$PARTITION" 2>&1 \
  | grep -E "\[AUTH\]|\[SUBMIT\]|\[POLL\]|\[OUTPUT\]|\[DONE\]|Payload|Response|ASSET_MATERIALIZATION|RUN_SUCCESS" \
  | tail -20

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    rundeck_eod_settlement        (daily partitioned, kinds: python + rundeck)
    rundeck_regulatory_extract    (daily partitioned, kinds: python + rundeck)

Also shipped:
    Jobs      rundeck_restart_execution / rundeck_disable_job /
              rundeck_enable_job / rundeck_abort_execution /
              rundeck_reconciliation
    Sensors   rundeck_external_execution_monitor / rundeck_inbound_trigger
    Schedule  rundeck_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'rundeck_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Rundeck — the open-source Docker path is a one-liner:

    docker run -d --name rundeck -p 4440:4440 \\
        -e RUNDECK_GRAILS_URL=http://localhost:4440 \\
        rundeck/rundeck:5.10.0

    # Log in at http://localhost:4440 with admin / admin
    # Create a project (e.g. "banking"), create a job (any shell step),
    # copy the job UUID from the job detail URL.
    # Generate a token: User Profile -> User API Tokens -> Generate New Token.

    # Then in $PROJECT_DIR/src/$PKG/defs/rundeck_integration/defs.yaml,
    # flip demo_mode to false, paste the job UUID into jobs[0].job_id, and:

    export RUNDECK_API_TOKEN='rdk-<your-token>'
    uv run dg dev
MSG

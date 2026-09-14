#!/usr/bin/env bash
# Jenkins Integration end-to-end demo — Dagster wraps Jenkins jobs as
# daily-partitioned assets, with ops for rebuild/disable/enable/stop/reconcile,
# and a Jenkins -> Dagster inbound-trigger sensor.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   jenkins_eod_settlement       ← daily-partitioned, kinds: python + jenkins
#   jenkins_regulatory_extract   ← daily-partitioned, kinds: python + jenkins
#
# Plus sensors + ops+jobs + reconciliation schedule (all STOPPED by default).
#
# COST: $0 — demo_mode: true simulates the full Jenkins REST API lifecycle on
# stdout (AUTH -> CRUMB -> TRIGGER -> QUEUE -> POLL -> CONSOLE -> DONE).
# Zero external dependencies. Point at a real Jenkins by setting
# demo_mode: false + JENKINS_USER / JENKINS_API_TOKEN env vars, or spin up
# jenkins/jenkins:lts in Docker — see the walkthrough for both paths.

set -euo pipefail
PROJECT_DIR="${1:-jenkins-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing jenkins_integration component"
$CLI add jenkins_integration --auto-install 2>&1 | tail -1

echo 'from .component import JenkinsIntegrationComponent, JenkinsJobSpec, JenkinsSourceTableSpec
__all__ = ["JenkinsIntegrationComponent", "JenkinsJobSpec", "JenkinsSourceTableSpec"]' \
  > "src/$PKG/components/jenkins_integration/__init__.py"

# Remove auto-installed example defs — we ship our own
rm -rf "src/$PKG/defs/jenkins_integration"

mkdir -p "src/$PKG/defs/jenkins_integration"
cat > "src/$PKG/defs/jenkins_integration/defs.yaml" <<EOF
type: $PKG.components.jenkins_integration.component.JenkinsIntegrationComponent

attributes:
  # demo_mode simulates the Jenkins REST API on stdout — flip to false to hit
  # a real Jenkins controller (requires JENKINS_USER / JENKINS_API_TOKEN env
  # vars). See the walkthrough for a jenkins/jenkins:lts Docker quickstart.
  demo_mode: true
  endpoint: "http://localhost:8080"

  group_name: jenkins_integration
  jenkins_user_env: JENKINS_USER
  jenkins_token_env: JENKINS_API_TOKEN

  max_retries: 2
  retry_delay_seconds: 60
  partition_start_date: "2024-01-01"

  jobs:
    - job_name: banking/eod-batch-settlement
      asset_name: jenkins_eod_settlement
      description: >-
        End-of-day batch settlement. Jenkins pipeline runs the actual
        settlement job on a Linux agent; Dagster triggers via REST with
        the partition date, polls the build to terminal result, and pulls
        consoleText for troubleshooting.
      folder: banking
      application: CORE_BANKING
      node_label: linux-heavy
      parameters:
        SETTLEMENT_DATE: "{partition_key}"

    - job_name: banking/regulatory-extract
      asset_name: jenkins_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      folder: banking
      application: COMPLIANCE
      node_label: linux-heavy

  # Optional: Jenkins-managed tables that Dagster observes (adds to lineage)
  source_tables: []
  # source_tables:
  #   - table_name: "CORE_BANKING.SETTLEMENT_LEDGER"
  #     asset_name: jenkins_settlement_table
  #     produced_by: jenkins_eod_settlement
EOF

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

# Materialize one partition to prove the whole REST lifecycle runs end-to-end
PARTITION="${PARTITION:-2024-06-01}"
echo ">>> Materializing partition $PARTITION for jenkins_eod_settlement"
uv run dg launch --assets 'jenkins_eod_settlement' --partition "$PARTITION" 2>&1 \
  | grep -E "AUTH|CRUMB|TRIGGER|QUEUE|POLL|CONSOLE|DONE|RUN_SUCCESS|ASSET_MATERIALIZATION" || true

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    jenkins_eod_settlement        (daily partitioned, kinds: python + jenkins)
    jenkins_regulatory_extract    (daily partitioned, kinds: python + jenkins)

Also shipped:
    Jobs      jenkins_rebuild_job / jenkins_disable_job / jenkins_enable_job /
              jenkins_stop_build / jenkins_reconciliation
    Sensors   jenkins_external_execution_monitor / jenkins_inbound_trigger
    Schedule  jenkins_reconciliation_schedule  (cron: 0 * * * *)

Materialize another partition:
    cd $PROJECT_DIR
    uv run dg launch --assets 'jenkins_eod_settlement' --partition 2024-06-02

Or open the Dagster UI (browse graph, click to materialize any partition,
toggle sensors + schedule):
    cd $PROJECT_DIR
    uv run dg dev

Point at a real Jenkins:
    Set demo_mode: false in defs.yaml, then:
    export JENKINS_USER=svc-dagster
    export JENKINS_API_TOKEN='<api-token-from-http://jenkins/user/<name>/configure>'
    uv run dg dev

Real Docker Jenkins (no license — jenkins/jenkins:lts):
    docker run -d --name jenkins-demo -p 8080:8080 -p 50000:50000 \\
      -v jenkins_home:/var/jenkins_home \\
      jenkins/jenkins:lts
    # Wait ~30s, then get the initial admin password:
    docker exec jenkins-demo cat /var/jenkins_home/secrets/initialAdminPassword
    # Then in http://localhost:8080: finish wizard, generate an API token
    # from your user's Configure page, create a folder 'banking' + freestyle
    # job 'eod-batch-settlement' with a String parameter SETTLEMENT_DATE and
    # an Execute-shell step. Point the component at http://localhost:8080
    # with demo_mode: false and materialize a partition — full walkthrough
    # in examples/jenkins_integration.md.
MSG

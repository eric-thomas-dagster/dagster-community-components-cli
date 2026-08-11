#!/usr/bin/env bash
# Event Automation demo — declarative event → action wiring, all in YAML.
#
# WHAT THIS DEMONSTRATES
#   Prefect Automations shape as ONE Dagster component. Multiple triggers
#   (run_status / asset_materialized / schedule / http_poll /
#   freshness_violation) fan out to actions (webhook / slack / pagerduty /
#   discord / materialize / launch_job / emit_event). Under the hood
#   every automation becomes a real Dagster sensor — full sensor UI,
#   full run history, no separate "automations" mini-app.
#
# This demo installs one EventAutomationComponent that:
#   1. Watches a purpose-built failing job (job_that_fails). When it fails,
#      hits httpbin.org/post with a templated body — proves the wiring
#      without needing real Slack / PagerDuty credentials.
#   2. Cron-heartbeat: every minute, hits httpbin.org/get. Proves the
#      schedule trigger works and delivers the timestamp token.
#
# Plus one always-passes job (job_that_succeeds) — same automation shape
# would fire on SUCCESS if configured.
#
# COST: $0 — httpbin.org/post + httpbin.org/get, no auth, no SaaS.

set -euo pipefail
PROJECT_DIR="${1:-event-automation-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q requests croniter

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing EventAutomationComponent"
$CLI add event_automation --auto-install

echo ">>> Writing the failing + succeeding jobs (targets for the automation)"
cat > "src/$PKG/defs/jobs.py" <<'PY'
"""Two Dagster jobs used to trigger the automation.

The failing one intentionally raises so the run_status FAILURE sensor
has a real event to fire against.
"""
import dagster as dg


@dg.op
def _fail_op():
    raise RuntimeError(
        "intentional failure — demonstrates the run_status FAILURE trigger"
    )


@dg.op
def _pass_op():
    return {"status": "ok", "message": "smooth run"}


@dg.job(description="Always fails on purpose — used to trigger the alert automation.")
def job_that_fails():
    _fail_op()


@dg.job(description="Always succeeds — a control run.")
def job_that_succeeds():
    _pass_op()


defs = dg.Definitions(jobs=[job_that_fails, job_that_succeeds])
PY

echo ">>> Writing the automation (event → webhook, cron → webhook)"
mkdir -p "src/$PKG/defs/automations"
cat > "src/$PKG/defs/automations/defs.yaml" <<YAML
type: $PKG.components.event_automation.component.EventAutomationComponent
attributes:
  name: demo_alert_on_failure_plus_heartbeat
  description: |
    Two triggers wired to httpbin webhooks. On any run FAILURE, POST to
    httpbin/post with a templated body. Also cron-heartbeat every minute.
    Swap the webhook actions for slack / pagerduty / discord in prod.
  default_status: RUNNING
  when:
    - type: run_status
      status: FAILURE
    - type: schedule
      cron: "* * * * *"
  then:
    - type: webhook
      url: "https://httpbin.org/post"
      method: POST
      headers:
        X-Automation-Source: event-automation-demo
      body_template: >
        {"event":"{event_type}","job":"{job_name}","status":"{status}",
         "run_id":"{run_id}","ts":"{timestamp}","message":"{message}"}
YAML

echo ">>> Writing an alerting variant that would go live with real credentials"
mkdir -p "src/$PKG/defs/alerting_example"
cat > "src/$PKG/defs/alerting_example/defs.yaml" <<YAML
type: $PKG.components.event_automation.component.EventAutomationComponent
attributes:
  name: production_alert_shape
  description: |
    Reference for what a real prod alert automation looks like. Set
    SLACK_WEBHOOK_URL + PAGERDUTY_ROUTING_KEY env vars and change
    default_status to RUNNING to activate. Left STOPPED for the demo.
  default_status: STOPPED
  when:
    - type: run_status
      status: FAILURE
      job_name: job_that_fails
  then:
    - type: slack
      webhook_url_env_var: SLACK_WEBHOOK_URL
      message: "🚨 {job_name} failed — run_id={run_id}"
    - type: pagerduty
      routing_key_env_var: PAGERDUTY_ROUTING_KEY
      severity: error
      summary_template: "Prod {job_name} failed"
      dedup_key_template: "prod-failure:{job_name}"
YAML

echo ">>> Writing a platform-observability reference automation (6 platform-health triggers)"
mkdir -p "src/$PKG/defs/platform_observability"
cat > "src/$PKG/defs/platform_observability/defs.yaml" <<YAML
type: $PKG.components.event_automation.component.EventAutomationComponent
attributes:
  name: platform_observability_tour
  description: |
    Reference automation: 6 platform-health triggers in one YAML.
    daemon/agent heartbeat, code location deploys, slow compute startup,
    step errors, metadata state, and container log patterns (K8s / ECS /
    Docker stdout+stderr). Left STOPPED — no real credentials needed to
    inspect the shape in the UI. Toggle to RUNNING to activate.
  default_status: STOPPED
  when:
    - type: daemon_heartbeat
      max_seconds_since_heartbeat: 120
    - type: code_location_status
      on_status: UNHEALTHY
    - type: run_startup_slow
      max_startup_seconds: 120
    - type: step_error
      exception_pattern: "OOMKilled|TimeoutError"
    - type: metadata_match
      asset_key: hourly_summary
      metadata_key: quality_grade
      regex: "poor|failed"
    - type: log_pattern
      pattern: "OOMKilled|SegFault|Panic"
      sources: [events, stdout, stderr]
  then:
    - type: webhook
      url: "https://httpbin.org/post"
      method: POST
      headers:
        X-Automation-Source: platform-observability-tour
      body_template: >
        {"event":"{event_type}","status":"{status}",
         "job":"{job_name}","message":"{message}"}
YAML

cat <<MSG

>>> Setup complete.

Next steps:
  cd $PROJECT_DIR
  uv run dg check defs                    # validate everything loads
  uv run dg list defs                     # see 8 sensors + 2 jobs
  uv run dg dev                           # open http://localhost:3000

3 automations shipped:
  - demo_alert_on_failure_plus_heartbeat   (RUNNING — run_status + schedule)
  - production_alert_shape                 (STOPPED — Slack + PagerDuty on failure)
  - platform_observability_tour            (STOPPED — 6 platform-health triggers)

To watch the automation fire end-to-end:
  1. Open the UI + navigate to Jobs → job_that_fails
  2. Launch it (it will fail)
  3. Watch the Sensors tab: 'demo_alert_on_failure_plus_heartbeat__run_status_0'
     will tick and POST to https://httpbin.org/post
  4. Check the sensor's tick history to see the successful POST

The 'production_alert_shape' automation is stopped by default. To make it
fire real Slack / PagerDuty alerts:
  export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
  export PAGERDUTY_ROUTING_KEY='...'
  # Edit src/$PKG/defs/alerting_example/defs.yaml: default_status: RUNNING
  # Restart dg dev

Walkthrough: https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/event_automation.md
MSG

# Event Automation — Prefect-Automations shape as one Dagster component

Prefect users get to wire triggers → actions in one YAML/UI object — no Python, no separate sensors and schedules and run-status handlers to keep in sync. Dagster has all the underlying primitives (`@sensor`, `@run_status_sensor`, `AutomationCondition`, freshness policies, asset checks, `RunsFilter`, event log storage, compute log manager, daemon status, workspace snapshots) but they're all Python-first and scattered across separate APIs. This demo shows the `EventAutomationComponent` — one YAML surface that collapses **22 trigger types** and **17 action types** into a single component, with real Dagster sensors under the covers.

**The full trigger surface** (grouped by category):

- **Run lifecycle** — `run_status`, `run_duration`, `run_stuck`, `run_startup_slow`
- **Asset events** — `asset_materialized`, `asset_observation`, `asset_check_failed`
- **Data quality** — `metric_threshold`, `metadata_match`, `asset_value_change`, `freshness_violation`, `absence`
- **Platform health** — `daemon_heartbeat`, `code_location_status`, `sensor_failing`, `concurrency_hit`
- **Errors + logs** — `step_error`, `log_pattern` (events + stdout + stderr — catches K8s / ECS / Docker container output)
- **External** — `schedule`, `http_poll`, `sqs_poll`
- **Composite** — `all_of`, `any_of` (AND / OR, one level of nesting)

**The full action surface:**

- **Dagster runs** — `materialize`, `launch_job`, `cancel_run`, `retry_run`, `toggle_sensor`, `toggle_schedule`
- **Alerts** — `slack`, `pagerduty`, `opsgenie`, `discord`, `teams`, `mattermost`, `email`
- **External** — `webhook`, `sns`, `sqs`, `emit_event`

Full field-level docs + more recipes in the component's [README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/sensors/event_automation/README.md). Comprehensive pytest suite (56 tests) at [`sensors/event_automation/tests/`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/event_automation/tests).

## Setup

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_event_automation_demo.sh | bash

cd event-automation-demo
uv run dg dev
```

Then open <http://localhost:3000>.

## What the demo does

Three automations in one YAML shape each.

### `demo_alert_on_failure_plus_heartbeat`

`src/<pkg>/defs/automations/defs.yaml`:

```yaml
type: <your_pkg>.components.event_automation.component.EventAutomationComponent
attributes:
  name: demo_alert_on_failure_plus_heartbeat
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
      body_template: >
        {"event":"{event_type}","job":"{job_name}","run_id":"{run_id}"}
```

**Two triggers, one action bundle.** Both fire the same webhook, but with different template tokens filled in.

### `production_alert_shape` (STOPPED by default)

The same shape you'd ship to prod — Slack + PagerDuty alerting via env-var-driven webhooks:

```yaml
type: <your_pkg>.components.event_automation.component.EventAutomationComponent
attributes:
  name: production_alert_shape
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
```

Left STOPPED so the demo doesn't need real credentials. To activate:

```bash
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
export PAGERDUTY_ROUTING_KEY='...'
# Edit the defs.yaml → default_status: RUNNING
uv run dg dev
```

### `platform_observability_tour` (STOPPED by default)

Reference automation exercising the six platform-health triggers in one YAML. Same STOPPED-by-default rationale — no real credentials needed to inspect the shape in the UI. Load it into `dg dev` to see six sensors emitted (one per trigger) all pointing at the same webhook.

```yaml
type: <your_pkg>.components.event_automation.component.EventAutomationComponent
attributes:
  name: platform_observability_tour
  description: |
    Reference wiring for platform-health signals: daemon / agent
    heartbeat, code location deploys, slow compute startup, step errors,
    metadata state, and container log patterns (K8s / ECS / Docker
    stdout+stderr). Left STOPPED — set the webhook env var and toggle to
    RUNNING in the UI to activate.
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
```

## Watch it fire

1. Open <http://localhost:3000>
2. Navigate to **Jobs → `job_that_fails`**
3. Click **Materialize** (or Launch Run) — it fails on purpose
4. Navigate to **Sensors** tab. The setup script emits 8 sensors across the three automations:
   - `demo_alert_on_failure_plus_heartbeat__run_status_0` (the FAILURE watcher — RUNNING)
   - `demo_alert_on_failure_plus_heartbeat__schedule_1` (the cron heartbeat — RUNNING)
   - `production_alert_shape__run_status_0` (Slack + PagerDuty on failure — STOPPED)
   - `platform_observability_tour__daemon_heartbeat_0` (agent / daemon watch — STOPPED)
   - `platform_observability_tour__code_location_status_1` (deploy failure — STOPPED)
   - `platform_observability_tour__run_startup_slow_2` (compute spinup — STOPPED)
   - `platform_observability_tour__step_error_3` (step-level OOM / Timeout — STOPPED)
   - `platform_observability_tour__metadata_match_4` (quality_grade regex — STOPPED)
   - `platform_observability_tour__log_pattern_5` (events + stdout + stderr scan — STOPPED)
5. The `run_status_0` sensor ticks within ~30s of the failure and POSTs to `https://httpbin.org/post`. Click into its **Tick History** to see the sensor evaluation timeline.
6. The `schedule_1` sensor ticks every minute, hitting `https://httpbin.org/post` with `event_type=schedule`.
7. Toggle any of the platform-observability sensors to RUNNING to see their tick history — most will `SkipReason` on a clean cluster (nothing stale, no OOMs), which is the correct signal. Trigger a real failure by killing the daemon process, breaking a code location's imports, or launching a run that OOMs to see them fire.

## Trigger + action catalog (22 triggers, 17 actions)

Every automation is `when: [triggers…]` OR-composition + `then: [actions…]` all-run-sequentially. Compound triggers (`all_of` + `any_of`, one level of nesting) support real AND/OR logic.

**Triggers** (`when:`):

| Type | Fires on |
|---|---|
| `run_status` | Any run finishing with `SUCCESS/FAILURE/CANCELED/STARTED` |
| `asset_materialized` | Named assets get materialized |
| `schedule` | Cron (schedule → sensor with cron gating) |
| `http_poll` | Poll a URL; fires on response change, HTTP 2xx, or JSON path non-empty |
| `freshness_violation` | Asset stale beyond `max_age_minutes` (ongoing DQ) |
| `run_duration` | Run finished + duration > threshold (slow-run detector) |
| `run_stuck` | Active run running > threshold (once-per-run guard) |
| `asset_check_failed` | Named asset check evaluated FAILURE |
| `metric_threshold` | Numeric metadata crossed a threshold (gt/gte/lt/lte/eq/neq) |
| `absence` | Dead-man's switch: no materialization in `max_gap_minutes` |
| `log_pattern` | Regex match on run log lines (events / stdout / stderr — covers K8s / ECS container output) |
| `daemon_heartbeat` | Dagster daemon / Dagster+ agent stopped heartbeating |
| `code_location_status` | Code location failed to load / stuck loading / errored |
| `run_startup_slow` | Run took too long from creation to STARTED (compute spinup) |
| `asset_observation` | AssetObservation event emitted (distinct from materialization) |
| `step_error` | Op step raised an exception (step-level, not run-level; fires N times per multi-error run) |
| `metadata_match` | Materialization/observation carries specific metadata key=value (or key/regex) |
| `asset_value_change` | Numeric metadata Δ across two consecutive materializations |
| `backfill_status` | Partition backfill entered a state (COMPLETED/FAILED/CANCELED/REQUESTED) |
| `sensor_failing` | Target sensor failed N consecutive ticks (meta-observability) |
| `concurrency_hit` | Active-run count > threshold, optional tag filter |
| `sqs_poll` | Poll an AWS SQS queue, fire per message |
| `all_of` | AND compound (all sub-triggers fire within `within_seconds`) |
| `any_of` | OR compound (nested inside `all_of` only) |

**Actions** (`then:`):

| Type | Effect |
|---|---|
| `materialize` | Launch a materialization run |
| `launch_job` | Launch a job |
| `cancel_run` | Terminate a run (`instance.run_launcher.terminate`) |
| `retry_run` | Re-execute a failed run |
| `toggle_sensor` | Start/stop a sensor by name |
| `toggle_schedule` | Start/stop a schedule by name |
| `webhook` | Arbitrary HTTP call with templated body |
| `slack` | Slack incoming-webhook alert |
| `pagerduty` | PagerDuty Events API v2 |
| `opsgenie` | OpsGenie Alerts API |
| `discord` | Discord webhook alert |
| `teams` | Microsoft Teams webhook |
| `mattermost` | Mattermost webhook |
| `email` | SMTP alert (stdlib smtplib) |
| `sns` | Publish to AWS SNS topic |
| `sqs` | Send to AWS SQS queue |
| `emit_event` | Log emission for downstream sensor chaining |

**Template tokens available in every action:** `{event_type}`, `{run_id}`, `{job_name}`, `{asset_key}`, `{status}`, `{timestamp}`, `{message}`, `{url}`.

Full field-level docs + more recipes in the component's [README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/sensors/event_automation/README.md).

## Common shapes

**Reprocess on upstream change:**

```yaml
when:
  - type: asset_materialized
    asset_keys: [raw_data]
then:
  - type: materialize
    asset_keys: [derived_data, aggregated_data]
```

**Freshness → auto-heal + escalate:**

```yaml
when:
  - type: freshness_violation
    asset_keys: [hourly_summary]
    max_age_minutes: 90
then:
  - type: materialize
    asset_keys: [hourly_summary]        # attempt self-heal
  - type: pagerduty
    routing_key_env_var: PD_ROUTING_KEY
    severity: warning
    summary_template: "{asset_key} stale — {message}"
```

**External queue → job launch:**

```yaml
when:
  - type: http_poll
    url: "https://api.example.com/pending-batches"
    condition: json_path_present
    json_path: "pending"
    minimum_interval_seconds: 30
then:
  - type: launch_job
    job_name: process_batch
```

**Kill stuck runs:**

```yaml
when:
  - type: run_stuck
    max_running_seconds: 1800
then:
  - type: cancel_run
    which: triggering
  - type: pagerduty
    routing_key_env_var: PD_KEY
    severity: warning
    summary_template: "Killed stuck run {run_id} of {job_name}"
```

**Multi-signal correlation (AND / OR nested compound):**

```yaml
when:
  - type: all_of
    within_seconds: 3600
    triggers:
      - type: any_of              # nested OR inside AND
        triggers:
          - type: run_status
            status: FAILURE
            job_name: job_a
          - type: run_status
            status: FAILURE
            job_name: job_b
      - type: freshness_violation
        asset_keys: [hourly_summary]
        max_age_minutes: 120
then:
  - type: opsgenie
    api_key_env_var: OPSGENIE_KEY
    priority: P1
```

Reads as: **(job_a failed OR job_b failed) AND freshness violated, all within 1 hour → P1 OpsGenie**.

**AWS bridge (SQS in → SNS out):**

```yaml
when:
  - type: sqs_poll
    queue_url: "https://sqs.us-east-1.amazonaws.com/12345/ingest"
    region: us-east-1
then:
  - type: launch_job
    job_name: process_batch
  - type: sns
    topic_arn: "arn:aws:sns:us-east-1:12345:dagster-events"
    message_template: "Ingest: {message}"
```

**Full observability stack (7 triggers → PagerDuty):**

```yaml
when:
  - type: code_location_status
    on_status: UNHEALTHY
  - type: run_startup_slow
    max_startup_seconds: 120
  - type: daemon_heartbeat
    max_seconds_since_heartbeat: 90
  - type: asset_observation
    asset_keys: [external_status]
  - type: step_error
    exception_pattern: "OOMKilled|Timeout"
  - type: metadata_match
    asset_key: hourly_summary
    metadata_key: quality_grade
    regex: "poor|failed"
  - type: log_pattern
    pattern: "OOMKilled|SegFault|Panic"
    sources: [events, stdout, stderr]
then:
  - type: pagerduty
    routing_key_env_var: PD_KEY
    severity: error
    summary_template: "OBSERVABILITY: {event_type} — {message}"
```

**Agent / daemon heartbeat (Dagster+ Hybrid K8s / ECS agent died):**

```yaml
when:
  - type: daemon_heartbeat
    max_seconds_since_heartbeat: 120
then:
  - type: opsgenie
    api_key_env_var: OPSGENIE_KEY
    priority: P1
    message_template: "Agent stopped heartbeating: {status}"
```

**Code location deploy failure:**

```yaml
when:
  - type: code_location_status
    on_status: ERROR
    location_name_pattern: "prod-.*"
then:
  - type: slack
    webhook_url_env_var: SLACK_URL
    message: "🚨 Deploy failure: {message}"
```

**Container OOM via stderr scan (log_pattern with compute logs):**

```yaml
when:
  - type: log_pattern
    pattern: "OOMKilled|killed as memory limit"
    sources: [events, stderr]     # catches K8s/ECS oomkill traces
then:
  - type: pagerduty
    routing_key_env_var: PD_KEY
    severity: error
```

**OOM detection via log_pattern:**

```yaml
when:
  - type: log_pattern
    pattern: "OOMKilled|OutOfMemoryError|Killed process"
then:
  - type: pagerduty
    routing_key_env_var: PD_KEY
    severity: error
    summary_template: "OOM in {job_name}"
```

**Revenue drop via asset_value_change:**

```yaml
when:
  - type: asset_value_change
    asset_key: daily_revenue
    metadata_key: total
    direction: decrease
    min_delta_pct: 20              # fires on 20%+ drop
then:
  - type: slack
    webhook_url_env_var: SLACK_URL
    message: "📉 Revenue dropped: {message}"
```

**Broken-sensor detection (meta-observability):**

```yaml
when:
  - type: sensor_failing
    target_sensor_name: kafka_ingest_sensor
    consecutive_failures: 5
then:
  - type: pagerduty
    routing_key_env_var: PD_KEY
    severity: warning
    summary_template: "kafka_ingest_sensor failing 5 ticks"
```

**Concurrency overload guardrail:**

```yaml
when:
  - type: concurrency_hit
    max_queued: 100
    tag_key: dagster/job
    tag_value: heavy_batch
then:
  - type: cancel_run
    which: all_matching
    job_name_filter: heavy_batch
```

**Metric threshold → email:**

```yaml
when:
  - type: metric_threshold
    asset_key: hourly_summary
    metadata_key: row_count
    comparison: lt
    threshold: 100
then:
  - type: email
    smtp_host_env_var: SMTP_HOST
    smtp_user_env_var: SMTP_USER
    smtp_password_env_var: SMTP_PASSWORD
    from_addr: "alerts@example.com"
    to: ["oncall@example.com"]
    subject_template: "Low row count: {asset_key}"
    body_template: "{asset_key} row_count dropped below 100: {message}"
```

**Cron + webhook (heartbeat pattern):**

```yaml
when:
  - type: schedule
    cron: "0 * * * *"
then:
  - type: webhook
    url: "https://uptime.example.com/hourly-heartbeat"
    method: GET
```

## Why vs Dagster+ native alerting

Dagster+ Pro ships native Slack/PagerDuty/email alerting as a paid feature. This component intentionally duplicates the alerting surface so:

- **OSS Dagster users** get alert-on-failure without needing a Dagster+ Pro seat
- **Dagster+ Serverless** users on the entry tier can wire the same alerts without upgrading
- **Prefect migrators** can port their Automations 1:1 without changing plans

If you're already on Dagster+ Pro, the native notifications UI is more polished — one less moving piece in code. This component's value is the "same shape as Prefect Automations, works everywhere Dagster runs" story.

## Why vs writing a Python sensor

You could hand-write every automation as a `@sensor` — that's the current Dagster idiom. The component's value:

- **YAML-declarative** → editable in the Dagster+ components UI, no Python editing
- **Composable with other components** → drops into `defs.yaml` alongside anything else
- **Prefect-migration path** → same mental model, one-file-per-automation
- **Consistent template surface** → every action gets the same `{event_type}` / `{run_id}` / etc. tokens

Under the hood, each trigger IS a real Dagster sensor — you get the full Dagster+ sensor UI, tick history, backfill, everything. Zero magic.

## Verified

- **Component loads:** `dg check defs` → "All definitions loaded successfully"
- **Sensors materialize:** `dg list defs` shows one sensor per trigger, named `<automation>__<trigger_type>_<i>`
- **Registered in the community-components manifest** with `agent_hints` covering when-to-use / typical-upstream / typical-downstream / example-prompts

## See also

- Component source: [`sensors/event_automation/component.py`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/event_automation)
- Prefect Automations concept (for comparison): <https://docs.prefect.io/v3/concepts/automations>
- Umbrella deploy doc: [`deploying.md`](deploying.md)
